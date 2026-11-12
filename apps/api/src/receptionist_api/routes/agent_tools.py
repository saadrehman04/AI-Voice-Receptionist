"""Endpoints the voice agent's tools call. Every route is scoped to one tenant by slug and
requires the shared agent token (`X-Agent-Token`).

Errors use stable codes so the agent can react: `tenant_not_found` (404), `unknown_service`
(422), `slot_taken` (409), `booking_not_found` (404), `calendar_unavailable` (503).
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from receptionist_api.booking_service import (
    BookingService,
    NotFoundError,
    SlotTakenError,
    UnknownServiceError,
)
from receptionist_api.calendar import CalendarError
from receptionist_api.calls import CallNotFoundError, CallRecords
from receptionist_api.jobs import JobQueue
from receptionist_api.knowledge_base import KnowledgeBase
from receptionist_api.tenants import TenantRegistry
from receptionist_api.worker import SUMMARIZE_CALL
from receptionist_shared.api_models import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookingCreate,
    BookingOut,
    CallEnd,
    CallStart,
    CancelRequest,
    HoldRequest,
    HoldResponse,
    LookupRequest,
    LookupResponse,
    MessageCreate,
    MessageOut,
    RescheduleRequest,
    ToolCallIn,
    TurnIn,
)
from receptionist_shared.phone import normalize_pk_phone
from receptionist_shared.tenant import TenantConfig


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "detail": detail})


async def require_agent_token(
    request: Request, x_agent_token: Annotated[str | None, Header()] = None
) -> None:
    expected = request.app.state.resources.agent_token
    if expected is None:
        # Fail closed: an api with no token configured accepts no tool calls at all.
        raise _error(503, "agent_token_not_configured", "set AGENT_API_TOKEN in .env")
    if x_agent_token is None or not hmac.compare_digest(x_agent_token, expected):
        raise _error(401, "unauthorized", "missing or wrong X-Agent-Token")


router = APIRouter(
    prefix="/v1/tenants/{slug}", tags=["agent tools"], dependencies=[Depends(require_agent_token)]
)


def get_tenant(slug: str, request: Request) -> TenantConfig:
    registry: TenantRegistry = request.app.state.resources.tenants
    tenant = registry.get(slug)
    if tenant is None:
        raise _error(404, "tenant_not_found", f"no tenant {slug!r}")
    return tenant


def get_bookings(request: Request) -> BookingService:
    service: BookingService = request.app.state.resources.bookings
    return service


def get_kb(request: Request) -> KnowledgeBase:
    kb: KnowledgeBase = request.app.state.resources.kb
    return kb


TenantDep = Annotated[TenantConfig, Depends(get_tenant)]
BookingsDep = Annotated[BookingService, Depends(get_bookings)]


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, SlotTakenError):
        return _error(409, "slot_taken", str(exc))
    if isinstance(exc, UnknownServiceError):
        return _error(422, "unknown_service", f"unknown service {exc}")
    if isinstance(exc, NotFoundError):
        return _error(404, "booking_not_found", f"no booking {exc}")
    if isinstance(exc, CalendarError):
        return _error(503, "calendar_unavailable", "calendar could not be reached")
    raise exc


@router.post("/availability", response_model=AvailabilityResponse)
async def availability(
    req: AvailabilityRequest, tenant: TenantDep, bookings: BookingsDep
) -> AvailabilityResponse:
    try:
        return await bookings.availability(
            tenant, req.service, req.day, req.preferred_time, req.call_id
        )
    except (UnknownServiceError, CalendarError) as exc:
        raise _translate(exc) from exc


@router.post("/holds", response_model=HoldResponse)
async def hold(req: HoldRequest, tenant: TenantDep, bookings: BookingsDep) -> HoldResponse:
    try:
        held = await bookings.hold(tenant, req.service, req.start, req.call_id)
    except (UnknownServiceError, CalendarError) as exc:
        raise _translate(exc) from exc
    return HoldResponse(held=held, expires_in_secs=bookings.hold_secs if held else 0)


@router.post("/bookings", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
async def create_booking(
    req: BookingCreate, tenant: TenantDep, bookings: BookingsDep
) -> BookingOut:
    try:
        return await bookings.create(tenant, req)
    except (SlotTakenError, UnknownServiceError, CalendarError) as exc:
        raise _translate(exc) from exc


@router.get("/bookings", response_model=list[BookingOut])
async def find_bookings(phone: str, tenant: TenantDep, bookings: BookingsDep) -> list[BookingOut]:
    normalized = normalize_pk_phone(phone)
    if normalized is None:
        raise _error(422, "invalid_phone", "phone must be a Pakistani number")
    return await bookings.find(tenant, normalized)


@router.post("/bookings/{code}/reschedule", response_model=BookingOut)
async def reschedule_booking(
    code: str, req: RescheduleRequest, tenant: TenantDep, bookings: BookingsDep
) -> BookingOut:
    try:
        return await bookings.reschedule(tenant, code.upper(), req)
    except (SlotTakenError, NotFoundError, CalendarError) as exc:
        raise _translate(exc) from exc


@router.post("/bookings/{code}/cancel", response_model=BookingOut)
async def cancel_booking(
    code: str, req: CancelRequest, tenant: TenantDep, bookings: BookingsDep
) -> BookingOut:
    try:
        return await bookings.cancel(tenant, code.upper())
    except (NotFoundError, CalendarError) as exc:
        raise _translate(exc) from exc


@router.post("/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
async def create_message(
    req: MessageCreate, tenant: TenantDep, bookings: BookingsDep
) -> MessageOut:
    return MessageOut(id=await bookings.add_message(tenant, req))


@router.post("/knowledge/lookup", response_model=LookupResponse)
async def knowledge_lookup(
    req: LookupRequest, tenant: TenantDep, kb: Annotated[KnowledgeBase, Depends(get_kb)]
) -> LookupResponse:
    result = await kb.lookup(tenant, req.question, req.language)
    return LookupResponse(found=result.found, answer=result.answer, sources=result.sources)


# ---------------------------------------------------------------- tenant config + call records


@router.get("/config", response_model=TenantConfig)
async def tenant_config(tenant: TenantDep) -> TenantConfig:
    """The tenant's current settings, so the agent can serve tenants created via onboarding."""
    return tenant


