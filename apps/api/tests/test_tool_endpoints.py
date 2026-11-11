"""The agent-facing endpoints against real Postgres + Redis and a fake calendar."""

from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from receptionist_api.booking_service import BookingService
from receptionist_api.calendar import FakeCalendar
from receptionist_api.db.models import Booking, Message

SALON = "/v1/tenants/demo-salon"
WED = "2026-11-18"
PHONE = "+923001234567"


async def options(api: httpx.AsyncClient, call_id: str = "call-a", **over: Any) -> list[str]:
    body = {"service": "Haircut", "day": WED, "preferred_time": "14:00", "call_id": call_id}
    r = await api.post(f"{SALON}/availability", json=body | over)
    assert r.status_code == 200, r.text
    return list(r.json()["options"])


def booking_body(start: str, call_id: str = "call-a", key: str = "key-0001") -> dict[str, Any]:
    return {
        "service": "Haircut",
        "start": start,
        "caller_name": "Sara",
        "phone": PHONE,
        "call_id": call_id,
        "idempotency_key": key,
    }


async def test_token_required(api: httpx.AsyncClient) -> None:
    r = await api.post(
        f"{SALON}/availability",
        headers={"X-Agent-Token": "wrong"},
        json={"service": "Haircut", "day": WED, "call_id": "c"},
    )
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "unauthorized"


async def test_unknown_tenant_and_service(api: httpx.AsyncClient) -> None:
    r = await api.post(
        "/v1/tenants/nope/availability", json={"service": "Haircut", "day": WED, "call_id": "c"}
    )
    assert r.json()["detail"]["code"] == "tenant_not_found"
    r = await api.post(
        f"{SALON}/availability", json={"service": "Massage", "day": WED, "call_id": "c"}
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_service"


async def test_availability_respects_calendar_events(
    api: httpx.AsyncClient, calendar: FakeCalendar, salon: Any
) -> None:
    before = await options(api)
    assert f"{WED}T14:00:00+05:00" in before
    calendar.add_external_event(
        "fake:demo-salon",
        datetime.fromisoformat(f"{WED}T13:30:00+05:00"),
        datetime.fromisoformat(f"{WED}T15:00:00+05:00"),
    )
    after = await options(api)
    assert f"{WED}T14:00:00+05:00" not in after


async def test_booking_lifecycle(api: httpx.AsyncClient, calendar: FakeCalendar) -> None:
    start = (await options(api))[0]
    assert (
        await api.post(
            f"{SALON}/holds", json={"service": "Haircut", "start": start, "call_id": "call-a"}
        )
    ).json()["held"]
    r = await api.post(f"{SALON}/bookings", json=booking_body(start))
    assert r.status_code == 201, r.text
    booking = r.json()
    assert booking["status"] == "confirmed"
    assert len(booking["id"]) == 8
    assert booking["calendar_event_id"] in calendar.events["fake:demo-salon"]

    found = (await api.get(f"{SALON}/bookings", params={"phone": "0300 1234567"})).json()
    assert [b["id"] for b in found] == [booking["id"]]

    new_start = (await options(api, call_id="call-b", preferred_time="18:00"))[0]
    await api.post(
        f"{SALON}/holds", json={"service": "Haircut", "start": new_start, "call_id": "call-b"}
    )
    moved = await api.post(
        f"{SALON}/bookings/{booking['id']}/reschedule",
        json={"start": new_start, "call_id": "call-b", "idempotency_key": "move-0001"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["start"] == new_start
    event = calendar.events["fake:demo-salon"][booking["calendar_event_id"]]
    assert event.start.isoformat() == new_start

    cancelled = await api.post(
        f"{SALON}/bookings/{booking['id']}/cancel", json={"call_id": "call-c"}
    )
    assert cancelled.json()["status"] == "cancelled"
    assert booking["calendar_event_id"] not in calendar.events["fake:demo-salon"]
    assert (await api.get(f"{SALON}/bookings", params={"phone": PHONE})).json() == []


async def test_idempotent_create(api: httpx.AsyncClient, bookings: BookingService) -> None:
    start = (await options(api))[0]
    first = await api.post(f"{SALON}/bookings", json=booking_body(start))
    again = await api.post(f"{SALON}/bookings", json=booking_body(start))
    assert first.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    async with bookings._sessions() as s:
        assert len((await s.scalars(select(Booking))).all()) == 1


async def test_hold_blocks_other_callers(api: httpx.AsyncClient) -> None:
    start = (await options(api))[0]
    held = await api.post(
        f"{SALON}/holds", json={"service": "Haircut", "start": start, "call_id": "call-a"}
    )
    assert held.json() == {"held": True, "expires_in_secs": 120}
    # Caller B no longer sees it, can't hold it, and can't book it.
    assert start not in await options(api, call_id="call-b")
    other = await api.post(
        f"{SALON}/holds", json={"service": "Haircut", "start": start, "call_id": "call-b"}
    )
    assert other.json()["held"] is False
    r = await api.post(f"{SALON}/bookings", json=booking_body(start, "call-b", "key-b-0001"))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_taken"


async def test_calendar_failure_writes_nothing(
    api: httpx.AsyncClient, calendar: FakeCalendar, bookings: BookingService
) -> None:
    start = (await options(api))[0]
    original_create = calendar.create_event

    async def broken(*args: Any, **kwargs: Any) -> str:
        from receptionist_api.calendar import CalendarError

        raise CalendarError("google is down")

    calendar.create_event = broken  # type: ignore[method-assign]
    r = await api.post(f"{SALON}/bookings", json=booking_body(start))
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "calendar_unavailable"
    async with bookings._sessions() as s:
        assert (await s.scalars(select(Booking))).all() == []
    calendar.create_event = original_create  # type: ignore[method-assign]
    assert (await api.post(f"{SALON}/bookings", json=booking_body(start))).status_code == 201


async def test_messages_and_handoffs_are_stored(
    api: httpx.AsyncClient, bookings: BookingService
) -> None:
    for kind in ("message", "handoff"):
        r = await api.post(
            f"{SALON}/messages",
            json={
                "kind": kind,
                "body": f"{kind} body",
                "caller_name": "Sara",
                "phone": PHONE,
                "call_id": "call-a",
            },
        )
        assert r.status_code == 201
    async with bookings._sessions() as s:
        rows = (await s.scalars(select(Message).order_by(Message.created_at))).all()
    assert [r.kind for r in rows] == ["message", "handoff"]
    assert all(r.status == "new" for r in rows)


async def test_knowledge_lookup_config_and_kb(api: httpx.AsyncClient) -> None:
    r = (await api.post(f"{SALON}/knowledge/lookup", json={"question": "is there parking"})).json()
    assert r["found"] and "free parking" in r["answer"]
    r = (
        await api.post(
            f"{SALON}/knowledge/lookup",
            json={"question": "how long should I wait before washing coloured hair"},
        )
    ).json()
    assert r["found"] and "48 hours" in r["answer"]
    assert any("hair-and-colour.md" in s for s in r["sources"])
    r = (await api.post(f"{SALON}/knowledge/lookup", json={"question": "gift vouchers?"})).json()
    assert r["found"] is False


@pytest.mark.parametrize("phone", ["12", "+1 415 555 0100"])
async def test_find_rejects_bad_phone(api: httpx.AsyncClient, phone: str) -> None:
    r = await api.get(f"{SALON}/bookings", params={"phone": phone})
    assert r.status_code == 422
