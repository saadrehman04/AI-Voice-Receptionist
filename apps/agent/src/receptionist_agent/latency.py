"""Latency features, each behind a flag so the benchmark can measure before/after (Phase 5).

- **Text chunking** (`TTS_TEXT_CHUNKING`): when the LLM's words go to TTS.
  `full` waits for the whole reply (the "no streaming" baseline). `sentence` is Pipecat's
  default. `clause` lets the *first* chunk of a reply end at a comma once it has a few words, so
  the voice can start before the first sentence is finished; the rest goes sentence by sentence.
- **Fillers** (`FILLER_AFTER_MS`): if a tool call has been running this long and the agent hasn't
  said anything yet this turn, say "One moment." (or the Urdu equivalent). The filler is not
  added to the LLM context: an assistant message between a tool call and its result would make
  the next request invalid, and the model shouldn't learn to say it.
- **Pre-warm** (`PREWARM_CONNECTIONS`): open the LLM and STT HTTPS connections when the call
  starts, so the first turn doesn't pay for DNS + TLS.
- **VAD stop** (`VAD_STOP_SECS`): how much silence ends the caller's speech.
- **Number pauses** (`VAD_NUMBER_STOP_SECS`): after the agent asks for a phone number, the next
  caller turn needs this much silence to end, because people pause between digit groups
  ("zero three zero zero ... one two three"). Found by the benchmark: at 0.2 s the turn ended
  after the first group and the agent answered half a number.

Simple-turn routing to a smaller model lives in `llm_fallback` (it is a choice of target).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    Frame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
    VADParamsUpdateFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.utils.text.base_text_aggregator import Aggregation, TextType
from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

from receptionist_shared import Settings

TextChunking = Literal["full", "sentence", "clause"]

FILLERS = {"en": "One moment.", "ur": "جی، ایک لمحہ۔"}
CLAUSE_MARKS = ",;:،؛"  # incl. the Urdu comma and semicolon
MIN_CLAUSE_WORDS = 4


@dataclass(frozen=True)
class LatencyFlags:
    text_chunking: TextChunking = "clause"
    filler_after_ms: int = 400  # 0 = no fillers
    prewarm: bool = True
    vad_stop_secs: float = 0.2
    vad_number_stop_secs: float = 0.8  # 0 = same as vad_stop_secs

    @classmethod
    def from_settings(cls, settings: Settings) -> LatencyFlags:
        return cls(
            text_chunking=settings.tts_text_chunking,
            filler_after_ms=settings.filler_after_ms,
            prewarm=settings.prewarm_connections,
            vad_stop_secs=settings.vad_stop_secs,
            vad_number_stop_secs=settings.vad_number_stop_secs,
        )


# ---------------------------------------------------------------- text chunking


class FirstClauseAggregator(SimpleTextAggregator):
    """Pipecat's sentence aggregator, except a reply's first chunk may end at a comma."""

    def __init__(self, *, min_words: int = MIN_CLAUSE_WORDS, language: str | None = None) -> None:
        super().__init__(  # type: ignore[no-untyped-call]  # Pipecat leaves it untyped
            aggregation_type=TextType.SENTENCE, language=language
        )
        self._min_words = min_words
        self._first_sent = False

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        for char in text:
            self._text += char
            result = await self._check_sentence_with_lookahead(char)
            if result is not None:
                self._first_sent = True
                yield result
            elif (
                not self._first_sent
                and char.isspace()  # "a comma then a space", so "2,500" is never split
                and len(self._text) >= 2
                and self._text[-2] in CLAUSE_MARKS
                and len(self._text.split()) >= self._min_words
            ):
                clause, self._text = self._text.strip(" "), ""
                await super().handle_interruption()  # type: ignore[no-untyped-call]  # lookahead reset
                self._first_sent = True
                yield Aggregation(text=clause, type=TextType.SENTENCE)

    async def flush(self) -> Aggregation | None:
        self._first_sent = False  # flush = end of a reply
        return await super().flush()

    async def handle_interruption(self) -> None:
        self._first_sent = False
        await super().handle_interruption()  # type: ignore[no-untyped-call]


def use_clause_chunking(tts: FrameProcessor) -> bool:
    """Swap a TTS service's sentence aggregator for `FirstClauseAggregator` (same language).

    Pipecat 1.15 has no constructor argument for a custom aggregator, so this replaces the
    private attribute; `test_latency` checks the swap still takes effect after an upgrade.
    Anything that isn't a Pipecat TTS service (e.g. a test fake) is left alone.
    """
    if not isinstance(tts, TTSService):
        return False
    current = tts._text_aggregator
    tts._text_aggregator = FirstClauseAggregator(language=current.language)
    return True


class FullReplyBuffer(FrameProcessor):
    """The no-streaming baseline: hold a reply's text until the LLM has finished it."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._parts = []
            elif isinstance(frame, LLMTextFrame):
                self._parts.append(frame.text)
                return
            elif isinstance(frame, LLMFullResponseEndFrame) and self._parts:
                text, self._parts = "".join(self._parts), []
                await self.push_frame(LLMTextFrame(text), direction)
            elif isinstance(frame, InterruptionFrame):
                self._parts = []
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------- fillers


