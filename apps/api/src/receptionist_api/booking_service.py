"""Bookings: availability, holds, create / reschedule / cancel, messages.

Defence in depth against double booking, in the order a write meets them:

1. Redis hold: while the agent reads a slot back, other callers can't hold or book it.
2. Idempotency key: a retried request returns the booking it already made.
3. Re-check: right before writing, the slot must still be free in the calendar and the DB.
4. Postgres exclusion constraint: the insert itself fails if any confirmed booking overlaps.

The calendar write happens inside the DB transaction: if Google fails, the row is rolled back;
if the commit fails after the event was created, the event is deleted again.
"""

from __future__ import annotations

import secrets
import string
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql.ranges import Range
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist_api.calendar import Calendar, event_id_for
from receptionist_api.db.models import Booking, Message, Tenant
from receptionist_api.db.session import transaction
from receptionist_api.holds import SlotHolds
from receptionist_shared.api_models import (
    AvailabilityResponse,
    BookingCreate,
    BookingOut,
    BookingStatus,
    MessageCreate,
    RescheduleRequest,
)
from receptionist_shared.availability import Interval, free_slots, search_options
from receptionist_shared.tenant import Service, TenantConfig

_CODE_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


class SlotTakenError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


class UnknownServiceError(ValueError):
    pass


def booking_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))


def guard_range(tenant: TenantConfig, start: datetime, end: datetime) -> Range[datetime]:
    """Half the buffer on each side: two guards overlap iff the bookings are < buffer apart."""
    half = timedelta(minutes=tenant.booking_buffer_min / 2)
    return Range(start - half, end + half, bounds="[)")


def _out(tenant: TenantConfig, row: Booking) -> BookingOut:
    return BookingOut(
        id=row.code,
        tenant_slug=tenant.slug,
        service=row.service,
        start=row.starts_at.astimezone(tenant.tz),
        end=row.ends_at.astimezone(tenant.tz),
        caller_name=row.customer_name,
        phone=row.customer_phone,
        status=BookingStatus(row.status),
        call_id=row.call_id,
        calendar_event_id=row.calendar_event_id,
    )


def _is_exclusion_violation(exc: IntegrityError) -> bool:
    return "ex_bookings_no_overlap" in str(exc.orig)


def _is_idempotency_violation(exc: IntegrityError) -> bool:
    return "uq_bookings_tenant_idem" in str(exc.orig)


