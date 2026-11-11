"""Azure TTS passes an interruption on at once, while Azure stops in the background."""

import time
from typing import Any

from pipecat.frames.frames import Frame, InterruptionFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.azure.tts import AzureTTSService
from pipecat.tests.utils import SleepFrame, run_test

from receptionist_agent.azure_tts import QuickStopAzureTTSService

AZURE_STOP_SECS = 0.5  # Azure's stop answer, Pakistan -> UAE North, was about 0.4 s


class SlowStop:
    def __init__(self) -> None:
        self.stops = 0

    def stop_speaking_async(self) -> Any:
        self.stops += 1

        class Future:
            def get(self) -> None:
                time.sleep(AZURE_STOP_SECS)

        return Future()


class Stamp(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.at = 0.0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame) and not self.at:
            self.at = time.monotonic()
        await self.push_frame(frame, direction)


def with_fake_synthesizer(cls: type[AzureTTSService], fake: SlowStop) -> AzureTTSService:
    class Probe(cls):  # type: ignore[valid-type,misc]
        async def start(self, frame: StartFrame) -> None:
            await super().start(frame)
            self._speech_synthesizer = fake

    return Probe(api_key="test-key", region="uaenorth")


async def interruption_delay(cls: type[AzureTTSService]) -> tuple[float, int]:
    fake = SlowStop()
    before, after = Stamp(), Stamp()
    pipeline = Pipeline([before, with_fake_synthesizer(cls, fake), after])
    await run_test(
        pipeline, frames_to_send=[InterruptionFrame(), SleepFrame(AZURE_STOP_SECS + 0.3)]
    )
    return after.at - before.at, fake.stops


async def test_interruption_reaches_the_speaker_without_waiting_for_azure() -> None:
    delay, stops = await interruption_delay(QuickStopAzureTTSService)
    assert delay < 0.1
    assert stops == 1  # Azure is still told to stop


async def test_pipecats_azure_service_waits_for_the_round_trip() -> None:
    """The bug the subclass exists for: if this ever fails, Pipecat fixed it upstream."""
    delay, _ = await interruption_delay(AzureTTSService)
    assert delay >= AZURE_STOP_SECS * 0.9


async def test_next_sentence_waits_for_the_background_stop() -> None:
    import asyncio

    tts = QuickStopAzureTTSService(api_key="test-key", region="uaenorth")
    tts._speech_synthesizer = None  # run_tts returns at once without a synthesizer

    async def slow_stop() -> None:
        await asyncio.sleep(0.3)

    tts._stopping = asyncio.create_task(slow_stop())
    started = time.monotonic()
    frames = [f async for f in tts.run_tts("hello", "ctx")]
    assert time.monotonic() - started >= 0.29
    assert frames == [] and tts._stopping is None
