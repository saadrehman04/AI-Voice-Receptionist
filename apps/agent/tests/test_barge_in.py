"""Barge-in: when the caller talks over the agent, its audio stops and the context keeps only
what was actually played. Real Pipecat TTS base class, output transport and assistant
aggregator; fake LLM and TTS audio, so no models or network.
"""

import time
from collections.abc import AsyncGenerator

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import SleepFrame, run_test

from receptionist_agent.bench.replay import ReplayTransport

SENTENCES = [
    "We have haircuts, blow dries and colour.",
    "A haircut is two thousand five hundred rupees.",
    "Colour starts at eight thousand rupees.",
]
SECS_PER_SENTENCE = 0.6
RATE = 24000


class Reply(TextFrame):
    """Test-only trigger: 'the LLM answers now'."""


class FakeLLM(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, Reply):
            await self.push_frame(LLMFullResponseStartFrame())
            for sentence in SENTENCES:
                await self.push_frame(LLMTextFrame(sentence + " "))
            await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)


class SlowTTS(TTSService):
    """Real TTSService plumbing; each sentence becomes 0.6 s of silence 'speech'."""

    def __init__(self) -> None:
        super().__init__(sample_rate=RATE)

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        samples = int(RATE * SECS_PER_SENTENCE)
        for _ in range(6):  # streamed in pieces, like a real TTS
            yield TTSAudioRawFrame(b"\x00\x00" * (samples // 6), RATE, 1, context_id=context_id)


class Clock(FrameProcessor):
    """Notes when the interruption went past."""

    interrupted_ns = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and not self.interrupted_ns:
            self.interrupted_ns = time.monotonic_ns()
        await self.push_frame(frame, direction)


async def test_caller_talking_over_the_agent_stops_it_and_trims_the_context() -> None:
    transport = ReplayTransport(out_rate=RATE)
    context = LLMContext()
    assistant = LLMContextAggregatorPair(context).assistant()
    clock = Clock()
    # The clock sits right before the speaker, so it sees the interruption when the output does.
    pipeline = Pipeline([FakeLLM(), SlowTTS(), clock, transport.output(), assistant])

    # Interrupt partway through the second sentence.
    await run_test(
        pipeline,
        frames_to_send=[
            Reply("go"),
            SleepFrame(SECS_PER_SENTENCE * 1.5),
            InterruptionFrame(),
            SleepFrame(1.0),
        ],
    )

    writes = transport.speaker.writes
    assert clock.interrupted_ns, "the interruption never reached the pipeline"
    hit = clock.interrupted_ns
    playing_until = max(w + d for w, d in writes if w <= hit + 50_000_000)
    # Audio stops with the chunk that was already playing, well inside 200 ms...
    assert (playing_until - hit) / 1e6 < 200
    # ...and nothing starts playing afterwards.
    assert [w for w, _ in writes if w > hit + 50_000_000] == []
    # ...and less than the full reply was ever played.
    played_secs = sum(d for _, d in writes) / 1e9
    assert played_secs < SECS_PER_SENTENCE * len(SENTENCES) - 0.3

    said = " ".join(
        str(m["content"])
        for m in context.get_messages()
        if isinstance(m, dict) and m.get("role") == "assistant"
    )
    assert SENTENCES[0] in said  # played in full, so it stays in the history
    assert SENTENCES[2] not in said  # never played, so the LLM must not think it was said
