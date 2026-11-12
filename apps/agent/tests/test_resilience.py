"""Phase 9 failure handling (DECISIONS D37): every provider failing still leaves the caller a
way through. No network, no models: fakes play the providers."""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.azure.tts import AzureTTSService

from receptionist_agent import degraded
from receptionist_agent.azure_tts import QuickStopAzureTTSService
from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.text_session import TextSession
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_agent.groq_pool import PooledGroqLLMService
from receptionist_agent.groq_stt import AutoLanguageGroqSTTService
from receptionist_agent.llm_fallback import (
    GROQ_CHAT_URL,
    BudgetBook,
    FallbackTransport,
    gemini_target,
    groq_target,
)
from receptionist_agent.local_stt import wav_to_float
from receptionist_agent.never_silent import SORRY, NeverSilentGuard
from receptionist_shared.tenant import load_tenant_by_slug

BIG = groq_target("openai/gpt-oss-120b", "gsk_big", reasoning_effort="low")
GEMINI = gemini_target("gemini-3.5-flash-lite", "AIza_test")
TENANTS = Path(__file__).parents[3] / "tenants"


def body(*messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "m",
        "stream": True,
        "messages": [{"role": "system", "content": "x"}, *messages],
    }


def user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": text}


def events(raw: bytes) -> list[dict[str, Any]]:
    out = []
    for line in raw.decode().splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


# ---------------------------------------------------------------- the scripted message-taker


def test_first_failed_turn_asks_for_a_message_in_the_callers_language() -> None:
    assert degraded.plan(body(user("I'd like a haircut tomorrow"))) == (degraded.ASK["en"], [])
    assert degraded.plan(body(user("کل بال کٹوانے ہیں")))[0] == degraded.ASK["ur"]


def test_after_the_ask_the_callers_words_become_a_message() -> None:
    asked = {"role": "assistant", "content": degraded.ASK["en"]}
    text, calls = degraded.plan(
        body(user("haircut?"), asked, user("Sara Ahmed, 0321 555 0101, please call me back"))
    )
    assert text is None
    (call,) = calls
    assert call["function"]["name"] == "take_message"
    args = json.loads(call["function"]["arguments"])
    assert args == {
        "message": "haircut? / Sara Ahmed, 0321 555 0101, please call me back",
        "phone": "+923215550101",
    }


def test_a_saved_message_gets_thanks_and_a_hang_up_an_unsaved_one_an_honest_apology() -> None:
    asked = {"role": "assistant", "content": degraded.ASK["en"]}
    call = {"id": "degraded_msg_1", "type": "function", "function": {"name": "take_message"}}
    tool_turn = {"role": "assistant", "content": None, "tool_calls": [call]}

    def result(ok: bool) -> dict[str, Any]:
        content = json.dumps({"ok": ok})
        return {"role": "tool", "tool_call_id": "degraded_msg_1", "content": content}

    text, calls = degraded.plan(body(user("hi"), asked, user("Sara"), tool_turn, result(True)))
    assert text == degraded.THANKS["en"]
    assert [c["function"]["name"] for c in calls] == ["end_call"]
    text, _ = degraded.plan(body(user("hi"), asked, user("Sara"), tool_turn, result(False)))
    assert text == degraded.NOT_SAVED["en"]


def read_back_turns() -> list[dict[str, Any]]:
    args = {"service": "Haircut", "start": "2026-11-19T16:00:00+05:00", "caller_confirmed": False}
    call = {
        "id": "call_7",
        "type": "function",
        "function": {"name": "create_booking", "arguments": json.dumps(args)},
    }
    result = {"ok": True, "booked": False, "read_back": {"when": "Thursday 19 November, 4:00 PM"}}
    return [
        user("Haircut Thursday 4 pm, Sara, 0321 5550101"),
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_7", "content": json.dumps(result)},
    ]


def test_a_plain_yes_to_a_read_back_is_committed_without_the_llm() -> None:
    """Seen live: the LLM went down between the read-back and the caller's 'Yes, please'."""
    text, calls = degraded.plan(body(*read_back_turns(), user("Yes, please.")))
    assert text is None
    (call,) = calls
    assert call["function"]["name"] == "create_booking"
    assert json.loads(call["function"]["arguments"])["caller_confirmed"] is True
    assert call["id"].startswith(degraded.CONFIRM_CALL_PREFIX)


