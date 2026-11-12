"""The last safety net: when a provider error gets through, say something (DECISIONS D37).

Each provider already recovers on its own: STT retries then uses local Whisper, the LLM
transport moves across models and finally takes a message, Azure falls back to Piper. If an
error still reaches the pipeline (both STT engines failed, an LLM stream broke halfway), the
caller would otherwise hear nothing and wonder if the line is dead. This observer sees the
error and has the agent ask the caller to repeat themselves.

It speaks at most once per `MIN_GAP_SECS`, so a provider that keeps failing can't make it loop,
and never for an error from anything other than the STT, LLM and TTS services.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from loguru import logger
from pipecat.frames.frames import ErrorFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.service_switcher import ServiceSwitcher
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

MIN_GAP_SECS = 8.0

SORRY = {
    "en": "Sorry, I missed that because of a problem on my side. Could you say it again?",
    "ur": "معاف کیجیے، میری طرف سے کچھ مسئلہ ہوا۔ کیا آپ دوبارہ بتا سکتے ہیں؟",
}

_SERVICES = (STTService, LLMService, TTSService, ServiceSwitcher)


class NeverSilentGuard(BaseObserver):
    def __init__(
        self,
        speak: Callable[[str], Awaitable[None]],
        language: Callable[[], str | None],
        *,
        min_gap_secs: float = MIN_GAP_SECS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._speak = speak
        self._language = language
        self._gap = min_gap_secs
        self._clock = clock
        self._last_spoke: float | None = None
        self._seen: set[int] = set()  # an error frame is seen at every hop it travels
        self.apologies = 0

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if not isinstance(frame, ErrorFrame) or frame.id in self._seen:
            return
        self._seen.add(frame.id)
        if not isinstance(frame.processor, _SERVICES) and not isinstance(data.source, _SERVICES):
            return
        now = self._clock()
        if self._last_spoke is not None and now - self._last_spoke < self._gap:
            return
        self._last_spoke = now
        self.apologies += 1
        source = type(frame.processor or data.source).__name__
        logger.warning("provider error reached the call; apologising", source=source)
        await self._speak(SORRY["ur" if self._language() == "ur" else "en"])


__all__ = ["SORRY", "NeverSilentGuard"]
