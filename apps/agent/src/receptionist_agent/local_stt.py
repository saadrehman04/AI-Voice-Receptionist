"""Local faster-whisper, used when Groq's hosted Whisper fails (DECISIONS D37).

It runs on the CPU (int8), so it is slower than Groq (about 1-2 s for a short utterance with
the "small" model on the dev laptop) but needs no key and no network. The model loads on first
use, in a thread, and is then shared by every call in the process.
"""

from __future__ import annotations

import asyncio
import io
import threading
import wave
from typing import Any, Protocol

import numpy as np

WHISPER_RATE = 16_000

_models: dict[str, Any] = {}
_models_lock = threading.Lock()


class Transcriber(Protocol):
    async def transcribe(self, wav: bytes, language: str | None) -> tuple[str, str | None]:
        """(text, detected language code) for one utterance in WAV format."""
        ...


def wav_to_float(wav: bytes) -> np.ndarray[Any, np.dtype[np.float32]]:
    """16-bit mono WAV -> float32 samples at 16 kHz, the format Whisper takes."""
    with wave.open(io.BytesIO(wav)) as reader:
        rate = reader.getframerate()
        pcm = reader.readframes(reader.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != WHISPER_RATE and len(samples):
        n = round(len(samples) * WHISPER_RATE / rate)
        samples = np.interp(
            np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples
        ).astype(np.float32)
    return samples


def _model(name: str) -> Any:
    with _models_lock:
        if name not in _models:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]

            _models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return _models[name]


class LocalWhisper:
    def __init__(self, model: str) -> None:
        self.model = model

    async def transcribe(self, wav: bytes, language: str | None) -> tuple[str, str | None]:
        def run() -> tuple[str, str | None]:
            segments, info = _model(self.model).transcribe(wav_to_float(wav), language=language)
            # `segments` decodes lazily: consume it here, in the worker thread.
            text = " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6)
            return text.strip(), getattr(info, "language", None)

        return await asyncio.to_thread(run)


__all__ = ["LocalWhisper", "Transcriber", "wav_to_float"]
