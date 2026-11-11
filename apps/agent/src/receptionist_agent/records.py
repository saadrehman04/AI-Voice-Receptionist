"""Call records: the agent reports the call to the api as it happens.

`ApiCallRecorder` never blocks the audio path: every record goes onto an in-memory queue and a
background task sends them to the api in order, with a few retries. If the api is down the call
carries on; the record is lost (and logged), which is the right trade-off for a live call.

What gets recorded:
- call start (call_id, channel) and end (language, recording path) -> api queues the summary job
- every caller turn (what the aggregator added to the context) and every agent turn (what was
  actually spoken, flagged if the caller interrupted it)
- the latency breakdown for each reply, attached to that agent turn
- every tool call with its arguments, result and duration
"""

from __future__ import annotations

import asyncio
import contextlib
import wave
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from receptionist_shared.api_models import CallEnd, CallStart, Speaker, ToolCallIn, TurnIn

Send = Callable[[str, dict[str, Any]], Awaitable[None]]  # (path under /calls, json body)

RETRIES = 3
FLUSH_TIMEOUT_SECS = 10.0


def now() -> datetime:
    return datetime.now(UTC)


class CallRecorder(Protocol):
    def caller_turn(self, text: str) -> None: ...
    def agent_turn(self, text: str, *, interrupted: bool) -> None: ...
    def timing(self, record: dict[str, Any]) -> None: ...
    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        ok: bool,
        duration_ms: int,
    ) -> None: ...
    async def start(self) -> None: ...
    async def end(self, *, language: str | None, recording_path: str | None) -> None: ...


class MemoryRecorder:
    """Keeps records in lists. Used with the in-memory backend and in tests."""

    def __init__(self, call_id: str, channel: str = "webrtc") -> None:
        self.call_id = call_id
        self.channel = channel
        self.started = False
        self.turns: list[TurnIn] = []
        self.tools: list[ToolCallIn] = []
        self.ended: CallEnd | None = None
        self._idx = 0
        self._pending_timing: dict[str, Any] | None = None

    def _turn(self, speaker: Speaker, text: str, interrupted: bool, timings: Any) -> TurnIn:
        turn = TurnIn(
            idx=self._idx,
            speaker=speaker,
            text=text,
            interrupted=interrupted,
            timings=timings,
            spoken_at=now(),
        )
        self._idx += 1
        return turn

    def caller_turn(self, text: str) -> None:
        if text.strip():
            self.turns.append(self._turn(Speaker.CALLER, text, False, None))

    def agent_turn(self, text: str, *, interrupted: bool) -> None:
        if not text.strip():
            return
        timings, self._pending_timing = self._pending_timing, None
        self.turns.append(self._turn(Speaker.AGENT, text, interrupted, timings))

    def timing(self, record: dict[str, Any]) -> None:
        self._pending_timing = dict(record.get("stages") or {})

    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        ok: bool,
        duration_ms: int,
    ) -> None:
        self.tools.append(
            ToolCallIn(
                name=name,
                arguments=arguments,
                result=result,
                ok=ok,
                duration_ms=duration_ms,
                called_at=now(),
            )
        )

    async def start(self) -> None:
        self.started = True

    async def end(self, *, language: str | None, recording_path: str | None) -> None:
        self.ended = CallEnd(ended_at=now(), language=language, recording_path=recording_path)


class ApiCallRecorder(MemoryRecorder):
    """Same bookkeeping as MemoryRecorder, plus a background sender to the api."""

    def __init__(self, call_id: str, send: Send, channel: str = "webrtc") -> None:
        super().__init__(call_id, channel)
        self._send = send
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def _put(self, path: str, body: dict[str, Any]) -> None:
        self._queue.put_nowait((path, body))

    async def _sender(self) -> None:
        while (item := await self._queue.get()) is not None:
            path, body = item
            for attempt in range(1, RETRIES + 1):
                try:
                    await self._send(path, body)
                    break
                except Exception as exc:
                    if attempt == RETRIES:
                        logger.warning("call record dropped", path=path, error=str(exc))
                    else:
                        await asyncio.sleep(0.2 * attempt)

    async def start(self) -> None:
        await super().start()
        self._task = asyncio.create_task(self._sender())
        body = CallStart(call_id=self.call_id, channel=self.channel, started_at=now())
        self._put("", body.model_dump(mode="json"))

    def caller_turn(self, text: str) -> None:
        before = len(self.turns)
        super().caller_turn(text)
        if len(self.turns) > before:
            self._put(f"/{self.call_id}/turns", self.turns[-1].model_dump(mode="json"))

    def agent_turn(self, text: str, *, interrupted: bool) -> None:
        before = len(self.turns)
        super().agent_turn(text, interrupted=interrupted)
        if len(self.turns) > before:
            self._put(f"/{self.call_id}/turns", self.turns[-1].model_dump(mode="json"))

    def tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        ok: bool,
        duration_ms: int,
    ) -> None:
        super().tool_call(name, arguments, result, ok=ok, duration_ms=duration_ms)
        self._put(f"/{self.call_id}/tool-calls", self.tools[-1].model_dump(mode="json"))

    async def end(self, *, language: str | None, recording_path: str | None) -> None:
        await super().end(language=language, recording_path=recording_path)
        if self.ended is not None:
            self._put(f"/{self.call_id}/end", self.ended.model_dump(mode="json"))
        self._queue.put_nowait(None)
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, FLUSH_TIMEOUT_SECS)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                logger.warning("call records not fully flushed before timeout")


def write_wav(path: Path, audio: bytes, sample_rate: int, num_channels: int) -> None:
    """16-bit PCM WAV. Blocking: call through asyncio.to_thread."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(num_channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio)
