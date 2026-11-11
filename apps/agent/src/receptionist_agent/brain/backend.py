"""Where tools read and write bookings and messages.

`BookingBackend` is the interface the tools use. Phase 2 ships `InMemoryBackend` (stub data,
no I/O); Phase 3 adds an HTTP backend that calls apps/api, which talks to Google Calendar and
Postgres. The tools never know which one they have.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol

from receptionist_shared.availability import Interval
from receptionist_shared.tenant import TenantConfig


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class SlotTakenError(RuntimeError):
    """Someone else booked the slot between the offer and the write."""


class BookingNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Booking:
    id: str
    tenant_slug: str
    service: str
    start: datetime
    end: datetime
    caller_name: str
    phone: str
    status: BookingStatus = BookingStatus.CONFIRMED
    call_id: str | None = None

    @property
    def interval(self) -> Interval:
        return Interval(self.start, self.end)


def idempotency_key(*parts: object) -> str:
    """Same request -> same key, so a retried tool call can't create a second booking."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


class BookingBackend(Protocol):
    async def busy_intervals(self, tenant: TenantConfig, day: date) -> list[Interval]: ...

    async def create_booking(
        self,
        tenant: TenantConfig,
        *,
        service: str,
        start: datetime,
        end: datetime,
        caller_name: str,
        phone: str,
        call_id: str,
        idempotency_key: str,
    ) -> Booking: ...

    async def find_bookings(self, tenant: TenantConfig, phone: str) -> list[Booking]: ...

    async def reschedule_booking(
        self, tenant: TenantConfig, booking_id: str, *, start: datetime, end: datetime
    ) -> Booking: ...

    async def cancel_booking(self, tenant: TenantConfig, booking_id: str) -> Booking: ...

    async def take_message(
        self,
        tenant: TenantConfig,
        *,
        caller_name: str | None,
        phone: str | None,
        message: str,
        call_id: str,
    ) -> str: ...

    async def request_handoff(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_name: str | None,
        phone: str | None,
        call_id: str,
    ) -> str: ...


@dataclass
class InMemoryBackend:
    """Stub backend for Phase 2 and tests. Keeps everything in dicts, per process."""

    bookings: dict[str, Booking] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    messages: list[dict[str, object]] = field(default_factory=list)
    handoffs: list[dict[str, object]] = field(default_factory=list)

    def _live(self, tenant: TenantConfig) -> list[Booking]:
        return [
            b
            for b in self.bookings.values()
            if b.tenant_slug == tenant.slug and b.status == BookingStatus.CONFIRMED
        ]

    async def busy_intervals(self, tenant: TenantConfig, day: date) -> list[Interval]:
        return [
            b.interval for b in self._live(tenant) if b.start.astimezone(tenant.tz).date() == day
        ]

    async def create_booking(
        self,
        tenant: TenantConfig,
        *,
        service: str,
        start: datetime,
        end: datetime,
        caller_name: str,
        phone: str,
        call_id: str,
        idempotency_key: str,
    ) -> Booking:
        if idempotency_key in self.by_key:
            return self.bookings[self.by_key[idempotency_key]]
        wanted = Interval(start, end)
        if any(b.interval.overlaps(wanted) for b in self._live(tenant)):
            raise SlotTakenError("slot already booked")
        booking = Booking(
            id=uuid.uuid4().hex[:8].upper(),
            tenant_slug=tenant.slug,
            service=service,
            start=start,
            end=end,
            caller_name=caller_name,
            phone=phone,
            call_id=call_id,
        )
        self.bookings[booking.id] = booking
        self.by_key[idempotency_key] = booking.id
        return booking

    async def find_bookings(self, tenant: TenantConfig, phone: str) -> list[Booking]:
        return sorted((b for b in self._live(tenant) if b.phone == phone), key=lambda b: b.start)

    def _get(self, tenant: TenantConfig, booking_id: str) -> Booking:
        booking = self.bookings.get(booking_id)
        if booking is None or booking.tenant_slug != tenant.slug:
            raise BookingNotFoundError(booking_id)
        return booking

    async def reschedule_booking(
        self, tenant: TenantConfig, booking_id: str, *, start: datetime, end: datetime
    ) -> Booking:
        booking = self._get(tenant, booking_id)
        wanted = Interval(start, end)
        if any(b.id != booking_id and b.interval.overlaps(wanted) for b in self._live(tenant)):
            raise SlotTakenError("slot already booked")
        moved = replace(booking, start=start, end=end)
        self.bookings[booking_id] = moved
        return moved

    async def cancel_booking(self, tenant: TenantConfig, booking_id: str) -> Booking:
        booking = replace(self._get(tenant, booking_id), status=BookingStatus.CANCELLED)
        self.bookings[booking_id] = booking
        return booking

    async def take_message(
        self,
        tenant: TenantConfig,
        *,
        caller_name: str | None,
        phone: str | None,
        message: str,
        call_id: str,
    ) -> str:
        message_id = uuid.uuid4().hex[:8]
        self.messages.append(
            {
                "id": message_id,
                "tenant": tenant.slug,
                "caller_name": caller_name,
                "phone": phone,
                "message": message,
                "call_id": call_id,
            }
        )
        return message_id

    async def request_handoff(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_name: str | None,
        phone: str | None,
        call_id: str,
    ) -> str:
        handoff_id = uuid.uuid4().hex[:8]
        self.handoffs.append(
            {
                "id": handoff_id,
                "tenant": tenant.slug,
                "reason": reason,
                "caller_name": caller_name,
                "phone": phone,
                "call_id": call_id,
            }
        )
        return handoff_id
