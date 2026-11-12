from datetime import datetime
from typing import Any

import pytest
from pipecat.processors.aggregators.llm_context import LLMContext

from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.state import CallState, Intent
from receptionist_agent.brain.tools import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    ReceptionistTools,
    count_caller_turns,
    days_in_play,
    goodbye_line,
    last_caller_language,
    weekdays_named,
)
from receptionist_shared.tenant import TenantConfig

WED = "2026-11-18"
PHONE = "0300 1234567"
# read-backs spell the digits out (D35)
SPELLED_PHONE = "zero three zero zero, one two three, four five six seven"


@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


def make_tools(
    tenant: TenantConfig, backend: InMemoryBackend, now: datetime, call_id: str = "call-1"
) -> ReceptionistTools:
    backend.clock = lambda: now
    return ReceptionistTools(tenant, CallState(), backend, call_id=call_id, clock=lambda: now)


async def offer(tools: ReceptionistTools, preferred: str = "14:00") -> dict[str, Any]:
    result = await tools.run(
        "check_availability", {"service": "haircut", "date": WED, "preferred_time": preferred}
    )
    assert result["ok"] and result["available"], result
    return result


def booking_args(start: str, **over: Any) -> dict[str, Any]:
    args = {
        "service": "Haircut",
        "start": start,
        "caller_name": "Sara",
        "phone": PHONE,
        "caller_confirmed": False,
    }
    return args | over


def caller_replies(tools: ReceptionistTools) -> None:
    """Simulate the caller speaking (in a real call this is counted from the LLM context)."""
    tools.state.heard_caller(tools.state.caller_turns + 1)


async def book(tools: ReceptionistTools, start: str) -> dict[str, Any]:
    await tools.run("create_booking", booking_args(start))
    caller_replies(tools)
    return await tools.run("create_booking", booking_args(start, caller_confirmed=True))


def test_every_guide_tool_is_defined() -> None:
    assert set(TOOL_NAMES) == {
        "check_availability",
        "create_booking",
        "reschedule_booking",
        "cancel_booking",
        "lookup_business_info",
        "take_message",
        "transfer_to_human",
        "end_call",
    }
    for d in TOOL_DEFINITIONS:
        assert set(d["required"]) <= set(d["properties"]), d["name"]