class BookingService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        holds: SlotHolds,
        calendar: Calendar,
        *,
        clock: Callable[[TenantConfig], datetime] | None = None,
    ) -> None:
        self._sessions = sessions
        self._holds = holds
        self._calendar = calendar
        self._clock = clock or (lambda tenant: tenant.now())
        self._tenant_ids: dict[str, uuid.UUID] = {}

    @property
    def hold_secs(self) -> int:
        return self._holds.hold_secs

    # ------------------------------------------------------------ tenants

    async def sync_tenant(self, tenant: TenantConfig) -> uuid.UUID:
        """Make sure the tenant has a row (YAML is the source of truth for its facts)."""
        async with transaction(self._sessions) as session:
            row = await session.scalar(select(Tenant).where(Tenant.slug == tenant.slug))
            if row is None:
                row = Tenant(slug=tenant.slug, name=tenant.business_name)
                session.add(row)
                await session.flush()
            elif row.name != tenant.business_name:
                row.name = tenant.business_name
            self._tenant_ids[tenant.slug] = row.id
            return row.id

    async def _tenant_id(self, tenant: TenantConfig) -> uuid.UUID:
        return self._tenant_ids.get(tenant.slug) or await self.sync_tenant(tenant)

    # ------------------------------------------------------------ helpers

    @staticmethod
    def calendar_id(tenant: TenantConfig) -> str:
        return tenant.calendar_id or f"fake:{tenant.slug}"

    @staticmethod
    def _service(tenant: TenantConfig, name: str) -> Service:
        service = tenant.find_service(name)
        if service is None:
            raise UnknownServiceError(name)
        return service

    async def _db_busy(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        start: datetime,
        end: datetime,
        *,
        exclude_code: str | None = None,
    ) -> list[Interval]:
        stmt = select(Booking.starts_at, Booking.ends_at, Booking.code).where(
            Booking.tenant_id == tenant_id,
            Booking.status == BookingStatus.CONFIRMED.value,
            Booking.starts_at < end,
            Booking.ends_at > start,
        )
        rows = (await session.execute(stmt)).all()
        return [Interval(s, e) for s, e, code in rows if code != exclude_code]

    async def _busy_for_day(self, tenant: TenantConfig, day: date, call_id: str) -> list[Interval]:
        start = datetime.combine(day, time.min, tzinfo=tenant.tz)
        end = start + timedelta(days=1)
        tenant_id = await self._tenant_id(tenant)
        cal = await self._calendar.busy(self.calendar_id(tenant), start, end)
        async with self._sessions() as session:
            db = await self._db_busy(session, tenant_id, start, end)
        held = await self._holds.held_by_others(tenant, day, call_id)
        return cal + db + held

    async def _is_free(
        self,
        tenant: TenantConfig,
        service: Service,
        start: datetime,
        *,
        exclude: Booking | None = None,
    ) -> bool:
        """Fresh check against calendar + DB (not holds) right before a write."""
        end = start + timedelta(minutes=service.duration_min)
        window_start = start - timedelta(hours=12)
        window_end = end + timedelta(hours=12)
        cal = await self._calendar.busy(self.calendar_id(tenant), window_start, window_end)
        if exclude is not None:
            own = Interval(exclude.starts_at, exclude.ends_at)
            cal = [b for b in cal if not (b.start == own.start and b.end == own.end)]
        async with self._sessions() as session:
            db = await self._db_busy(
                session,
                await self._tenant_id(tenant),
                window_start,
                window_end,
                exclude_code=exclude.code if exclude else None,
            )
        slots = free_slots(
            tenant, service, start.astimezone(tenant.tz).date(), cal + db, now=self._clock(tenant)
        )
        return start in slots

    # ------------------------------------------------------------ reads

    async def availability(
        self,
        tenant: TenantConfig,
        service_name: str,
        day: date,
        preferred: time | None,
        call_id: str,
    ) -> AvailabilityResponse:
        service = self._service(tenant, service_name)

        async def busy_for(d: date) -> list[Interval]:
            return await self._busy_for_day(tenant, d, call_id)

        result = await search_options(
            tenant, service, day, busy_for, now=self._clock(tenant), preferred=preferred
        )
        return AvailabilityResponse(
            day=result.day,
            options=result.options,
            requested_day_available=result.requested_day_available,
        )

    async def find(self, tenant: TenantConfig, phone: str) -> list[BookingOut]:
        tenant_id = await self._tenant_id(tenant)
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Booking)
                .where(
                    Booking.tenant_id == tenant_id,
                    Booking.customer_phone == phone,
                    Booking.status == BookingStatus.CONFIRMED.value,
                    Booking.ends_at > self._clock(tenant),
                )
                .order_by(Booking.starts_at)
            )
            return [_out(tenant, r) for r in rows]

    async def _get(self, session: AsyncSession, tenant: TenantConfig, code: str) -> Booking:
        row = await session.scalar(
            select(Booking)
            .where(Booking.tenant_id == await self._tenant_id(tenant), Booking.code == code)
            .with_for_update()
        )
        if row is None:
            raise NotFoundError(code)
        return row

    # ------------------------------------------------------------ holds

    async def hold(
        self, tenant: TenantConfig, service_name: str, start: datetime, call_id: str
    ) -> bool:
        service = self._service(tenant, service_name)
        end = start + timedelta(minutes=service.duration_min)
        if not await self._is_free(tenant, service, start):
            return False
        return await self._holds.acquire(tenant, start, end, call_id)

    # ------------------------------------------------------------ writes

    async def _ensure_hold(
        self, tenant: TenantConfig, start: datetime, end: datetime, call_id: str
    ) -> None:
        if not await self._holds.owns(tenant, start, end, call_id) and not (
            await self._holds.acquire(tenant, start, end, call_id)
        ):
            raise SlotTakenError("slot is held by another call")

    async def create(self, tenant: TenantConfig, req: BookingCreate) -> BookingOut:
        tenant_id = await self._tenant_id(tenant)
        async with self._sessions() as session:
            existing = await session.scalar(
                select(Booking).where(
                    Booking.tenant_id == tenant_id, Booking.idempotency_key == req.idempotency_key
                )
            )
            if existing is not None:
                return _out(tenant, existing)

        service = self._service(tenant, req.service)
        start = req.start.astimezone(tenant.tz)
        end = start + timedelta(minutes=service.duration_min)
        await self._ensure_hold(tenant, start, end, req.call_id)
        try:
            if not await self._is_free(tenant, service, start):
                raise SlotTakenError("slot no longer free")
            return await self._insert(tenant, tenant_id, service, start, end, req)
        finally:
            await self._holds.release(tenant, req.call_id)

    async def _insert(
        self,
        tenant: TenantConfig,
        tenant_id: uuid.UUID,
        service: Service,
        start: datetime,
        end: datetime,
        req: BookingCreate,
    ) -> BookingOut:
        event_id = event_id_for(f"{tenant.slug}:{req.idempotency_key}")
        calendar_id = self.calendar_id(tenant)
        created_event = False
        try:
            async with transaction(self._sessions) as session:
                row = Booking(
                    tenant_id=tenant_id,
                    code=booking_code(),
                    call_id=req.call_id,
                    service=service.name,
                    customer_name=req.caller_name,
                    customer_phone=req.phone,
                    starts_at=start,
                    ends_at=end,
                    guard=guard_range(tenant, start, end),
                    status=BookingStatus.CONFIRMED.value,
                    calendar_event_id=event_id,
                    idempotency_key=req.idempotency_key,
                )
                session.add(row)
                await session.flush()  # the exclusion constraint fires here
                await self._calendar.create_event(
                    calendar_id,
                    event_id=event_id,
                    summary=f"{service.name}: {req.caller_name}",
                    description=f"Booked by phone. {req.phone}. Ref {row.code}.",
                    start=start,
                    end=end,
                    timezone=tenant.timezone,
                )
                created_event = True
            logger.info("booking stored", code=row.code, tenant=tenant.slug)
            return _out(tenant, row)
        except IntegrityError as exc:
            if _is_exclusion_violation(exc):
                raise SlotTakenError("overlaps a confirmed booking") from exc
            if _is_idempotency_violation(exc):
                async with self._sessions() as session:
                    again = await session.scalar(
                        select(Booking).where(
                            Booking.tenant_id == tenant_id,
                            Booking.idempotency_key == req.idempotency_key,
                        )
                    )
                if again is not None:
                    return _out(tenant, again)
            raise
        except BaseException:
            if created_event:  # commit failed after the event was written: undo it
                await self._calendar.cancel_event(calendar_id, event_id)
            raise

    async def reschedule(
        self, tenant: TenantConfig, code: str, req: RescheduleRequest
    ) -> BookingOut:
        new_start = req.start.astimezone(tenant.tz)
        async with transaction(self._sessions) as session:
            row = await self._get(session, tenant, code)
            if row.status != BookingStatus.CONFIRMED.value:
                raise NotFoundError(code)
            if row.starts_at == new_start:
                return _out(tenant, row)  # retried request
            service = tenant.find_service(row.service) or Service(
                name=row.service,
                duration_min=int((row.ends_at - row.starts_at).total_seconds() // 60),
            )
            new_end = new_start + timedelta(minutes=service.duration_min)
            await self._ensure_hold(tenant, new_start, new_end, req.call_id)
            try:
                if not await self._is_free(tenant, service, new_start, exclude=row):
                    raise SlotTakenError("slot no longer free")
                row.starts_at, row.ends_at = new_start, new_end
                row.guard = guard_range(tenant, new_start, new_end)
                try:
                    await session.flush()
                except IntegrityError as exc:
                    if _is_exclusion_violation(exc):
                        raise SlotTakenError("overlaps a confirmed booking") from exc
                    raise
                if row.calendar_event_id:
                    await self._calendar.move_event(
                        self.calendar_id(tenant),
                        row.calendar_event_id,
                        start=new_start,
                        end=new_end,
                        timezone=tenant.timezone,
                    )
                return _out(tenant, row)
            finally:
                await self._holds.release(tenant, req.call_id)

    async def cancel(self, tenant: TenantConfig, code: str) -> BookingOut:
        async with transaction(self._sessions) as session:
            row = await self._get(session, tenant, code)
            if row.status == BookingStatus.CANCELLED.value:
                return _out(tenant, row)
            row.status = BookingStatus.CANCELLED.value
            await session.flush()
            if row.calendar_event_id:
                await self._calendar.cancel_event(self.calendar_id(tenant), row.calendar_event_id)
            return _out(tenant, row)

    async def add_message(self, tenant: TenantConfig, req: MessageCreate) -> str:
        async with transaction(self._sessions) as session:
            row = Message(
                tenant_id=await self._tenant_id(tenant),
                call_id=req.call_id,
                kind=req.kind.value,
                caller_name=req.caller_name,
                caller_phone=req.phone,
                body=req.body,
            )
            session.add(row)
            await session.flush()
            return str(row.id)
