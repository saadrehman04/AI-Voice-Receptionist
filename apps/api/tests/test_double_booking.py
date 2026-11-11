"""Two callers, one slot, same moment: exactly one booking must win.

These run against real Postgres and Redis because the guarantees live there.
"""

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from receptionist_api.booking_service import BookingService, SlotTakenError
from receptionist_api.db.models import Booking
from receptionist_shared.api_models import BookingCreate, BookingOut
from receptionist_shared.tenant import TenantConfig

WED_14 = "2026-11-18T14:00:00+05:00"


def request(start: str, n: int) -> BookingCreate:
    return BookingCreate(
        service="Haircut",
        start=datetime.fromisoformat(start),
        caller_name=f"Caller {n}",
        phone=f"+92300000000{n}",
        call_id=f"call-{n}",
        idempotency_key=f"race-key-{n:04d}",
    )


async def confirmed(bookings: BookingService) -> list[Booking]:
    async with bookings._sessions() as s:
        return list((await s.scalars(select(Booking).where(Booking.status == "confirmed"))).all())


async def race(bookings: BookingService, salon: TenantConfig, starts: list[str]) -> list[object]:
    await bookings.sync_tenant(salon)
    return await asyncio.gather(
        *(bookings.create(salon, request(s, i)) for i, s in enumerate(starts)),
        return_exceptions=True,
    )


async def test_same_slot_same_moment(bookings: BookingService, salon: TenantConfig) -> None:
    results = await race(bookings, salon, [WED_14] * 5)
    winners = [r for r in results if not isinstance(r, BaseException)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(e, SlotTakenError) for e in losers), losers
    assert len(await confirmed(bookings)) == 1


async def test_overlapping_slots_with_different_starts(
    bookings: BookingService, salon: TenantConfig
) -> None:
    # 14:00 and 14:30 haircuts (45 min) overlap; 16:00 does not.
    results = await race(
        bookings,
        salon,
        [WED_14, "2026-11-18T14:30:00+05:00", "2026-11-18T16:00:00+05:00"],
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    assert len(ok) == 2
    starts = sorted(r.start.strftime("%H:%M") for r in ok if isinstance(r, BookingOut))
    assert starts[1] == "16:00"


async def test_database_constraint_alone_blocks_overlap(
    bookings: BookingService, salon: TenantConfig
) -> None:
    """Skip Redis and the re-check entirely: Postgres must still refuse the second insert."""
    tenant_id = await bookings.sync_tenant(salon)
    service = salon.find_service("Haircut")
    assert service is not None
    start = datetime.fromisoformat(WED_14)
    end = start + timedelta(minutes=service.duration_min)
    results = await asyncio.gather(
        *(
            bookings._insert(salon, tenant_id, service, start, end, request(WED_14, n))
            for n in range(4)
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, BaseException) for r in results) == 1
    assert all(isinstance(r, SlotTakenError) for r in results if isinstance(r, BaseException))
    assert len(await confirmed(bookings)) == 1


async def test_buffer_is_enforced_by_the_constraint(
    bookings: BookingService, salon: TenantConfig
) -> None:
    """Salon buffer is 15 min: a booking 10 min after another ends must be refused."""
    tenant_id = await bookings.sync_tenant(salon)
    service = salon.find_service("Haircut")
    assert service is not None
    a = datetime.fromisoformat(WED_14)
    await bookings._insert(
        salon, tenant_id, service, a, a + timedelta(minutes=45), request(WED_14, 1)
    )
    b = a + timedelta(minutes=55)  # 10 min gap < 15 min buffer
    try:
        await bookings._insert(
            salon, tenant_id, service, b, b + timedelta(minutes=45), request(b.isoformat(), 2)
        )
    except SlotTakenError:
        pass
    else:
        raise AssertionError("buffer violation was accepted")
    c = a + timedelta(minutes=60)  # exactly 15 min gap: allowed
    await bookings._insert(
        salon, tenant_id, service, c, c + timedelta(minutes=45), request(c.isoformat(), 3)
    )
    assert len(await confirmed(bookings)) == 2