async def test_availability_offers_three_near_preference(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    result = await offer(tools)
    starts = [o["start"] for o in result["options"]]
    assert len(starts) == 3
    assert f"{WED}T14:00:00+05:00" in starts
    assert result["options"][0]["spoken"].startswith("Wednesday 18 November")
    assert tools.state.intent is Intent.BOOK
    assert tools.state.service == "Haircut"


async def test_closed_day_suggests_next_open_day(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    result = await tools.run("check_availability", {"service": "Facial", "date": "2026-11-17"})
    assert result["available"] is False
    assert result["date"] == WED
    assert "Nothing free on Tuesday" in result["note"]


async def test_unknown_service_lists_real_ones(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    result = await tools.run("check_availability", {"service": "massage", "date": WED})
    assert result["ok"] is False
    assert "Haircut" in result["available_services"]


async def test_bad_date_is_an_error_not_a_crash(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    assert (await tools.run("check_availability", {"service": "Haircut", "date": "Wed"}))[
        "ok"
    ] is False


async def test_booking_needs_readback_then_yes(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][1]["start"]

    first = await tools.run("create_booking", booking_args(start))
    assert first["booked"] is False
    assert first["read_back"]["phone"] == SPELLED_PHONE
    assert first["read_back"]["name"] == "Sara"
    assert backend.bookings == {}

    caller_replies(tools)
    second = await tools.run("create_booking", booking_args(start, caller_confirmed=True))
    assert second["booked"] is True
    assert tools.state.booking_id == second["booking_id"]
    (stored,) = backend.bookings.values()
    assert stored.phone == "+923001234567"
    assert stored.start.isoformat() == start


async def test_confirmed_without_readback_is_refused(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    result = await tools.run("create_booking", booking_args(start, caller_confirmed=True))
    assert result["ok"] is False
    assert "read the details back first" in result["error"]
    assert backend.bookings == {}


async def test_confirm_in_the_same_turn_as_the_readback_is_refused(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    """Found live with gpt-oss-120b: it read back and confirmed in one go, inventing the yes."""
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    await tools.run("create_booking", booking_args(start), caller_turns=3)
    same_turn = await tools.run(
        "create_booking", booking_args(start, caller_confirmed=True), caller_turns=3
    )
    assert same_turn["ok"] is False
    assert "not answered yet" in same_turn["error"]
    assert backend.bookings == {}
    # Once the caller has spoken, the same commit goes through.
    done = await tools.run(
        "create_booking", booking_args(start, caller_confirmed=True), caller_turns=4
    )
    assert done["booked"] is True


async def test_cancel_also_needs_a_caller_turn(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    await book(tools, (await offer(tools))["options"][0]["start"])
    other = make_tools(salon, backend, monday_9am, call_id="call-2")
    await other.run("cancel_booking", {"phone": PHONE, "caller_confirmed": False}, caller_turns=1)
    refused = await other.run(
        "cancel_booking", {"phone": PHONE, "caller_confirmed": True}, caller_turns=1
    )
    assert refused["ok"] is False
    assert "not answered yet" in refused["error"]


async def test_requested_time_that_is_free_is_called_out(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    exact = await offer(tools, preferred="14:00")
    assert exact["requested_time_free"] is True
    assert "Wednesday 18 November, 2:00 PM" in exact["next_step"]
    vague = await tools.run("check_availability", {"service": "haircut", "date": WED})
    assert "requested_time_free" not in vague


async def test_readback_step_follows_the_callers_language(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    """Seen live: models copied the English read-back out to Urdu callers."""
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    english = await tools.run("create_booking", booking_args(start), caller_language="en")
    assert "Urdu" not in english["next_step"]
    urdu = await tools.run("create_booking", booking_args(start), caller_language="ur")
    assert "Urdu script" in urdu["next_step"]
    # the tool spells the number out digit by digit; the LLM only copies it
    assert urdu["read_back"]["phone"] == "صفر تین صفر صفر، ایک دو تین، چار پانچ چھ سات"
    assert (
        english["read_back"]["phone"] == "zero three zero zero, one two three, four five six seven"
    )
    assert tools.state.language == "ur"


def test_caller_turns_and_language_come_from_the_llm_context() -> None:
    context = LLMContext()
    assert count_caller_turns(None) is None
    assert last_caller_language(context) is None
    context.add_message({"role": "assistant", "content": "Hello, Noor Beauty Salon."})
    context.add_message({"role": "user", "content": "Hi, a haircut please"})
    context.add_message({"role": "user", "content": "جی ہاں، ٹھیک ہے"})
    context.add_message({"role": "user", "content": "0300 1234567"})  # digits only: no language
    assert count_caller_turns(context) == 3
    assert last_caller_language(context) == "ur"


async def test_changed_details_need_a_new_readback(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    await tools.run("create_booking", booking_args(start))
    caller_replies(tools)
    result = await tools.run(
        "create_booking", booking_args(start, caller_name="Sara Khan", caller_confirmed=True)
    )
    assert result["ok"] is False
    assert "differ" in result["error"]
    assert result["read_back"]["name"] == "Sara Khan"
    assert backend.bookings == {}


async def test_unoffered_time_cannot_be_booked(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    await offer(tools)
    result = await tools.run("create_booking", booking_args(f"{WED}T11:05:00+05:00"))
    assert result["ok"] is False
    assert "not offered" in result["error"]


async def test_bad_phone_asks_to_repeat(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    result = await tools.run("create_booking", booking_args(start, phone="0300 12"))
    assert "repeat" in result["error"]


async def test_retried_commit_is_idempotent(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    first = await book(tools, start)
    # The LLM repeats the commit: the read-back was used up, so nothing new is written.
    caller_replies(tools)
    again = await tools.run("create_booking", booking_args(start, caller_confirmed=True))
    assert again["ok"] is False
    assert len(backend.bookings) == 1
    # And at the backend, the same idempotency key returns the same booking.
    booking = backend.bookings[first["booking_id"]]
    key = next(k for k, v in backend.by_key.items() if v == booking.id)
    repeat = await backend.create_booking(
        salon,
        service="Haircut",
        start=booking.start,
        caller_name="Sara",
        phone=booking.phone,
        call_id="call-1",
        idempotency_key=key,
    )
    assert repeat.id == booking.id
    assert len(backend.bookings) == 1


async def test_held_slot_is_hidden_from_other_callers(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    a = make_tools(salon, backend, monday_9am, call_id="call-a")
    b = make_tools(salon, backend, monday_9am, call_id="call-b")
    start = (await offer(a))["options"][1]["start"]
    held = await a.run("create_booking", booking_args(start))  # read-back -> hold
    assert held["booked"] is False
    offered_to_b = [o["start"] for o in (await offer(b))["options"]]
    assert start not in offered_to_b


async def test_slot_taken_by_another_caller(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    a = make_tools(salon, backend, monday_9am, call_id="call-a")
    b = make_tools(salon, backend, monday_9am, call_id="call-b")
    start = (await offer(a))["options"][1]["start"]
    await offer(b)  # both callers were offered the same slot
    assert (await book(a, start))["booked"] is True
    result = await b.run("create_booking", booking_args(start))  # B's read-back
    assert result["ok"] is False
    assert "another caller" in result["error"]
    assert result["options"]
    assert start not in [o["start"] for o in result["options"]]
    assert len(backend.bookings) == 1


async def test_reschedule_flow(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    booking_id = (await book(tools, start))["booking_id"]

    caller2 = make_tools(salon, backend, monday_9am, call_id="call-2")
    new_start = (await offer(caller2, preferred="17:00"))["options"][0]["start"]
    args = {"phone": "+923001234567", "new_start": new_start, "caller_confirmed": False}
    first = await caller2.run("reschedule_booking", args)
    assert first["rescheduled"] is False
    assert first["read_back"]["to"].startswith("Wednesday 18 November")
    caller_replies(caller2)
    done = await caller2.run("reschedule_booking", args | {"caller_confirmed": True})
    assert done["rescheduled"] is True
    assert backend.bookings[booking_id].start.isoformat() == new_start


async def test_cancel_flow_and_policy(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    booking_id = (await book(tools, start))["booking_id"]

    caller2 = make_tools(salon, backend, monday_9am, call_id="call-2")
    first = await caller2.run("cancel_booking", {"phone": PHONE, "caller_confirmed": False})
    assert "4 hours" in first["policy"]
    caller_replies(caller2)
    done = await caller2.run("cancel_booking", {"phone": PHONE, "caller_confirmed": True})
    assert done["cancelled"] is True
    assert backend.bookings[booking_id].status == "cancelled"
    assert await backend.find_bookings(salon, "+923001234567") == []


async def test_cancel_with_no_booking(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    result = await tools.run("cancel_booking", {"phone": PHONE, "caller_confirmed": False})
    assert result["ok"] is False
    assert "no upcoming booking" in result["error"]


async def test_lookup_message_handoff_end(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    info = await tools.run("lookup_business_info", {"question": "do you sell gift vouchers?"})
    assert info["found"] is False

    msg = await tools.run("take_message", {"message": "Please call me about bridal packages"})
    assert msg["ok"] and "phone number" in msg["hint"]
    assert backend.messages[0]["message"] == "Please call me about bridal packages"

    handoff = await tools.run("transfer_to_human", {"reason": "asked for the manager"})
    assert handoff["say"].startswith("Ayesha from the team")
    assert tools.state.intent is Intent.HANDOFF

    assert (await tools.run("end_call", {}))["ended"] is True
    assert tools.state.ended


async def test_handoff_keeps_the_number_the_caller_already_gave(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    """Found by the Phase 9 injection eval: the number was dropped and asked for again."""
    tools = make_tools(salon, backend, monday_9am)
    handoff = await tools.run(
        "transfer_to_human",
        {"reason": "message for the owner", "caller_name": "Kamran", "phone": "0333 4567890"},
    )
    assert "hint" not in handoff
    assert (tools.state.caller_name, tools.state.phone) == ("Kamran", "+923334567890")
    assert backend.handoffs[-1]["phone"] == "+923334567890"


async def test_unknown_tool_and_handler_crash_are_contained(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    assert (await tools.run("delete_everything", {}))["ok"] is False

    async def boom(_: object) -> dict[str, Any]:
        raise RuntimeError("calendar down")

    tools.handlers["check_availability"] = boom
    result = await tools.run("check_availability", {})
    assert result["ok"] is False
    assert "take a message" in result["error"]


def test_goodbye_line_follows_the_callers_language(salon: TenantConfig) -> None:
    assert goodbye_line(salon, "en") == "Thank you for calling Noor Beauty Salon. Goodbye!"
    assert goodbye_line(salon, "ur").startswith("نور بیوٹی سیلون")
    assert goodbye_line(salon, None).endswith("Goodbye!")


# ---------------------------------------------------------------- wrong weekday (evals, Phase 6)


def test_weekdays_named_in_english_and_urdu() -> None:
    assert weekdays_named("Thursday at 3, or maybe friday?") == {3, 4}
    assert weekdays_named("جمعرات کو تین بجے") == {3}
    assert weekdays_named("اگلے ہفتے") == set()  # "next week", not Saturday
    assert weekdays_named("Sundays are busy?") == set()  # whole words only


def test_days_in_play_follow_the_latest_caller_request_and_later_offers() -> None:
    context = LLMContext()
    assert days_in_play(context) is None
    context.add_message({"role": "user", "content": "Can I come on Wednesday?"})
    context.add_message({"role": "user", "content": "Sorry, Thursday at 3 instead."})
    assert days_in_play(context) == frozenset({3})
    context.add_message({"role": "assistant", "content": "Thursday is full; Friday at 3 is free."})
    context.add_message({"role": "user", "content": "Yes please, I'm Rabia."})
    # the offered Friday can be accepted with a plain yes
    assert days_in_play(context) == frozenset({3, 4})


async def test_booking_on_a_weekday_nobody_asked_for_is_refused(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    """Seen in the live evals: caller asked for Thursday, the model sent Monday's date."""
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]  # a Wednesday
    refused = await tools.run("create_booking", booking_args(start), days_in_play=frozenset({3}))
    assert refused["ok"] is False
    assert "is a Wednesday, but the caller asked for Thursday" in refused["error"]
    assert tools.state.pending is None  # nothing waiting for a yes

    ok = await tools.run("create_booking", booking_args(start), days_in_play=frozenset({2}))
    assert ok["ok"] and ok["booked"] is False
    # no weekday named at all ("tomorrow", "the 18th"): no check
    assert (await tools.run("create_booking", booking_args(start), days_in_play=None))["ok"]


async def test_reschedule_to_a_weekday_nobody_asked_for_is_refused(
    salon: TenantConfig, backend: InMemoryBackend, monday_9am: datetime
) -> None:
    tools = make_tools(salon, backend, monday_9am)
    start = (await offer(tools))["options"][0]["start"]
    await book(tools, start)
    new_start = (await offer(tools, preferred="17:00"))["options"][0]["start"]
    args = {"phone": PHONE, "new_start": new_start, "caller_confirmed": False}
    refused = await tools.run("reschedule_booking", args, days_in_play=frozenset({4}))
    assert refused["ok"] is False and "Friday" in refused["error"]
