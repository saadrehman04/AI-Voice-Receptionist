"""Where tools read and write bookings, holds, messages and business knowledge.

`BookingBackend` is the interface the tools use:

- `ApiBackend` (brain/api_backend.py): the real one, calls apps/api over HTTP, which talks to
  Google Calendar, Redis (holds) and Postgres.
- `InMemoryBackend` (here): same behaviour in dicts, for tests and `make chat`.

Holds: when the agent reads a slot back to the caller it *holds* it (2 minutes), so nobody else
can take it during the "shall I book that?" pause. A call holds at most one slot; holding a new
one releases the old.
"""

from __future__ import annotations

import hashlib
import secrets
import string
import time as monotonic_time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol

from receptionist_shared.api_models import BookingOut, BookingStatus
from receptionist_shared.availability import (
    Interval,
    guard_interval,
    hold_buckets,
    search_options,
)
from receptionist_shared.knowledge import KnowledgeResult, lookup
from receptionist_shared.tenant import TenantConfig

HOLD_SECS = 120
_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"

Booking = BookingOut


class SlotTakenError(RuntimeError):
    """Someone else holds or booked the slot."""


class BookingNotFoundError(LookupError):
    pass


class UnknownServiceError(ValueError):
    pass


@dataclass(frozen=True)
class Availability:
    day: date
    options: list[datetime]
    requested_day_available: bool


