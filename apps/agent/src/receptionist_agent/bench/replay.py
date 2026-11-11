"""A fake phone line for benchmarks: plays caller audio in, "hears" the agent's audio out.

`ReplayTransport` is a Pipecat transport like SmallWebRTC, minus the network:

- Input streams 20 ms chunks in real time, silence when no utterance is queued (VAD needs to
  hear silence to notice the caller stopped). `play()` queues an utterance and returns when its
  last chunk has gone in, so the caller knows exactly when it *really* stopped speaking.
- Output "plays" each agent audio chunk by sleeping for its duration (like a speaker would) and
  records when audio was written, so barge-in can be timed by when audio actually stopped.

Everything runs the real pipeline: Silero VAD, Smart Turn, STT, LLM, TTS. Only the wire is fake,
so numbers exclude browser/network time (about 20-50 ms on a local WebRTC call).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

CHUNK_SECS = 0.02


@dataclass
class Utterance:
    chunks: deque[bytes]
    started: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    started_ns: int = 0
    ended_ns: int = 0


class ReplayInput(BaseInputTransport):
    def __init__(self, params: TransportParams) -> None:
        super().__init__(params)
        self._queue: deque[Utterance] = deque()
        self._feeder: asyncio.Task[None] | None = None

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        if self._feeder is None:
            self._feeder = self.create_task(self._feed(), name="replay-feed")

    async def _feed(self) -> None:
        chunk_bytes = int(self.sample_rate * CHUNK_SECS) * 2
        silence = b"\x00" * chunk_bytes
        next_at = time.monotonic()
        while True:
            utterance = self._queue[0] if self._queue else None
            if utterance is not None and utterance.chunks:
                if not utterance.started_ns:
                    utterance.started_ns = time.monotonic_ns()
                    utterance.started.set()
                audio = utterance.chunks.popleft().ljust(chunk_bytes, b"\x00")
            else:
                audio = silence
            await self.push_audio_frame(
                InputAudioRawFrame(audio=audio, sample_rate=self.sample_rate, num_channels=1)
            )
            if utterance is not None and not utterance.chunks:
                utterance.ended_ns = time.monotonic_ns()
                utterance.done.set()
                self._queue.popleft()
            next_at += CHUNK_SECS
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    def start_playing(self, pcm16: bytes) -> Utterance:
        """Queue `pcm16` (mono, at this transport's input rate); its events say when it plays."""
        size = int(self.sample_rate * CHUNK_SECS) * 2
        utterance = Utterance(deque(pcm16[i : i + size] for i in range(0, len(pcm16), size)))
        self._queue.append(utterance)
        return utterance

    async def play(self, pcm16: bytes) -> tuple[int, int]:
        """Speak `pcm16` and wait until it has all gone in. Returns (start_ns, end_ns)."""
        utterance = self.start_playing(pcm16)
        await utterance.done.wait()
        return utterance.started_ns, utterance.ended_ns

    async def _stop_feeder(self) -> None:
        if self._feeder is not None:
            await self.cancel_task(self._feeder)
            self._feeder = None

    async def cleanup(self) -> None:
        await self._stop_feeder()
        await super().cleanup()  # type: ignore[no-untyped-call]


class ReplayOutput(BaseOutputTransport):
    def __init__(self, params: TransportParams) -> None:
        super().__init__(params)
        self.writes: list[tuple[int, int]] = []  # (written_at_ns, duration_ns)
        self.audio = bytearray()

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        duration = len(frame.audio) / 2 / frame.sample_rate / max(frame.num_channels, 1)
        self.writes.append((time.monotonic_ns(), int(duration * 1e9)))
        self.audio.extend(frame.audio)
        await asyncio.sleep(duration)  # a speaker takes this long to play it
        return True

    def audio_since(self, t_ns: int) -> list[tuple[int, int]]:
        return [w for w in self.writes if w[0] >= t_ns]


class ReplayTransport(BaseTransport):
    def __init__(self, *, in_rate: int = 16000, out_rate: int = 24000) -> None:
        super().__init__()
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=in_rate,
            audio_out_sample_rate=out_rate,
            audio_out_end_silence_secs=0,  # Pipecat pads a hang-up with 2 s of silence; not speech
        )
        self._input = ReplayInput(params)
        self._output = ReplayOutput(params)

    def input(self) -> FrameProcessor:
        return self._input

    def output(self) -> FrameProcessor:
        return self._output

    @property
    def caller(self) -> ReplayInput:
        return self._input

    @property
    def speaker(self) -> ReplayOutput:
        return self._output
