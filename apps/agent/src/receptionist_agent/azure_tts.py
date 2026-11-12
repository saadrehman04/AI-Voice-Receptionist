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

Phase 9 (D37): if Azure fails (an error, a rejected key, or no audio within
`FIRST_AUDIO_SECS`) before a sentence has started playing, the same sentence is spoken by a
local Piper voice instead, so the caller still hears the reply, in a different voice.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, InterruptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.azure.tts import AzureTTSService

from receptionist_agent.local_tts import LocalVoice

FIRST_AUDIO_SECS = 5.0  # Azure's first audio normally comes in ~0.2-0.5 s
TRIP_AFTER = 2  # sentences in a row Azure failed on...
SKIP_SECS = 60.0  # ...before it is skipped this long (seen live: each failure cost ~0.5 s)


class QuickStopAzureTTSService(AzureTTSService):
    def __init__(
        self,
        *,
        local_fallback: LocalVoice | None = None,
        first_audio_secs: float = FIRST_AUDIO_SECS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._stopping: asyncio.Task[Any] | None = None
        self._local = local_fallback
        self._first_audio_secs = first_audio_secs
        self.served_locally = 0
        self._failures = 0
        self._skip_until = 0.0

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
        if self._local is not None and time.monotonic() < self._skip_until:
            async for frame in self._speak_locally(text, context_id, "azure skipped for now"):
                yield frame
            return
        azure = super().run_tts(text, context_id)
        failure: str | None = None
        try:
            # Until the first audio: watch for an error or a stall, and hold frames back so a
            # failed attempt leaves nothing behind.
            while True:
                try:
                    frame = await asyncio.wait_for(anext(azure), self._first_audio_secs)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    failure = "no audio in time"
                    break
                if isinstance(frame, ErrorFrame):
                    failure = frame.error[:200]
                    break
                yield frame
                if isinstance(frame, TTSAudioRawFrame):
                    break
            if failure is None:
                self._failures = 0
                async for frame in azure:  # playing: errors from here pass on as they are
                    yield frame
                return
        finally:
            await azure.aclose()
        self._failures += 1
        if self._failures >= TRIP_AFTER:
            # A pulled key or an outage: stop paying a failed round trip before every sentence.
            self._skip_until = time.monotonic() + SKIP_SECS
            logger.error("azure tts keeps failing; using the local voice", skip_secs=SKIP_SECS)
        async for frame in self._speak_locally(text, context_id, failure):
            yield frame

    async def _speak_locally(
        self, text: str, context_id: str, failure: str
    ) -> AsyncGenerator[Frame, None]:
        logger.warning("speaking with the local voice", reason=failure)
        if self._local is None:
            yield ErrorFrame(error=f"azure tts failed: {failure}")
            return
        try:
            rate = await self._local.sample_rate()
            async for frame in self._stream_audio_frames_from_iterator(
                self._local.speak(text), in_sample_rate=rate, context_id=context_id
            ):
                yield frame
            self.served_locally += 1
        except Exception as exc:
            logger.error("local voice failed too", error=type(exc).__name__)
            yield ErrorFrame(error=f"tts failed: {failure}; local voice: {type(exc).__name__}")