def test_anything_but_a_plain_yes_goes_to_a_message() -> None:
    for answer in ("No, make it 5 pm", "Yes but on Friday", "What time was that?"):
        text, calls = degraded.plan(body(*read_back_turns(), user(answer)))
        assert (text, calls) == (degraded.ASK["en"], []), answer


def test_the_committed_booking_is_confirmed_from_the_tool_result() -> None:
    turns = [*read_back_turns(), user("جی ہاں")]
    _, (call,) = degraded.plan(body(*turns))
    done = {
        "ok": True,
        "booked": True,
        "booking_id": "AB12",
        "service": "Haircut",
        "when": "Thu 4 PM",
    }
    turns += [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(done)},
    ]
    text, calls = degraded.plan(body(*turns))
    assert text is not None and "A B 1 2" in text and "Haircut" in text
    assert "خدا حافظ" in text  # the caller said yes in Urdu
    assert [c["function"]["name"] for c in calls] == ["end_call"]
    failed = {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"ok": False})}
    assert degraded.plan(body(*turns[:-1], failed))[0] == degraded.ASK["ur"]


def test_the_message_carries_the_whole_request() -> None:
    asked = {"role": "assistant", "content": degraded.ASK["en"]}
    _, (call,) = degraded.plan(body(user("A facial on Friday?"), asked, user("Sara, 0321 5550101")))
    message = json.loads(call["function"]["arguments"])["message"]
    assert message == "A facial on Friday? / Sara, 0321 5550101"


def test_nothing_more_once_the_script_has_hung_up() -> None:
    end = {"id": "degraded_end_1", "type": "function", "function": {"name": "end_call"}}
    turns = [
        user("A facial on Friday"),
        {"role": "assistant", "content": degraded.ASK["en"]},
        user("Sara, 0321 5550101"),
        {"role": "assistant", "content": degraded.THANKS["en"], "tool_calls": [end]},
        {"role": "tool", "tool_call_id": "degraded_end_1", "content": '{"ok": true}'},
        user("Please call me back."),  # arrived while the goodbye played
    ]
    assert degraded.plan(body(*turns)) == (None, [])


def test_an_interrupted_ask_still_counts() -> None:
    cut = {"role": "assistant", "content": degraded.ASK["en"][:40]}
    _, calls = degraded.plan(body(user("hi"), cut, user("Call me back please")))
    assert calls and calls[0]["function"]["name"] == "take_message"


@pytest.mark.parametrize(
    ("text", "phone"),
    [
        ("my number is 0321-5550101", "+923215550101"),
        ("\u06f0\u06f3\u06f2\u06f1 \u06f5\u06f5\u06f5 \u06f0\u06f1\u06f0\u06f1", "+923215550101"),
        ("call me at 4 pm", None),
        ("and my number is 0-3-2-1-5,550,101.", "+923215550101"),  # Whisper, seen live
    ],
)
def test_find_phone(text: str, phone: str | None) -> None:
    assert degraded.find_phone(text) == phone


def test_reply_is_a_valid_openai_stream() -> None:
    chunks = events(degraded.degraded_reply(body(user("hello"))))
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    said = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert said == degraded.ASK["en"]


# ---------------------------------------------------------------- the LLM transport


def transport(handler: Any, targets: list[Any], **kw: Any) -> httpx2.AsyncClient:
    t = FallbackTransport(
        targets, inner=httpx2.MockTransport(handler), budgets=BudgetBook(), degrade=True, **kw
    )
    return httpx2.AsyncClient(transport=t)


async def test_a_rejected_key_is_not_asked_again_on_the_next_turn() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        model = json.loads(request.content)["model"]
        seen.append(model)
        if "gpt-oss" in model:
            return httpx2.Response(401, json={"error": {"message": "invalid api key"}})
        return httpx2.Response(200, json={"served": model})

    async with transport(handler, [BIG, GEMINI]) as client:
        await client.post(GROQ_CHAT_URL, json=body(user("hi")))
        await client.post(GROQ_CHAT_URL, json=body(user("again")))
    assert seen == ["openai/gpt-oss-120b", "gemini-3.5-flash-lite", "gemini-3.5-flash-lite"]


async def test_rejected_keys_end_in_the_message_taker_not_an_error() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content)["model"])
        return httpx2.Response(401, json={"error": {"message": "invalid api key"}})

    async with transport(handler, [BIG, GEMINI]) as client:
        response = await client.post(GROQ_CHAT_URL, json=body(user("hi")))
        assert response.status_code == 200
        assert degraded.ASK["en"] in response.text
        assert seen == ["openai/gpt-oss-120b", "gemini-3.5-flash-lite"]  # a 401 isn't retried


