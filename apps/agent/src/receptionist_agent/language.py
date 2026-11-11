"""Urdu / English handling around the TTS (see DECISIONS D10).

Kokoro has no Urdu voice, Piper does. So each bot reply is spoken by the voice that matches
its script: Urdu script -> Piper `ur_PK`, otherwise -> Kokoro. Two processors do this:

- `CallerLanguageTracker` (after STT) notes the caller's language from each transcript, so
  tools can answer in it (e.g. Urdu FAQ answers).
- `ReplyLanguageRouter` (before the TTS switcher) holds a reply's first frames until the first
  letter shows its script, switches the TTS if needed, then releases them in order. The switch
  happens before any text reaches a TTS, so a reply is never split across two voices.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Literal

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    ManuallySwitchServiceFrame,
    SystemFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

Script = Literal["ur", "hi", "en"]


def detect_script(text: str) -> Script | None:
    """Script of the first letter in `text`: Arabic-script -> ur, Devanagari -> hi, else en.

    None if there is no letter yet (only spaces, digits or punctuation).
    """
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if name.startswith("ARABIC"):
            return "ur"
        if name.startswith("DEVANAGARI"):
            return "hi"
        return "en"
    return None


def caller_language(text: str) -> str | None:
    """Language for replies: Urdu for Urdu or Hindi script (spoken Urdu often transcribes as
    Devanagari), English for Latin script."""
    script = detect_script(text)
    if script is None:
        return None
    return "ur" if script in ("ur", "hi") else "en"


class CallerLanguageTracker(FrameProcessor):
    """Calls `on_language` with "ur"/"en" for each final transcript. Passes every frame on."""

    def __init__(self, on_language: Callable[[str], None]) -> None:
        super().__init__()
        self._on_language = on_language

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and (lang := caller_language(frame.text)):
            self._on_language(lang)
        await self.push_frame(frame, direction)


class ReplyLanguageRouter(FrameProcessor):
    """Picks the TTS for each reply from its script; sits right before a `ServiceSwitcher`."""

    def __init__(self, *, english_tts: FrameProcessor, urdu_tts: FrameProcessor) -> None:
        super().__init__()
        self._voices = {"en": english_tts, "ur": urdu_tts}
        self._active = "en"
        self._held: list[tuple[Frame, FrameDirection]] | None = None  # None = not holding

    @property
    def active_language(self) -> str:
        return self._active

    async def _switch(self, language: str) -> None:
        if language != self._active:
            self._active = language
            await self.push_frame(ManuallySwitchServiceFrame(service=self._voices[language]))

    async def _release(self) -> None:
        held, self._held = self._held or [], None
        for frame, direction in held:
            await self.push_frame(frame, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            self._held = None  # the reply was interrupted: drop what it hadn't said
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TTSSpeakFrame):  # e.g. the greeting: whole text is known
            script = caller_language(frame.text)
            if script:
                await self._switch(script)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._held = [(frame, direction)]
            return

        if self._held is None or isinstance(frame, SystemFrame):
            await self.push_frame(frame, direction)
            return

        self._held.append((frame, direction))
        if isinstance(frame, LLMTextFrame) and (language := caller_language(frame.text)):
            await self._switch(language)
            await self._release()
        elif isinstance(frame, LLMFullResponseEndFrame):
            await self._release()  # a reply with no letters (e.g. only a tool call)
