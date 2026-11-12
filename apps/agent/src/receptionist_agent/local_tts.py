"""Local Piper voice, spoken when a cloud TTS (Azure) fails (DECISIONS D37).

Piper runs on the CPU much faster than real time, needs no key, and its models are already on
disk for the default setup. A voice loads on first use, in a thread, and is then shared by every
call in the process.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any, Protocol

_voices: dict[str, Any] = {}
_voices_lock = threading.Lock()


class LocalVoice(Protocol):
    async def sample_rate(self) -> int: ...
    def speak(self, text: str) -> AsyncIterator[bytes]:
        """16-bit mono PCM chunks for `text`."""
        ...


def _load(name: str) -> Any:
    with _voices_lock:
        if name not in _voices:
            from pipecat.services.piper.tts import PIPER_CACHE_DIR
            from piper import PiperVoice
            from piper.download_voices import download_voice

            path = PIPER_CACHE_DIR / f"{name}.onnx"
            if not path.exists():
                PIPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                download_voice(name, PIPER_CACHE_DIR)
            _voices[name] = PiperVoice.load(path)
        return _voices[name]


class PiperVoiceFallback:
    def __init__(self, voice: str) -> None:
        self.voice = voice

    async def sample_rate(self) -> int:
        voice = await asyncio.to_thread(_load, self.voice)
        return int(voice.config.sample_rate)

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        voice = await asyncio.to_thread(_load, self.voice)
        chunks: Iterator[Any] = iter(voice.synthesize(text))

        def step() -> bytes | None:
            item = next(chunks, None)
            return None if item is None else bytes(item.audio_int16_bytes)

        while (audio := await asyncio.to_thread(step)) is not None:
            yield audio


__all__ = ["LocalVoice", "PiperVoiceFallback"]