async def test_a_slow_target_is_abandoned_and_the_next_one_answers() -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if "groq" in str(request.url):
            await asyncio.sleep(1)
        return httpx2.Response(200, json={"served": json.loads(request.content)["model"]})

    async with transport(handler, [BIG, GEMINI], timeout_secs=0.05) as client:
        response = await client.post(GROQ_CHAT_URL, json=body(user("hi")))
    assert response.json() == {"served": "gemini-3.5-flash-lite"}


async def test_a_passing_failure_is_retried_once() -> None:
    attempts = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx2.ConnectError("blip")
        return httpx2.Response(200, json={"ok": True})

    async with transport(handler, [BIG]) as client:
        assert (await client.post(GROQ_CHAT_URL, json=body(user("hi")))).json() == {"ok": True}
    assert attempts == 2


async def test_no_targets_at_all_still_answers() -> None:
    async with transport(lambda r: httpx2.Response(500), []) as client:
        response = await client.post(GROQ_CHAT_URL, json=body(user("hi")))
    assert degraded.ASK["en"] in response.text


async def test_whole_call_without_any_llm_takes_a_message() -> None:
    """Through the real OpenAI SDK, Pipecat and our tools: the caller is asked, the message is
    saved for the owner, and the call ends politely."""
    tenant = load_tenant_by_slug("demo-salon", TENANTS)
    now = datetime(2026, 11, 16, 9, 0, tzinfo=tenant.tz)
    llm = PooledGroqLLMService(
        targets=[BIG, GEMINI],
        network=httpx2.MockTransport(lambda r: httpx2.Response(503, json={"error": {}})),
        api_key="gsk_big",
        settings=PooledGroqLLMService.Settings(model=BIG.model, system_instruction="test"),
    )
    backend = InMemoryBackend(clock=lambda: now)
    state = CallState()
    tools = ReceptionistTools(tenant, state, backend, call_id="outage")
    async with TextSession(tenant=tenant, llm=llm, tools=tools, state=state) as session:
        first = await session.send("Can I book a haircut on Wednesday?")
        assert first.text == degraded.ASK["en"]
        second = await session.send("Sara Ahmed, 0321 5550101. Please call me about a haircut.")
    assert [c.name for c in second.tool_calls] == ["take_message", "end_call"]
    assert degraded.THANKS["en"] in second.text
    assert second.ended
    (saved,) = backend.messages
    assert saved["phone"] == "+923215550101"
    assert "haircut" in str(saved["message"])


# ---------------------------------------------------------------- STT


def wav(seconds: float = 0.2, rate: int = 16_000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(rate * seconds))
    return out.getvalue()


class FakeLocal:
    def __init__(self) -> None:
        self.calls = 0

    async def transcribe(self, wav: bytes, language: str | None) -> tuple[str, str | None]:
        self.calls += 1
        return "local words", "ur"


class FlakyGroq:
    """Stands in for the OpenAI client's audio.transcriptions."""

    def __init__(self, failures: int, delay: float = 0.0) -> None:
        self.failures = failures
        self.delay = delay
        self.calls = 0
        self.audio = self
        self.transcriptions = self

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.calls <= self.failures:
            raise RuntimeError("groq is down")

        class Response:
            text = "groq words"

        return Response()


def stt(groq: FlakyGroq, local: FakeLocal | None, **kw: Any) -> AutoLanguageGroqSTTService:
    service = AutoLanguageGroqSTTService(
        api_key="gsk_test",
        local_fallback=local,
        settings=AutoLanguageGroqSTTService.Settings(model="whisper-large-v3-turbo", language=None),
        **kw,
    )
    service._client = groq  # type: ignore[assignment]
    return service


async def transcripts(service: AutoLanguageGroqSTTService) -> list[Frame]:
    return [f async for f in service.run_stt(wav())]


async def test_stt_retries_once_before_falling_back() -> None:
    groq, local = FlakyGroq(failures=1), FakeLocal()
    (frame,) = await transcripts(stt(groq, local))
    assert isinstance(frame, TranscriptionFrame) and frame.text == "groq words"
    assert (groq.calls, local.calls) == (2, 0)


async def test_stt_falls_back_to_local_whisper_and_then_skips_groq_for_a_while() -> None:
    groq, local = FlakyGroq(failures=99), FakeLocal()
    service = stt(groq, local)
    for _ in range(2):
        (frame,) = await transcripts(service)
        assert isinstance(frame, TranscriptionFrame) and frame.text == "local words"
    assert groq.calls == 4  # two utterances, each tried and retried
    await transcripts(service)
    assert groq.calls == 4  # tripped: the third goes straight to the local model
    assert local.calls == 3


