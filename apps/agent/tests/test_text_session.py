"""Full conversations through the real Pipecat context/tool machinery, with a scripted LLM."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.text_session import TextSession
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_agent.testing import (
    CallTools,
    Say,
    ScriptedLLM,
    Step,
    last_tool_result,
    last_user_text,
)
from receptionist_shared.tenant import TenantConfig


def booking_script(messages: Sequence[dict[str, Any]]) -> Step:
    """A well-behaved receptionist: check, read back, confirm, goodbye."""
    user = last_user_text(messages).lower()
    tool = last_tool_result(messages)
    last = messages[-1]
    if last.get("role") == "tool":
        result = json.loads(tool or "{}")
        if "options" in result:
            first = result["options"][0]
            return Say(f"I have {first['spoken']}. Can I have your name and number?")
        if result.get("booked") is False:
            rb = result["read_back"]
            return Say(
                f"{rb['service']} on {rb['when']} for {rb['name']}, {rb['phone']}. "
                "Shall I book that?"
            )
        if result.get("booked") is True:
            return Say(f"Done, your booking number is {result['booking_id']}.")
        return Say("Okay.")
    if "haircut" in user:
        return CallTools([("check_availability", {"service": "Haircut", "date": "2026-11-18"})])
    if "sara" in user:
        start = next(
            json.loads(m["content"])["options"][0]["start"]
            for m in reversed(messages)
            if m.get("role") == "tool" and "options" in json.loads(m["content"])
        )
        args = {"service": "Haircut", "start": start, "caller_name": "Sara", "phone": "03001234567"}
        return CallTools([("create_booking", args | {"caller_confirmed": False})])
    if user.startswith("yes"):
        prior = next(
            m for m in reversed(messages) if m.get("role") == "assistant" and m.get("tool_calls")
        )
        args = json.loads(prior["tool_calls"][0]["function"]["arguments"])
        return CallTools([("create_booking", args | {"caller_confirmed": True})])
    if "bye" in user:
        return CallTools([("end_call", {"reason": "caller said goodbye"})], text="Goodbye!")
    return Say("How can I help?")


async def test_scripted_booking_conversation(salon: TenantConfig, monday_9am: datetime) -> None:
    backend = InMemoryBackend(clock=lambda: monday_9am)
    state = CallState()
    tools = ReceptionistTools(salon, state, backend, call_id="t-1", clock=lambda: monday_9am)
    llm = ScriptedLLM(booking_script)

    async with TextSession(tenant=salon, llm=llm, tools=tools, state=state) as session:
        assert "Noor Beauty Salon" in session.greeting

        r1 = await session.send("Hi, I'd like a haircut on Wednesday")
        assert [c.name for c in r1.tool_calls] == ["check_availability"]
        assert r1.text.startswith("I have Wednesday 18 November")

        r2 = await session.send("Sara, 0300 1234567")
        assert [c.name for c in r2.tool_calls] == ["create_booking"]
        assert "Shall I book that?" in r2.text
        assert "0300 123 4567" in r2.text
        assert backend.bookings == {}  # nothing written before the caller says yes

        r3 = await session.send("yes please")
        assert r3.tool_calls[0].arguments["caller_confirmed"] is True
        assert "booking number" in r3.text
        assert len(backend.bookings) == 1

        r4 = await session.send("thanks, bye")
        assert r4.text == "Goodbye!"
        assert r4.ended

    assert state.booking_id is not None
    # The greeting and every turn are in the context the LLM saw.
    first_context = llm.seen[0]
    assert first_context[0]["role"] == "assistant"
    assert "Noor Beauty Salon" in first_context[0]["content"]


async def test_tool_error_is_shown_to_the_llm(salon: TenantConfig, monday_9am: datetime) -> None:
    def script(messages: Sequence[dict[str, Any]]) -> Step:
        if messages[-1].get("role") == "tool":
            return Say("error seen" if '"ok": false' in (last_tool_result(messages) or "") else "?")
        return CallTools([("check_availability", {"service": "massage", "date": "2026-11-18"})])

    state = CallState()
    tools = ReceptionistTools(
        salon, state, InMemoryBackend(), call_id="t-2", clock=lambda: monday_9am
    )
    async with TextSession(tenant=salon, llm=ScriptedLLM(script), tools=tools, state=state) as s:
        reply = await s.send("can I get a massage?")
    assert reply.text == "error seen"


def eager_script(messages: Sequence[dict[str, Any]]) -> Step:
    """Misbehaves like gpt-oss-120b did live: confirms right after its own read-back."""
    last = messages[-1]
    result = json.loads(last_tool_result(messages) or "{}") if last.get("role") == "tool" else {}
    if "options" in result:
        start = result["options"][0]["start"]
        args = {"service": "Haircut", "start": start, "caller_name": "Sara", "phone": "03001234567"}
        return CallTools([("create_booking", args | {"caller_confirmed": False})])
    if result.get("booked") is False:
        prior = next(
            m for m in reversed(messages) if m.get("role") == "assistant" and m.get("tool_calls")
        )
        args = json.loads(prior["tool_calls"][0]["function"]["arguments"])
        return CallTools([("create_booking", args | {"caller_confirmed": True})])
    if last.get("role") == "tool":
        return Say("refused" if result.get("ok") is False else "booked")
    return CallTools([("check_availability", {"service": "Haircut", "date": "2026-11-18"})])


async def test_llm_cannot_confirm_without_hearing_the_caller(
    salon: TenantConfig, monday_9am: datetime
) -> None:
    """Through the real Pipecat glue: caller turns are counted from the LLM context."""
    backend = InMemoryBackend(clock=lambda: monday_9am)
    state = CallState()
    tools = ReceptionistTools(salon, state, backend, call_id="t-3", clock=lambda: monday_9am)
    async with TextSession(
        tenant=salon, llm=ScriptedLLM(eager_script), tools=tools, state=state
    ) as session:
        reply = await session.send("Haircut Wednesday please, I'm Sara, 0300 1234567")
    names = [c.name for c in reply.tool_calls]
    assert names == ["check_availability", "create_booking", "create_booking"]
    assert "not answered yet" in reply.tool_calls[-1].result["error"]
    assert reply.text == "refused"
    assert backend.bookings == {}
