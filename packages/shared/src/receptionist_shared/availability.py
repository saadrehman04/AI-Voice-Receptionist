"""Free appointment slots from opening hours, service length and existing bookings.

Pure functions (no I/O) so the same logic serves the Phase 2 stub backend and the Phase 3
calendar-backed api, and can be tested exhaustively.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from receptionist_shared.tenant import Service, TenantConfig

SLOT_STEP_MIN = 15


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
