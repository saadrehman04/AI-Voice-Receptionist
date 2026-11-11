"""Groq Whisper STT that can auto-detect the language per utterance.

Pipecat 1.15's `GroqSTTService` always sends a `language` (default English) and asserts it is
set. A bilingual line needs Whisper to detect Urdu or English per utterance, which means *not*
sending one, so with `STT_LANGUAGE` unset this subclass leaves the field out.
Found by the Phase 5 benchmark: with `language=None` every transcription failed.
"""

from __future__ import annotations

from typing import Any

from pipecat.services.groq.stt import GroqSTTService


class AutoLanguageGroqSTTService(GroqSTTService):
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
