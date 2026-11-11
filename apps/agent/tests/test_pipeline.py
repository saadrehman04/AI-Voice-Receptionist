"""Run the timing observer inside a real Pipecat pipeline with fake caller / LLM / TTS.

No audio, network or models: each fake processor emits the frames the real service would,
so this checks our wiring against Pipecat's actual frame-push and clock semantics.
"""

from typing import Any, cast

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.tests.utils import run_test
from pipecat.transports.base_transport import BaseTransport

from receptionist_agent.bot import build_task
from receptionist_agent.providers import Services
from receptionist_agent.timing import TurnTimingObserver


class CallerTrigger(TextFrame):
    """Test-only frame: 'the caller just said <text>'."""


class FakeCaller(FrameProcessor):
    """Turns a CallerTrigger into what VAD + STT + end-of-turn would emit."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, CallerTrigger):
            await self.push_frame(VADUserStoppedSpeakingFrame())
            await self.push_frame(TranscriptionFrame(frame.text, "caller", ""))
            await self.push_frame(UserStoppedSpeakingFrame())
        else:
            await self.push_frame(frame, direction)


class FakeLLM(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame):
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame("Sure, what day works for you?"))
            await self.push_frame(LLMFullResponseEndFrame())


class FakeTTS(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, LLMTextFrame):
            await self.push_frame(TTSAudioRawFrame(b"\x00\x00" * 320, 16000, 1))
            await self.push_frame(BotStartedSpeakingFrame())


async def test_observer_records_a_turn_in_a_real_pipeline() -> None:
    records: list[dict[str, Any]] = []

    async def on_turn(record: dict[str, Any]) -> None:
        records.append(record)

    observer = TurnTimingObserver(call_id="call-xyz", on_turn=on_turn)
    pipeline = Pipeline([FakeCaller(), FakeLLM(), FakeTTS()])

    await run_test(
        pipeline,
        frames_to_send=[CallerTrigger("I'd like to book a haircut")],
        observers=[observer],
    )

    assert len(records) == 1
    record = records[0]
    assert record["call_id"] == "call-xyz"
    stages = record["stages"]
    # Every stage was observed and time only moves forward.
    for name in ("stt", "end_of_turn", "llm", "tts", "playout", "total"):
        assert stages[name] is not None, name
        assert stages[name] >= 0, name
    assert record["bot_started_speaking"] == stages["total"]


class FakeTransport(BaseTransport):
    def __init__(self) -> None:
        super().__init__()
        self._in = FrameProcessor(name="transport-in")
        self._out = FrameProcessor(name="transport-out")

    def input(self) -> FrameProcessor:
        return self._in

    def output(self) -> FrameProcessor:
        return self._out


def _flatten(processor: FrameProcessor) -> list[FrameProcessor]:
    if isinstance(processor, Pipeline):
        return [leaf for child in processor.processors for leaf in _flatten(child)]
    return [processor]


def test_build_task_wires_processors_in_order() -> None:
    stt, llm, tts = (FrameProcessor(name=n) for n in ("stt", "llm", "tts"))
    services = Services(
        stt=cast(STTService, stt), llm=cast(LLMService, llm), tts=cast(TTSService, tts)
    )
    task, context = build_task(FakeTransport(), services, call_id="c-1")
    order = [p.name for p in _flatten(task.pipeline)]
    assert order.index("transport-in") < order.index("stt") < order.index("llm")
    assert order.index("llm") < order.index("tts") < order.index("transport-out")
    assert context.messages == []
