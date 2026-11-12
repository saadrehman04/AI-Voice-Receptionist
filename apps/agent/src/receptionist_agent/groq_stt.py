"""Groq Whisper STT: auto-detects the language, retries once, then falls back to local Whisper.

Auto language: Pipecat 1.15's `GroqSTTService` always sends a `language` (default English) and
asserts it is set. A bilingual line needs Whisper to detect Urdu or English per utterance,
which means *not* sending one, so with `STT_LANGUAGE` unset this subclass leaves the field out.
Found by the Phase 5 benchmark: with `language=None` every transcription failed.

Failure handling (Phase 9, DECISIONS D37): a Groq error or a reply slower than
`STT_TIMEOUT_SECS` is retried once; if that fails too, the same audio goes to local
faster-whisper, so the caller's words are never dropped. After `TRIP_AFTER` failed utterances
in a row (a pulled key, an outage) Groq is skipped for `SKIP_SECS` and utterances go straight to
the local model, instead of making the caller wait for two doomed requests each turn.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.groq.stt import GroqSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

from receptionist_agent.local_stt import Transcriber

STT_TIMEOUT_SECS = 4.0  # Groq answers a short utterance in ~0.3-0.6 s
TRIP_AFTER = 2
SKIP_SECS = 60.0


class AutoLanguageGroqSTTService(GroqSTTService):
    def __init__(
        self,
        *,
        local_fallback: Transcriber | None = None,
        timeout_secs: float = STT_TIMEOUT_SECS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._local = local_fallback
        self._timeout = timeout_secs
        self._failures = 0  # utterances in a row that Groq failed
        self._skip_until = 0.0
        self.served_locally = 0

    async def _transcribe(self, audio: bytes) -> Any:
        settings: Any = self._settings  # Pipecat's settings type is not visible to mypy
        if settings.language is not None:
            return await super()._transcribe(audio)
        kwargs: dict[str, Any] = {
            "file": ("audio.wav", audio, "audio/wav"),
            "model": settings.model,
            "response_format": "verbose_json" if self._include_prob_metrics else "json",
        }
        if settings.prompt is not None:
            kwargs["prompt"] = settings.prompt
        if settings.temperature is not None:
            kwargs["temperature"] = settings.temperature
        return await self._client.audio.transcriptions.create(**kwargs)

    async def _groq_once(self, audio: bytes) -> list[Frame] | None:
        """Groq's frames for this utterance, or None if it failed or was too slow."""
        frames: list[Frame] = []
        try:
            async with asyncio.timeout(self._timeout):
                async for frame in super().run_stt(audio):
                    if isinstance(frame, ErrorFrame):
                        logger.warning("groq stt failed", error=frame.error[:200])
                        return None
                    frames.append(frame)
        except TimeoutError:
            logger.warning("groq stt timed out", timeout_secs=self._timeout)
            return None
        return frames

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if time.monotonic() >= self._skip_until:
            for _attempt in range(2):  # try, then retry once
                frames = await self._groq_once(audio)
                if frames is not None:
                    self._failures = 0
                    for frame in frames:
                        yield frame
                    return
            self._failures += 1
            if self._failures >= TRIP_AFTER:
                self._skip_until = time.monotonic() + SKIP_SECS
                logger.error("groq stt keeps failing; using local whisper", skip_secs=SKIP_SECS)
        async for frame in self._run_local(audio):
            yield frame

    async def _run_local(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if self._local is None:
            yield ErrorFrame(error="speech-to-text unavailable (Groq failed, no local fallback)")
            return
        settings: Any = self._settings
        language = settings.language.value if settings.language is not None else None
        try:
            text, detected = await self._local.transcribe(audio, language)
        except Exception as exc:
            logger.error("local whisper failed", error=type(exc).__name__)
            yield ErrorFrame(error=f"local speech-to-text failed: {type(exc).__name__}")
            return
        self.served_locally += 1
        if not text:
            return
        try:
            spoken = Language(detected) if detected else None
        except ValueError:
            spoken = None
        logger.info("stt served by local whisper", language=detected)
        yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), spoken)
