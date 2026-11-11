"""Owner-facing routes: sign-up (onboarding), login, and the dashboard.

Every dashboard query is scoped by `owner.tenant_id` from the verified JWT. No route accepts a
tenant id or slug from the client, so there is nothing to tamper with: asking for another
tenant's call by id simply finds nothing (404).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr, Field

from receptionist_api.auth import Owner, OwnerDep, create_token, verify_password
from receptionist_api.calls import CallNotFoundError, CallRecords, since_days
from receptionist_api.db.models import Booking, Call, Message, ToolCallRow, Turn
from receptionist_api.tenants import EmailTakenError, TenantExistsError, TenantStore
from receptionist_shared.tenant import TenantConfig

auth_router = APIRouter(prefix="/v1", tags=["owners"])
dashboard = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "detail": detail})


def _store(request: Request) -> TenantStore:
    store: TenantStore = request.app.state.resources.store
    return store


def _calls(request: Request) -> CallRecords:
    records: CallRecords = request.app.state.resources.calls
    return records


def _secret(request: Request) -> str:
    secret: str | None = request.app.state.resources.jwt_secret
    if secret is None:
        raise _error(503, "jwt_not_configured", "set JWT_SECRET")
    return secret


StoreDep = Annotated[TenantStore, Depends(_store)]
CallsDep = Annotated[CallRecords, Depends(_calls)]


# ---------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth token type, not a secret
    tenant_slug: str


class OnboardingRequest(BaseModel):
    tenant: TenantConfig
    owner_email: EmailStr
    password: str = Field(min_length=10, max_length=200)


@auth_router.post("/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, store: StoreDep, request: Request) -> TokenResponse:
    found = await store.find_user(req.email)
    user, slug = found if found else (None, "")
    if not verify_password(user.password_hash if user else None, req.password) or user is None:
        raise _error(401, "bad_credentials", "wrong email or password")
    owner = Owner(user.id, user.tenant_id, slug, user.email)
    return TokenResponse(access_token=create_token(owner, _secret(request)), tenant_slug=slug)


@auth_router.post("/onboarding", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def onboarding(req: OnboardingRequest, store: StoreDep, request: Request) -> TokenResponse:
    """Create a business (validated config) and its owner account; returns a session token."""
    secret = _secret(request)
    try:
        tenant_id, user_id = await store.create(
            req.tenant, owner_email=str(req.owner_email), password=req.password
        )
    except TenantExistsError as exc:
        raise _error(409, "tenant_exists", f"slug {req.tenant.slug!r} is taken") from exc
    except EmailTakenError as exc:
        raise _error(409, "email_taken", "an account with that email exists") from exc
    owner = Owner(user_id, tenant_id, req.tenant.slug, str(req.owner_email).lower())
    return TokenResponse(access_token=create_token(owner, secret), tenant_slug=req.tenant.slug)


# ---------------------------------------------------------------- dashboard models


class Me(BaseModel):
    email: str
    tenant_slug: str
    business_name: str


class CallItem(BaseModel):
    id: uuid.UUID
    call_id: str
    channel: str
    caller_phone: str | None
    started_at: datetime
    ended_at: datetime | None
    duration_secs: int | None
    status: str
    outcome: str | None
    intent: str | None
    sentiment: str | None
    summary: str | None
    language: str | None


class TurnOut(BaseModel):
    idx: int
    speaker: str
    text: str
    interrupted: bool
    timings: dict[str, Any] | None
    spoken_at: datetime


class ToolCallOut(BaseModel):
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    ok: bool
    duration_ms: int
    called_at: datetime


class CallDetailOut(CallItem):
    turns: list[TurnOut]
    tool_calls: list[ToolCallOut]
    has_recording: bool


class BookingItem(BaseModel):
    code: str
    service: str
    customer_name: str
    customer_phone: str
    start: datetime
    end: datetime
    status: str
    call_id: str | None


class MessageItem(BaseModel):
    id: uuid.UUID
    kind: str
    caller_name: str | None
    caller_phone: str | None
    body: str
    status: str
    created_at: datetime
    call_id: str | None


class MessageUpdate(BaseModel):
    status: Literal["new", "done"]


class StageStats(BaseModel):
    n: int
    p50: float | None
    p95: float | None


class Stats(BaseModel):
    days: int
    calls: int
    by_outcome: dict[str, int]
    latency_ms: dict[str, StageStats]


def _call_item(c: Call) -> CallItem:
    duration = int((c.ended_at - c.started_at).total_seconds()) if c.ended_at else None
    return CallItem(
        id=c.id,
        call_id=c.call_id,
        channel=c.channel,
        caller_phone=c.caller_phone,
        started_at=c.started_at,
        ended_at=c.ended_at,
        duration_secs=duration,
        status=c.status,
        outcome=c.outcome,
        intent=c.intent,
        sentiment=c.sentiment,
        summary=c.summary,
        language=c.language,
    )


def _turn(t: Turn) -> TurnOut:
    return TurnOut(
        idx=t.idx,
        speaker=t.speaker,
        text=t.text,
        interrupted=t.interrupted,
        timings=t.timings,
        spoken_at=t.spoken_at,
    )


def _tool(t: ToolCallRow) -> ToolCallOut:
    return ToolCallOut(
        name=t.name,
        arguments=t.arguments,
        result=t.result,
        ok=t.ok,
        duration_ms=t.duration_ms,
        called_at=t.called_at,
    )


def _booking(b: Booking) -> BookingItem:
    return BookingItem(
        code=b.code,
        service=b.service,
        customer_name=b.customer_name,
        customer_phone=b.customer_phone,
        start=b.starts_at,
        end=b.ends_at,
        status=b.status,
        call_id=b.call_id,
    )


def _message(m: Message) -> MessageItem:
    return MessageItem(
        id=m.id,
        kind=m.kind,
        caller_name=m.caller_name,
        caller_phone=m.caller_phone,
        body=m.body,
        status=m.status,
        created_at=m.created_at,
        call_id=m.call_id,
    )


# ---------------------------------------------------------------- dashboard routes


@dashboard.get("/me", response_model=Me)
async def me(owner: OwnerDep, store: StoreDep) -> Me:
    tenant = store.registry.get(owner.tenant_slug)
    return Me(
        email=owner.email,
        tenant_slug=owner.tenant_slug,
        business_name=tenant.business_name if tenant else owner.tenant_slug,
    )


@dashboard.get("/calls", response_model=list[CallItem])
async def list_calls(
    owner: OwnerDep,
    calls: CallsDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: datetime | None = None,
) -> list[CallItem]:
    return [
        _call_item(c) for c in await calls.list_calls(owner.tenant_id, limit=limit, before=before)
    ]


@dashboard.get("/calls/{call_pk}", response_model=CallDetailOut)
async def call_detail(call_pk: uuid.UUID, owner: OwnerDep, calls: CallsDep) -> CallDetailOut:
    try:
        d = await calls.detail(owner.tenant_id, call_pk)
    except CallNotFoundError as exc:
        raise _error(404, "call_not_found", str(call_pk)) from exc
    return CallDetailOut(
        **_call_item(d.call).model_dump(),
        turns=[_turn(t) for t in d.turns],
        tool_calls=[_tool(t) for t in d.tool_calls],
        has_recording=d.call.recording_path is not None,
    )


@dashboard.get("/bookings", response_model=list[BookingItem])
async def list_bookings(
    owner: OwnerDep, calls: CallsDep, upcoming: bool = True
) -> list[BookingItem]:
    start = datetime.now(UTC) if upcoming else None
    return [_booking(b) for b in await calls.bookings(owner.tenant_id, start=start)]


@dashboard.get("/messages", response_model=list[MessageItem])
async def list_messages(
    owner: OwnerDep, calls: CallsDep, status: Literal["new", "done"] | None = None
) -> list[MessageItem]:
    return [_message(m) for m in await calls.messages(owner.tenant_id, status=status)]


@dashboard.patch("/messages/{message_id}", response_model=MessageItem)
async def update_message(
    message_id: uuid.UUID, req: MessageUpdate, owner: OwnerDep, calls: CallsDep
) -> MessageItem:
    try:
        row = await calls.set_message_status(owner.tenant_id, message_id, req.status)
    except CallNotFoundError as exc:
        raise _error(404, "message_not_found", str(message_id)) from exc
    return _message(row)


@dashboard.get("/stats", response_model=Stats)
async def stats(
    owner: OwnerDep, calls: CallsDep, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> Stats:
    since = since_days(datetime.now(UTC), days)
    counts = await calls.counts(owner.tenant_id, since=since)
    latency = await calls.latency_stats(owner.tenant_id, since=since)
    return Stats(
        days=days,
        calls=counts["total"],
        by_outcome=counts["by_outcome"],
        latency_ms={k: StageStats(**v) for k, v in latency.items()},
    )


@dashboard.get("/settings", response_model=TenantConfig)
async def get_settings_(owner: OwnerDep, store: StoreDep) -> TenantConfig:
    tenant = store.registry.get(owner.tenant_slug)
    if tenant is None:
        raise _error(404, "tenant_not_found", owner.tenant_slug)
    return tenant


@dashboard.put("/settings", response_model=TenantConfig)
async def put_settings(config: TenantConfig, owner: OwnerDep, store: StoreDep) -> TenantConfig:
    if config.slug != owner.tenant_slug:
        raise _error(403, "forbidden", "you can only edit your own business")
    await store.update(owner.tenant_id, config)
    return config


__all__ = ["auth_router", "dashboard"]
