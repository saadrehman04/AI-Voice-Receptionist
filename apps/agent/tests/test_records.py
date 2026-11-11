import asyncio
from typing import Any

from receptionist_agent.records import ApiCallRecorder, MemoryRecorder


def test_timing_attaches_to_the_next_agent_turn() -> None:
    r = MemoryRecorder("c1")
    r.agent_turn("Hello!", interrupted=False)
    r.caller_turn("I want a haircut")
    r.timing({"stages": {"stt": 200.0, "total": 850.0}})
    r.agent_turn("Sure, when?", interrupted=False)
    r.caller_turn("   ")  # silence/noise is not a turn
    r.agent_turn("Sorry, I", interrupted=True)
    assert [(t.idx, t.speaker.value) for t in r.turns] == [
        (0, "agent"),
        (1, "caller"),
        (2, "agent"),
        (3, "agent"),
    ]
    assert r.turns[0].timings is None
    assert r.turns[2].timings == {"stt": 200.0, "total": 850.0}
    assert r.turns[3].interrupted and r.turns[3].timings is None


async def test_api_recorder_sends_in_order_and_flushes_on_end() -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(path: str, body: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        sent.append((path, body))

    r = ApiCallRecorder("c2", send)
    await r.start()
    r.caller_turn("hi")
    r.tool_call(
        "lookup_business_info", {"question": "parking"}, {"ok": True}, ok=True, duration_ms=40
    )
    r.agent_turn("Yes, free parking.", interrupted=False)
    await r.end(language="en", recording_path=None)
    assert [p for p, _ in sent] == ["", "/c2/turns", "/c2/tool-calls", "/c2/turns", "/c2/end"]
    assert sent[0][1]["call_id"] == "c2"
    assert sent[-1][1]["language"] == "en"


async def test_api_recorder_retries_then_gives_up_without_raising() -> None:
    attempts: dict[str, int] = {}

    async def flaky(path: str, body: dict[str, Any]) -> None:
        attempts[path] = attempts.get(path, 0) + 1
        if path.endswith("/turns"):
            raise ConnectionError("api down")
        if path == "" and attempts[path] < 2:
            raise ConnectionError("blip")

    r = ApiCallRecorder("c3", flaky)
    await r.start()
    r.caller_turn("hello?")
    await r.end(language=None, recording_path=None)  # must not raise
    assert attempts[""] == 2  # retried once, then succeeded
    assert attempts["/c3/turns"] == 3  # gave up after 3 tries
    assert attempts["/c3/end"] == 1
