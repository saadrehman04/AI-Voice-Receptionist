"""The 30 s availability cache (Phase 5): faster reads, never a double booking.

Real Postgres + Redis, like the other booking tests.
"""

from datetime import date, datetime, time, timedelta
from typing import Any

import pytest

from receptionist_api.booking_service import BookingService, SlotTakenError
from receptionist_api.calendar import FakeCalendar
from receptionist_shared.api_models import BookingCreate
from receptionist_shared.tenant import TenantConfig

WED = date(2026, 11, 18)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def busy_calls(calendar: FakeCalendar, monkeypatch: pytest.MonkeyPatch) -> list[date]:
    calls: list[date] = []
    real = calendar.busy

    async def counting(calendar_id: str, start: datetime, end: datetime) -> Any:
        calls.append(start.date())
        return await real(calendar_id, start, end)

    monkeypatch.setattr(calendar, "busy", counting)
    return calls


@pytest.fixture
def cached(bookings: BookingService, calendar: FakeCalendar, clock: Clock) -> BookingService:
    return BookingService(
        bookings._sessions,
        bookings._holds,
        calendar,
        clock=bookings._clock,
        cache_secs=30,
        monotonic=clock,
    )


async def offers(
    service: BookingService, salon: TenantConfig, call_id: str = "c1"
) -> list[datetime]:
    return (await service.availability(salon, "Haircut", WED, time(14, 0), call_id)).options


def booking(start: datetime, n: int = 1) -> BookingCreate:
    return BookingCreate(
        service="Haircut",
        start=start,
        caller_name=f"Caller {n}",
        phone=f"+92300000000{n}",
        call_id=f"c{n}",
        idempotency_key=f"cache-key-{n:04d}",
    )


async def test_repeat_reads_hit_the_cache_until_it_expires(
    cached: BookingService, salon: TenantConfig, busy_calls: list[date], clock: Clock
) -> None:
    await cached.sync_tenant(salon)
    await offers(cached, salon)
    await offers(cached, salon)
    assert busy_calls == [WED]  # the second read didn't ask the calendar
    clock.now += 31
    await offers(cached, salon)
    assert busy_calls == [WED, WED]


async def test_a_booking_clears_the_cache_at_once(
    cached: BookingService, salon: TenantConfig
) -> None:
    await cached.sync_tenant(salon)
    first = (await offers(cached, salon))[0]
    await cached.create(salon, booking(first))
    assert first not in await offers(cached, salon, call_id="c2")


async def test_a_stale_cache_can_offer_a_slot_but_never_double_book_it(
    cached: BookingService, salon: TenantConfig, calendar: FakeCalendar
) -> None:
    """The owner blocks 14:00 in Google Calendar; the cache hasn't seen it yet."""
    await cached.sync_tenant(salon)
    slot = (await offers(cached, salon))[0]
    calendar.add_external_event(cached.calendar_id(salon), slot, slot + timedelta(minutes=45))
    assert slot in await offers(cached, salon)  # stale, for up to 30 s
    with pytest.raises(SlotTakenError):
        await cached.create(salon, booking(slot))  # the write re-checks without the cache


async def test_holds_are_never_cached(cached: BookingService, salon: TenantConfig) -> None:
    await cached.sync_tenant(salon)
    slot = (await offers(cached, salon, call_id="a"))[0]
    await cached.hold(salon, "Haircut", slot, "a")
    assert slot not in await offers(cached, salon, call_id="b")


async def test_cache_off_by_default(
    bookings: BookingService, salon: TenantConfig, busy_calls: list[date]
) -> None:
    await bookings.sync_tenant(salon)
    await offers(bookings, salon)
    await offers(bookings, salon)
    assert busy_calls == [WED, WED]