async def test_a_slow_groq_counts_as_a_failure() -> None:
    groq, local = FlakyGroq(failures=0, delay=1.0), FakeLocal()
    (frame,) = await transcripts(stt(groq, local, timeout_secs=0.05))
    assert isinstance(frame, TranscriptionFrame) and frame.text == "local words"


async def test_without_a_local_model_the_error_is_reported_for_the_guard() -> None:
    (frame,) = await transcripts(stt(FlakyGroq(failures=99), None))
    assert isinstance(frame, ErrorFrame)


def test_wav_is_resampled_to_whisper_rate() -> None:
    assert len(wav_to_float(wav(seconds=1.0, rate=8_000))) == 16_000


# ---------------------------------------------------------------- TTS


class FakeVoice:
    async def sample_rate(self) -> int:
        return 16_000

    async def speak(self, text: str) -> AsyncIterator[bytes]:
        yield b"\x01\x00" * 160


def azure_that(
    monkeypatch: pytest.MonkeyPatch, frames: list[Frame | None]
) -> QuickStopAzureTTSService:
    """A None in `frames` means "hang" (no audio arrives)."""

    async def fake_run_tts(self: Any, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        for frame in frames:
            if frame is None:
                await asyncio.sleep(10)
            else:
                yield frame

    monkeypatch.setattr(AzureTTSService, "run_tts", fake_run_tts)
    tts = QuickStopAzureTTSService(
        local_fallback=FakeVoice(), first_audio_secs=0.05, api_key="k", region="uaenorth"
    )
    tts._sample_rate = 16_000
    return tts


async def spoken(tts: QuickStopAzureTTSService) -> list[Frame]:
    return [f async for f in tts.run_tts("Hello there", "ctx")]


async def test_azure_error_is_spoken_by_the_local_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = azure_that(monkeypatch, [ErrorFrame(error="401 invalid key")])
    frames = await spoken(tts)
    assert frames and all(isinstance(f, TTSAudioRawFrame) for f in frames)
    assert tts.served_locally == 1


async def test_azure_silence_is_spoken_by_the_local_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = azure_that(monkeypatch, [None])
    assert any(isinstance(f, TTSAudioRawFrame) for f in await spoken(tts))


async def test_azure_is_skipped_after_failing_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    tries = 0

    async def failing(self: Any, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        nonlocal tries
        tries += 1
        yield ErrorFrame(error="401 invalid key")

    tts = azure_that(monkeypatch, [])
    monkeypatch.setattr(AzureTTSService, "run_tts", failing)
    for _ in range(3):
        assert any(isinstance(f, TTSAudioRawFrame) for f in await spoken(tts))
    assert tries == 2  # the third sentence went straight to the local voice
    assert tts.served_locally == 3


async def test_healthy_azure_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    audio = TTSAudioRawFrame(b"\x00\x00" * 10, 16_000, 1, context_id="ctx")
    tts = azure_that(monkeypatch, [audio, audio])
    assert await spoken(tts) == [audio, audio]
    assert tts.served_locally == 0


# ---------------------------------------------------------------- the last safety net


class Pushed:
    def __init__(self, frame: Frame, source: Any) -> None:
        self.frame = frame
        self.source = source


async def test_guard_apologises_once_per_gap_and_only_for_providers() -> None:
    said: list[str] = []
    now = [0.0]

    async def speak(text: str) -> None:
        said.append(text)

    guard = NeverSilentGuard(speak, lambda: "ur", clock=lambda: now[0])
    service = stt(FlakyGroq(0), None)
    error = ErrorFrame(error="boom", processor=service)
    await guard.on_push_frame(Pushed(error, service))  # type: ignore[arg-type]
    await guard.on_push_frame(Pushed(error, object()))  # type: ignore[arg-type]  # same frame
    await guard.on_push_frame(Pushed(ErrorFrame("again", processor=service), service))  # type: ignore[arg-type]
    assert said == [SORRY["ur"]]
    now[0] = 60.0
    await guard.on_push_frame(Pushed(ErrorFrame("later", processor=service), service))  # type: ignore[arg-type]
    await guard.on_push_frame(Pushed(ErrorFrame("not a provider"), object()))  # type: ignore[arg-type]
    assert said == [SORRY["ur"], SORRY["ur"]]
