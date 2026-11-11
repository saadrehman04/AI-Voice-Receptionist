"""Free appointment slots from opening hours, service length and existing bookings.

Pure functions (no I/O) so the same logic serves the Phase 2 stub backend and the Phase 3
calendar-backed api, and can be tested exhaustively.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from receptionist_shared.tenant import Service, TenantConfig

SLOT_STEP_MIN = 15
HOLD_BUCKET_MIN = 15


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: Interval) -> bool:
        return self.start < other.end and other.start < self.end


def free_slots(
    tenant: TenantConfig,
    service: Service,
    day: date,
    busy: Iterable[Interval],
    *,
    now: datetime,
    step_min: int = SLOT_STEP_MIN,
) -> list[datetime]:
    """Every start time on `day` where `service` fits, in the tenant's timezone.

    A slot must lie inside one opening range, start after `now + min_notice`, fall within
    `max_days_ahead`, and keep `booking_buffer_min` clear of every busy interval.
    """
    tz = tenant.tz
    now = now.astimezone(tz)
    if day < now.date() or day > now.date() + timedelta(days=tenant.max_days_ahead):
        return []
    earliest = now + timedelta(minutes=tenant.min_notice_min)
    duration = timedelta(minutes=service.duration_min)
    buffer = timedelta(minutes=tenant.booking_buffer_min)
    blocked = [Interval(b.start - buffer, b.end + buffer) for b in busy]

    slots: list[datetime] = []
    for opening in tenant.hours_on(day):
        start = datetime.combine(day, opening.start, tzinfo=tz)
        close = datetime.combine(day, opening.end, tzinfo=tz)
        cursor = start
        while cursor + duration <= close:
            candidate = Interval(cursor, cursor + duration)
            if cursor >= earliest and not any(candidate.overlaps(b) for b in blocked):
                slots.append(cursor)
            cursor += timedelta(minutes=step_min)
    return slots


def pick_options(
    slots: Sequence[datetime], *, preferred: time | None = None, limit: int = 3
) -> list[datetime]:
    """At most `limit` slots to offer: closest to `preferred`, else spread over the day."""
    if not slots or limit <= 0:
        return []
    if preferred is not None:

        def distance(s: datetime) -> float:
            minutes = s.hour * 60 + s.minute
            return abs(minutes - (preferred.hour * 60 + preferred.minute))

        return sorted(sorted(slots, key=distance)[:limit])
    if len(slots) <= limit:
        return list(slots)
    # First, last and evenly spaced in between: gives morning/afternoon/evening choices.
    step = (len(slots) - 1) / (limit - 1) if limit > 1 else 0
    picked = sorted({round(i * step) for i in range(limit)})
    return [slots[i] for i in picked]


def spoken_slot(dt: datetime) -> str:
    """'Wednesday 13 November, 2:30 PM'. The LLM turns this into natural speech."""
    hour = dt.strftime("%I").lstrip("0")
    return f"{dt:%A} {dt.day} {dt:%B}, {hour}:{dt:%M} {dt:%p}"


@dataclass(frozen=True)
class SearchResult:
    """Options to offer: on the requested day if possible, else the next day that has any."""

    day: date
    options: list[datetime]
    requested_day_available: bool


BusyLookup = Callable[[date], Awaitable[Sequence[Interval]]]


async def search_options(
    tenant: TenantConfig,
    service: Service,
    day: date,
    busy_for: BusyLookup,
    *,
    now: datetime,
    preferred: time | None = None,
    search_days: int = 7,
    limit: int = 3,
) -> SearchResult:
    """`pick_options` for `day`; if it has none, the first later day (within `search_days`)
    that has some."""
    for offset in range(search_days + 1):
        candidate = day + timedelta(days=offset)
        busy = await busy_for(candidate)
        slots = free_slots(tenant, service, candidate, busy, now=now)
        if slots:
            return SearchResult(
                candidate, pick_options(slots, preferred=preferred, limit=limit), offset == 0
            )
    return SearchResult(day, [], False)


def guard_interval(tenant: TenantConfig, start: datetime, end: datetime) -> Interval:
    """The time a booking keeps clear: the appointment plus the buffer on both sides."""
    buffer = timedelta(minutes=tenant.booking_buffer_min)
    return Interval(start - buffer, end + buffer)


def hold_buckets(tenant: TenantConfig, start: datetime, end: datetime) -> list[datetime]:
    """15-minute buckets a booking (with buffer) touches, aligned to the hour.

    Holding every bucket means two callers can't hold overlapping slots, even ones that start
    at different times (14:00 for 45 min vs 14:30), which a per-start-time lock would allow.
    """
    guard = guard_interval(tenant, start, end)
    first = guard.start.astimezone(tenant.tz)
    first = first.replace(
        minute=first.minute - first.minute % HOLD_BUCKET_MIN, second=0, microsecond=0
    )
    buckets: list[datetime] = []
    cursor = first
    while cursor < guard.end:
        buckets.append(cursor)
        cursor += timedelta(minutes=HOLD_BUCKET_MIN)
    return buckets
