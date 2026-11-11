"""Azure TTS that goes quiet as soon as the caller interrupts (DECISIONS D30).

Pipecat 1.15's `AzureTTSService` handles an interruption by asking Azure to stop speaking and
*waiting for Azure's answer* before the interruption is passed on to the speaker. That is a
network round trip (about 400 ms from Pakistan to UAE North), and the speaker keeps playing
queued audio until it ends. The benchmark measured exactly that: audio ran 404 ms past the
interruption, versus 22-40 ms with a local voice.

This subclass passes the interruption on at once and lets Azure stop in the background. The
next sentence waits for that stop to finish (it is long done by the time the caller has spoken
and the LLM has answered), so a late "cancelled" signal from the old synthesis can't cut the
new sentence short.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger
from pipecat.frames.frames import Frame, InterruptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.azure.tts import AzureTTSService


class QuickStopAzureTTSService(AzureTTSService):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stopping: asyncio.Task[Any] | None = None

    async def _handle_interruption(
        self, frame: InterruptionFrame, direction: FrameDirection
    ) -> None:
        synthesizer: Any = self._speech_synthesizer  # Pipecat leaves its type as None
        # With no synthesizer the parent skips its blocking stop call but still resets its
        # word-timestamp state and empties its queues.
        self._speech_synthesizer = None
        try:
            await super()._handle_interruption(frame, direction)
        finally:
            self._speech_synthesizer = synthesizer
        if synthesizer is not None:
            future = synthesizer.stop_speaking_async()
            self._stopping = self.create_task(asyncio.to_thread(future.get), name="azure-stop")

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        if self._stopping is not None:
            stopping, self._stopping = self._stopping, None
            try:
                await stopping
            except Exception as exc:  # the old synthesis is gone either way
                logger.warning("azure stop failed", error=type(exc).__name__)
        async for frame in super().run_tts(text, context_id):
            yield frame