def booking_code() -> str:
    """8 characters, no 0/O/1/I, easy to read out over the phone."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


class BookingBackend(Protocol):
    async def availability(
        self,
        tenant: TenantConfig,
        service: str,
        day: date,
        *,
        preferred: time | None,
        call_id: str,
    ) -> Availability: ...

    async def hold(
        self, tenant: TenantConfig, *, service: str, start: datetime, call_id: str
    ) -> bool: ...

    async def create_booking(
        self,
        tenant: TenantConfig,
        *,
        service: str,
        start: datetime,
        caller_name: str,
        phone: str,
        call_id: str,
        idempotency_key: str,
    ) -> Booking: ...

    async def find_bookings(self, tenant: TenantConfig, phone: str) -> list[Booking]: ...

    async def reschedule_booking(
        self,
        tenant: TenantConfig,
        booking_id: str,
        *,
        start: datetime,
        call_id: str,
        idempotency_key: str,
    ) -> Booking: ...

    async def cancel_booking(
        self, tenant: TenantConfig, booking_id: str, *, call_id: str
    ) -> Booking: ...

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

    async def lookup(
        self, tenant: TenantConfig, question: str, *, language: str
    ) -> KnowledgeResult: ...


@dataclass
class InMemoryBackend:
    """Stub backend for tests and `make chat`. Same rules as the api, kept in dicts."""

    bookings: dict[str, Booking] = field(default_factory=dict)
    by_key: dict[str, str] = field(default_factory=dict)
    holds: dict[tuple[str, datetime], tuple[str, float]] = field(default_factory=dict)
    messages: list[dict[str, object]] = field(default_factory=list)
    handoffs: list[dict[str, object]] = field(default_factory=list)
    clock: Callable[[], datetime] | None = None  # override 'now' in tests

    # -- helpers

    def _now(self, tenant: TenantConfig) -> datetime:
        return self.clock() if self.clock is not None else tenant.now()

    def _live(self, tenant: TenantConfig) -> list[Booking]:
        return [
            b
            for b in self.bookings.values()
            if b.tenant_slug == tenant.slug and b.status == BookingStatus.CONFIRMED
        ]

    def _service(self, tenant: TenantConfig, name: str) -> tuple[str, int]:
        service = tenant.find_service(name)
        if service is None:
            raise UnknownServiceError(name)
        return service.name, service.duration_min

    def _prune_holds(self) -> None:
        now = monotonic_time.monotonic()
        for key in [k for k, (_, exp) in self.holds.items() if exp <= now]:
            del self.holds[key]

    def _held_by_others(self, tenant: TenantConfig, call_id: str) -> list[Interval]:
        self._prune_holds()
        width = timedelta(minutes=15)
        return [
            Interval(bucket, bucket + width)
            for (slug, bucket), (owner, _) in self.holds.items()
            if slug == tenant.slug and owner != call_id
        ]

    def _conflicts(
        self, tenant: TenantConfig, start: datetime, end: datetime, *, ignore: str | None = None
    ) -> bool:
        wanted = guard_interval(tenant, start, end)
        return any(
            b.id != ignore and Interval(b.start, b.end).overlaps(wanted) for b in self._live(tenant)
        )

    # -- interface

    async def availability(
        self,
        tenant: TenantConfig,
        service: str,
        day: date,
        *,
        preferred: time | None,
        call_id: str,
    ) -> Availability:
        svc = tenant.find_service(service)
        if svc is None:
            raise UnknownServiceError(service)

        async def busy_for(d: date) -> list[Interval]:
            booked = [
                Interval(b.start, b.end)
                for b in self._live(tenant)
                if b.start.astimezone(tenant.tz).date() == d
            ]
            return booked + self._held_by_others(tenant, call_id)

        result = await search_options(
            tenant, svc, day, busy_for, now=self._now(tenant), preferred=preferred
        )
        return Availability(result.day, result.options, result.requested_day_available)

    async def hold(
        self, tenant: TenantConfig, *, service: str, start: datetime, call_id: str
    ) -> bool:
        _, duration = self._service(tenant, service)
        end = start + timedelta(minutes=duration)
        self._prune_holds()
        buckets = hold_buckets(tenant, start, end)
        if any(
            self.holds.get((tenant.slug, b), (call_id, 0))[0] != call_id for b in buckets
        ) or self._conflicts(tenant, start, end):
            return False
        for key in [k for k, (owner, _) in self.holds.items() if owner == call_id]:
            del self.holds[key]  # one hold per call
        expires = monotonic_time.monotonic() + HOLD_SECS
        for b in buckets:
            self.holds[(tenant.slug, b)] = (call_id, expires)
        return True

    async def create_booking(
        self,
        tenant: TenantConfig,
        *,
        service: str,
        start: datetime,
        caller_name: str,
        phone: str,
        call_id: str,
        idempotency_key: str,
    ) -> Booking:
        if idempotency_key in self.by_key:
            return self.bookings[self.by_key[idempotency_key]]
        name, duration = self._service(tenant, service)
        if not await self.hold(tenant, service=name, start=start, call_id=call_id):
            raise SlotTakenError("slot already held or booked")
        booking = Booking(
            id=booking_code(),
            tenant_slug=tenant.slug,
            service=name,
            start=start,
            end=start + timedelta(minutes=duration),
            caller_name=caller_name,
            phone=phone,
            status=BookingStatus.CONFIRMED,
            call_id=call_id,
        )
        self.bookings[booking.id] = booking
        self.by_key[idempotency_key] = booking.id
        self._release(call_id)
        return booking

    def _release(self, call_id: str) -> None:
        for key in [k for k, (owner, _) in self.holds.items() if owner == call_id]:
            del self.holds[key]

    async def find_bookings(self, tenant: TenantConfig, phone: str) -> list[Booking]:
        now = self._now(tenant)
        return sorted(
            (b for b in self._live(tenant) if b.phone == phone and b.end > now),
            key=lambda b: b.start,
        )

    def _get(self, tenant: TenantConfig, booking_id: str) -> Booking:
        booking = self.bookings.get(booking_id)
        if booking is None or booking.tenant_slug != tenant.slug:
            raise BookingNotFoundError(booking_id)
        return booking

    async def reschedule_booking(
        self,
        tenant: TenantConfig,
        booking_id: str,
        *,
        start: datetime,
        call_id: str,
        idempotency_key: str,
    ) -> Booking:
        booking = self._get(tenant, booking_id)
        if booking.start == start:
            return booking  # retried request, already moved
        end = start + (booking.end - booking.start)
        if self._conflicts(tenant, start, end, ignore=booking_id):
            raise SlotTakenError("slot already booked")
        moved = booking.model_copy(update={"start": start, "end": end})
        self.bookings[booking_id] = moved
        self._release(call_id)
        return moved

    async def cancel_booking(
        self, tenant: TenantConfig, booking_id: str, *, call_id: str
    ) -> Booking:
        booking = self._get(tenant, booking_id).model_copy(
            update={"status": BookingStatus.CANCELLED}
        )
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
        message_id = secrets.token_hex(4)
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
        handoff_id = secrets.token_hex(4)
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

    async def lookup(
        self, tenant: TenantConfig, question: str, *, language: str
    ) -> KnowledgeResult:
        return lookup(tenant, question, language=language)


def idempotency_key(*parts: object) -> str:
    """Same request -> same key, so a retried tool call can't create a second booking."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
