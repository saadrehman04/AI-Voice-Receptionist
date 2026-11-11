"""Per-turn latency measurement.

`TurnTimingObserver` is a Pipecat observer: it sees every frame pushed between processors
without being part of the pipeline, so measuring never slows the audio path.

A turn starts when the caller stops speaking (VAD) and ends when the bot starts speaking.
In between we record when each stage first produced output:

    user_stopped_speaking  VADUserStoppedSpeakingFrame   caller went quiet (t=0)
    stt_final              TranscriptionFrame            final transcript arrived
    turn_released          UserStoppedSpeakingFrame      end-of-turn detector let the LLM run
    llm_first_token        LLMTextFrame                  first token from the LLM
    tts_first_audio        TTSAudioRawFrame              first synthesized audio chunk
    bot_started_speaking   BotStartedSpeakingFrame       audio actually started playing

Each completed turn becomes one JSON line in logs/turns.jsonl (see `TurnTiming.to_record`).
If the caller keeps talking after a pause, the turn restarts from the latest VAD stop, since
that is the silence the caller actually waits through.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

NS_PER_MS = 1_000_000

TurnCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _ms(start_ns: int | None, end_ns: int | None) -> float | None:
    if start_ns is None or end_ns is None:
        return None
    return round((end_ns - start_ns) / NS_PER_MS, 1)


@dataclass
class TurnTiming:
    """Timestamps (pipeline clock, ns) for one caller turn -> bot reply."""

    call_id: str
    turn_index: int
    user_stopped_ns: int
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    stt_final_ns: int | None = None
    turn_released_ns: int | None = None
    llm_first_token_ns: int | None = None
    tts_first_audio_ns: int | None = None
    bot_started_ns: int | None = None
    transcript: str = ""

    def to_record(self) -> dict[str, Any]:
        """Flat JSON-able record: offsets from t=0 plus per-stage durations, in ms."""
        t0 = self.user_stopped_ns
        llm_start = max(
            (t for t in (self.stt_final_ns, self.turn_released_ns) if t is not None),
            default=None,
        )
        end = self.bot_started_ns if self.bot_started_ns is not None else self.tts_first_audio_ns
        return {
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "turn_index": self.turn_index,
            "started_at": self.started_at,
            "transcript_chars": len(self.transcript),
            # offsets from the moment the caller stopped speaking
            "user_stopped_speaking": 0.0,
            "stt_final": _ms(t0, self.stt_final_ns),
            "turn_released": _ms(t0, self.turn_released_ns),
            "llm_first_token": _ms(t0, self.llm_first_token_ns),
            "tts_first_audio": _ms(t0, self.tts_first_audio_ns),
            "bot_started_speaking": _ms(t0, self.bot_started_ns),
            # how long each stage took
            "stages": {
                "stt": _ms(t0, self.stt_final_ns),
                "end_of_turn": _ms(t0, self.turn_released_ns),
                "llm": _ms(llm_start, self.llm_first_token_ns),
                "tts": _ms(self.llm_first_token_ns, self.tts_first_audio_ns),
                "playout": _ms(self.tts_first_audio_ns, self.bot_started_ns),
                "total": _ms(t0, end),
            },
        }


class TurnTimingObserver(BaseObserver):
    """Builds a `TurnTiming` per caller turn and hands finished ones to `on_turn`."""

    def __init__(self, *, call_id: str, on_turn: TurnCallback) -> None:
        super().__init__(observe_every_push=False)
        self._call_id = call_id
        self._on_turn = on_turn
        self._turn: TurnTiming | None = None
        self._turn_count = 0

    @property
    def current_turn(self) -> TurnTiming | None:
        return self._turn

    async def on_push_frame(self, data: FramePushed) -> None:
        if not data.first_push:
            return
        frame, ts = data.frame, data.timestamp

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._on_user_stopped(ts)
            return

        turn = self._turn
        if turn is None:
            return  # e.g. the greeting, which no caller turn triggered

        if isinstance(frame, TranscriptionFrame):
            if turn.stt_final_ns is None:
                turn.stt_final_ns = ts
            turn.transcript = (turn.transcript + " " + frame.text).strip()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            if turn.turn_released_ns is None:
                turn.turn_released_ns = ts
        elif isinstance(frame, LLMTextFrame):
            if turn.llm_first_token_ns is None:
                turn.llm_first_token_ns = ts
        elif isinstance(frame, TTSAudioRawFrame):
            if turn.llm_first_token_ns is not None and turn.tts_first_audio_ns is None:
                turn.tts_first_audio_ns = ts
        elif isinstance(frame, BotStartedSpeakingFrame) and turn.llm_first_token_ns is not None:
            turn.bot_started_ns = ts
            self._turn = None
            await self._finish(turn)

    def _on_user_stopped(self, ts: int) -> None:
        turn = self._turn
        if turn is not None and turn.llm_first_token_ns is None:
            # Caller paused and carried on before we answered: restart the clock and
            # drop the earlier transcript time, since the final transcript comes later.
            turn.user_stopped_ns = ts
            turn.stt_final_ns = None
            turn.turn_released_ns = None
            return
        if turn is not None:
            logger.debug("turn abandoned before the bot spoke", turn_id=turn.turn_id)
        self._turn_count += 1
        self._turn = TurnTiming(
            call_id=self._call_id, turn_index=self._turn_count, user_stopped_ns=ts
        )

    async def _finish(self, turn: TurnTiming) -> None:
        record = turn.to_record()
        logger.bind(turn_id=turn.turn_id, stages=record["stages"]).info("turn timing")
        await self._on_turn(record)


class JsonlTurnWriter:
    """Appends turn records to a JSON-lines file, off the event loop."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            await asyncio.to_thread(self._append, line)
