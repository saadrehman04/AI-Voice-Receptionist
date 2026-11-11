"""Caller audio for the benchmark: synthesised once, cached as 16 kHz mono WAVs.

A real recording at `<audio_dir>/recorded/<id>.wav` (16 kHz, mono, 16-bit) always wins over
the synthetic one, so the same benchmark can be re-run with real voices later.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

import numpy as np
import soxr

IN_RATE = 16000
KOKORO_DIR = Path.home() / ".cache/pipecat/kokoro-onnx"
PIPER_DIR = Path.home() / ".cache/pipecat/piper"
CALLER_EN_VOICE = "am_michael"  # not the agent's af_heart, so caller and agent sound different
CALLER_UR_VOICE = "ur_PK-fasih-medium"


def read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != IN_RATE:
            raise ValueError(f"{path} must be 16 kHz mono 16-bit")
        return w.readframes(w.getnframes())


def write_wav(path: Path, pcm16: bytes, rate: int = IN_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)


def _to_pcm16(floats: np.ndarray, rate: int) -> bytes:
    """Float samples in [-1, 1] at `rate` -> 16 kHz 16-bit PCM."""
    resampled = soxr.resample(floats.astype(np.float32), rate, IN_RATE)
    return bytes((np.clip(resampled, -1, 1) * 32767).astype("<i2").tobytes())


class Synth:
    """Loads each voice on first use (Kokoro for English callers, Piper for Urdu)."""

    def __init__(self) -> None:
        self._kokoro: Any = None
        self._piper: Any = None

    def english(self, text: str) -> bytes:
        if self._kokoro is None:
            from kokoro_onnx import Kokoro

            self._kokoro = Kokoro(
                str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin")
            )
        samples, rate = self._kokoro.create(text, voice=CALLER_EN_VOICE, lang="en-us")
        return _to_pcm16(np.asarray(samples), rate)

    def urdu(self, text: str) -> bytes:
        if self._piper is None:
            from piper import PiperVoice

            self._piper = PiperVoice.load(str(PIPER_DIR / f"{CALLER_UR_VOICE}.onnx"))
        chunks = list(self._piper.synthesize(text))
        pcm = b"".join(c.audio_int16_bytes for c in chunks)
        ints = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        return _to_pcm16(ints, self._piper.config.sample_rate)


SILENCE_LEVEL = 330  # ~-40 dBFS: quieter than this counts as silence
KEEP_SECS = 0.03


def trim_silence(pcm16: bytes) -> bytes:
    """Cut leading/trailing silence, so "the caller stopped" is when speech really ended."""
    samples = np.frombuffer(pcm16, dtype="<i2")
    loud = np.flatnonzero(np.abs(samples.astype(np.int32)) > SILENCE_LEVEL)
    if loud.size == 0:
        return pcm16
    keep = int(IN_RATE * KEEP_SECS)
    start, end = max(0, loud[0] - keep), min(samples.size, loud[-1] + keep)
    return bytes(samples[start:end].tobytes())


def caller_audio(
    utterance_id: str, text: str, language: str, audio_dir: Path, synth: Synth
) -> bytes:
    """The caller's audio for one utterance: a real recording if present, else synthesised."""
    recorded = audio_dir / "recorded" / f"{utterance_id}.wav"
    if recorded.exists():
        return trim_silence(read_wav(recorded))
    digest = hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:8]
    cached = audio_dir / "synthetic" / f"{utterance_id}-{digest}.wav"  # re-made if text changes
    if not cached.exists():
        write_wav(cached, synth.urdu(text) if language == "ur" else synth.english(text))
    return trim_silence(read_wav(cached))