SLOW_REPLY_TASK = "slow-reply-filler"
SLOW_REPLY_MS = 2500  # well above a healthy Groq first token (~0.7 s), below Gemini's (~4 s)


class ToolFiller(FrameProcessor):
    """Sits between the LLM and TTS; says a short filler while a slow tool call runs, or when
    the LLM itself is slow to start answering (Phase 9: a busy Groq hands the turn to Gemini,
    which can take 4-6 s; the caller hears "One moment" instead of silence)."""

    def __init__(
        self,
        *,
        after_ms: int,
        language: Callable[[], str | None],
        slow_reply_ms: int = SLOW_REPLY_MS,
    ) -> None:
        super().__init__()
        self._after = after_ms / 1000
        self._slow_reply = slow_reply_ms / 1000
        self._language = language
        self._spoke = False  # the agent has said something since the caller's last turn
        self._timer: asyncio.Task[None] | None = None
        self.fillers_said = 0

    async def _cancel_timer(self) -> None:
        if self._timer is not None:
            await self.cancel_task(self._timer)
            self._timer = None

    async def _say_filler_later(self, after: float | None = None) -> None:
        await asyncio.sleep(self._after if after is None else after)
        if self._spoke:
            return
        self._spoke = True
        self.fillers_said += 1
        text = FILLERS.get(self._language() or "en", FILLERS["en"])
        logger.debug("tool filler", text=text)
        await self.push_frame(TTSSpeakFrame(text, append_to_context=False))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, (UserStoppedSpeakingFrame, InterruptionFrame)):
                self._spoke = False
                await self._cancel_timer()
            elif isinstance(frame, LLMTextFrame) and frame.text.strip():
                self._spoke = True
                await self._cancel_timer()
            elif isinstance(frame, FunctionCallsStartedFrame) and not self._spoke:
                await self._cancel_timer()
                self._timer = self.create_task(self._say_filler_later(), name="tool-filler")
            elif (
                isinstance(frame, LLMFullResponseStartFrame)
                and not self._spoke
                and self._slow_reply > 0
                and self._timer is None
            ):
                self._timer = self.create_task(
                    self._say_filler_later(self._slow_reply), name=SLOW_REPLY_TASK
                )
            elif (
                isinstance(frame, LLMFullResponseEndFrame)
                and self._timer is not None
                and self._timer.get_name() == SLOW_REPLY_TASK
            ):
                await self._cancel_timer()  # the reply ended (even if empty): nothing to cover
        await self.push_frame(frame, direction)

    async def cleanup(self) -> None:
        await self._cancel_timer()
        await super().cleanup()  # type: ignore[no-untyped-call]


# ---------------------------------------------------------------- number pauses

ASKS_FOR_NUMBER = re.compile(r"\b(phone|number|digits|mobile)\b|نمبر|فون|موبائل", re.IGNORECASE)


class NumberPauseTuner(FrameProcessor):
    """After a reply that asks for a phone number, let the caller pause longer between digits.

    Sits after the LLM and sends `VADParamsUpdateFrame` upstream, to the user aggregator that
    owns the VAD; the normal setting comes back once that caller turn has ended.
    """

    def __init__(self, *, normal_secs: float, number_secs: float) -> None:
        super().__init__()
        self._normal = VADParams(stop_secs=normal_secs)
        self._number = VADParams(stop_secs=number_secs)
        self._reply: list[str] = []
        self._raised = False

    async def _set(self, params: VADParams) -> None:
        await self.push_frame(VADParamsUpdateFrame(params=params), FrameDirection.UPSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._reply = []
            elif isinstance(frame, LLMTextFrame):
                self._reply.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                text = "".join(self._reply)
                is_question = "?" in text or "؟" in text
                if is_question and ASKS_FOR_NUMBER.search(text) and not self._raised:
                    self._raised = True
                    await self._set(self._number)
            elif isinstance(frame, UserStoppedSpeakingFrame) and self._raised:
                self._raised = False
                await self._set(self._normal)
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------- pre-warm


async def prewarm(*services: Any) -> None:
    """Open each service's HTTPS connection now (a cheap GET), so turn one skips DNS + TLS."""

    async def warm(service: Any) -> None:
        client = getattr(service, "_client", None)
        models = getattr(client, "models", None)
        if models is None:
            return
        try:
            await asyncio.wait_for(models.list(), timeout=5)
        except Exception as exc:  # a failed warm-up costs nothing but the warm-up
            logger.debug("prewarm failed", service=type(service).__name__, error=type(exc).__name__)

    await asyncio.gather(*(warm(s) for s in services))