def _tenant_id(request: Request, tenant: TenantConfig) -> uuid.UUID:
    tenant_id = request.app.state.resources.tenants.id_of(tenant.slug)
    if tenant_id is None:
        raise _error(404, "tenant_not_found", f"no tenant {tenant.slug!r}")
    return tenant_id  # type: ignore[no-any-return]


def get_calls(request: Request) -> CallRecords:
    records: CallRecords = request.app.state.resources.calls
    return records


CallsDep = Annotated[CallRecords, Depends(get_calls)]


@router.post("/calls", status_code=status.HTTP_201_CREATED)
async def start_call(
    req: CallStart, tenant: TenantDep, calls: CallsDep, request: Request
) -> dict[str, str]:
    pk = await calls.start(_tenant_id(request, tenant), req)
    return {"id": str(pk)}


@router.post("/calls/{call_id}/turns", status_code=status.HTTP_201_CREATED)
async def add_turn(
    call_id: str, req: TurnIn, tenant: TenantDep, calls: CallsDep, request: Request
) -> dict[str, bool]:
    try:
        await calls.add_turn(_tenant_id(request, tenant), call_id, req)
    except CallNotFoundError as exc:
        raise _error(404, "call_not_found", call_id) from exc
    return {"ok": True}


@router.post("/calls/{call_id}/tool-calls", status_code=status.HTTP_201_CREATED)
async def add_tool_call(
    call_id: str, req: ToolCallIn, tenant: TenantDep, calls: CallsDep, request: Request
) -> dict[str, bool]:
    try:
        await calls.add_tool_call(_tenant_id(request, tenant), call_id, req)
    except CallNotFoundError as exc:
        raise _error(404, "call_not_found", call_id) from exc
    return {"ok": True}


@router.post("/calls/{call_id}/end")
async def end_call(
    call_id: str, req: CallEnd, tenant: TenantDep, calls: CallsDep, request: Request
) -> dict[str, str]:
    tenant_id = _tenant_id(request, tenant)
    try:
        pk = await calls.end(tenant_id, call_id, req)
    except CallNotFoundError as exc:
        raise _error(404, "call_not_found", call_id) from exc
    queue: JobQueue = request.app.state.resources.queue
    job_id = await queue.enqueue(SUMMARIZE_CALL, {"tenant_id": str(tenant_id), "call_pk": str(pk)})
    return {"id": str(pk), "job_id": job_id}


# Not under /tenants/{slug}: an incoming WhatsApp call only knows which number was called.
whatsapp_router = APIRouter(
    prefix="/v1/whatsapp", tags=["agent tools"], dependencies=[Depends(require_agent_token)]
)


class WhatsAppNumberOwner(BaseModel):
    tenant: str


@whatsapp_router.get("/numbers/{phone_number_id}", response_model=WhatsAppNumberOwner)
async def whatsapp_number_owner(phone_number_id: str, request: Request) -> WhatsAppNumberOwner:
    """Which business owns the WhatsApp number that was called (Phase 8)."""
    registry: TenantRegistry = request.app.state.resources.tenants
    tenant = registry.by_whatsapp_number(phone_number_id)
    if tenant is None:
        raise _error(404, "number_not_found", "no business uses that WhatsApp number")
    return WhatsAppNumberOwner(tenant=tenant.slug)
