"""FastAPI app. Run with `make api` (uvicorn, auto-reload in dev).

Resources (DB, Redis, calendar, knowledge base, tenants) are created once in the lifespan and
kept on `app.state.resources`. Tests pass their own `Resources` to `create_app` instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis

from receptionist_api.booking_service import BookingService
from receptionist_api.calendar import (
    Calendar,
    FakeCalendar,
    GoogleCalendar,
    ServiceAccountTokens,
)
from receptionist_api.calls import CallRecords
from receptionist_api.db.session import make_engine, make_sessionmaker
from receptionist_api.holds import SlotHolds
from receptionist_api.jobs import JobQueue
from receptionist_api.knowledge_base import KnowledgeBase, make_embedder
from receptionist_api.ratelimit import RateLimiter
from receptionist_api.routes import agent_tools, owners, setup
from receptionist_api.tenants import TenantRegistry, TenantStore
from receptionist_api.usage import Limits, UsageService
from receptionist_api.website_draft import (
    Drafter,
    LlmDrafter,
    LlmEndpoint,
    gemini_endpoint,
    groq_endpoint,
)
from receptionist_shared import Settings, get_settings
from receptionist_shared.logging import configure_logging
from receptionist_shared.tenant import list_tenants


class Health(BaseModel):
    status: Literal["ok"]
    environment: str
    version: str


@dataclass
class Resources:
    tenants: TenantRegistry
    store: TenantStore
    bookings: BookingService
    calls: CallRecords
    queue: JobQueue
    kb: KnowledgeBase
    agent_token: str | None
    jwt_secret: str | None
    closers: list[Callable[[], Awaitable[object]]] = field(default_factory=list)
    # Phase 7 (onboarding and the dashboard); tests set what they exercise.
    calendar: Calendar = field(default_factory=FakeCalendar)
    calendar_account: str | None = None  # the service account a business shares with
    drafter: Drafter | None = None  # None = drafting from a website is switched off
    draft_limiter: RateLimiter | None = None
    web_client: httpx.AsyncClient | None = None  # fetches owners' websites
    storage_dir: Path = Path("storage")
    # Phase 9: per-business limits (None = no limits, e.g. in tests that don't exercise them).
    usage: UsageService | None = None
    booking_limiter: RateLimiter | None = None
    message_limiter: RateLimiter | None = None

    async def close(self) -> None:
        for close in reversed(self.closers):
            try:
                await close()
            except Exception as exc:  # shutting down: log, keep closing the rest
                logger.warning("error during shutdown", error=str(exc))


Closers = list[Callable[[], Awaitable[object]]]


def make_calendar(settings: Settings) -> tuple[Calendar, str | None, Closers]:
    """The calendar, the account businesses share their calendar with, and its closers."""
    if settings.calendar_provider == "google":
        if not settings.google_service_account_file:
            raise RuntimeError("CALENDAR_PROVIDER=google needs GOOGLE_SERVICE_ACCOUNT_FILE")
        tokens = ServiceAccountTokens(Path(settings.google_service_account_file))
        google = GoogleCalendar(tokens.token)
        return google, tokens.email, [google.aclose]
    return FakeCalendar(), None, []


def draft_endpoints(settings: Settings) -> list[LlmEndpoint]:
    """Groq first, Gemini as the backup; empty when neither key is set."""
    out: list[LlmEndpoint] = []
    if settings.groq_api_key:
        out.append(
            groq_endpoint(
                settings.groq_api_key.get_secret_value(),
                settings.groq_llm_model,
                settings.groq_reasoning_effort,
            )
        )
    if settings.gemini_api_key:
        key = settings.gemini_api_key.get_secret_value()
        out.append(gemini_endpoint(key, settings.gemini_model))
    return out


async def build_resources(settings: Settings) -> Resources:
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url)
    calendar, calendar_account, closers = make_calendar(settings)
    bookings = BookingService(
        sessions,
        SlotHolds(redis, hold_secs=settings.hold_secs),
        calendar,
        cache_secs=settings.availability_cache_secs,
    )
    tenants = TenantRegistry()
    store = TenantStore(sessions, tenants)
    await store.seed(list_tenants(Path(settings.tenants_dir)))
    await store.load()
    embedder = await asyncio.to_thread(
        make_embedder, settings.embeddings_enabled, settings.embeddings_model
    )
    token = settings.agent_api_token.get_secret_value() if settings.agent_api_token else None
    if token is None:
        logger.warning("AGENT_API_TOKEN is not set: tool endpoints will refuse every request")
    jwt_secret = settings.jwt_secret.get_secret_value() if settings.jwt_secret else None
    if jwt_secret is None:
        logger.warning("JWT_SECRET is not set: owner login and the dashboard are disabled")
    logger.info(
        "api ready",
        tenants=[t.slug for t in tenants.all()],
        calendar=settings.calendar_provider,
        semantic_kb=embedder is not None,
    )
    web_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        headers={"User-Agent": "VoiceReceptionistOnboarding/0.1"},
    )
    endpoints = draft_endpoints(settings)
    return Resources(
        tenants=tenants,
        store=store,
        bookings=bookings,
        calls=CallRecords(sessions),
        queue=JobQueue(redis, "default"),
        kb=KnowledgeBase(Path(settings.kb_dir), embedder),
        agent_token=token,
        jwt_secret=jwt_secret,
        closers=[*closers, web_client.aclose, redis.aclose, engine.dispose],
        calendar=calendar,
        calendar_account=calendar_account,
        drafter=LlmDrafter(endpoints, web_client) if endpoints else None,
        draft_limiter=RateLimiter(redis, prefix="draft", limit=20, window_secs=3600),
        usage=UsageService(
            sessions,
            redis,
            Limits(settings.tenant_calls_per_hour, settings.tenant_monthly_minutes),
        ),
        booking_limiter=RateLimiter(
            redis, prefix="bookings", limit=settings.tenant_bookings_per_hour, window_secs=3600
        ),
        message_limiter=RateLimiter(
            redis, prefix="messages", limit=settings.tenant_messages_per_hour, window_secs=3600
        ),
        web_client=web_client,
        storage_dir=Path(settings.storage_dir),
    )


def create_app(resources: Resources | None = None) -> FastAPI:
    """Build the app. Pass `resources` (tests) to skip connecting to real services."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resources is None:
            # Also runs in uvicorn's reload worker process, which doesn't inherit __main__'s setup.
            settings = get_settings()
            configure_logging(settings.log_level, json_logs=settings.log_json)
        app.state.resources = resources or await build_resources(get_settings())
        try:
            yield
        finally:
            if resources is None:
                await app.state.resources.close()

    app = FastAPI(title="Voice Receptionist API", version="0.1.0", lifespan=lifespan)
    if resources is not None:
        app.state.resources = resources  # usable even without running the lifespan

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        """Liveness check: the process is up. (DB/Redis readiness comes in Phase 4.)"""
        return Health(status="ok", environment=get_settings().environment, version=app.version)

    app.include_router(agent_tools.router)
    app.include_router(agent_tools.whatsapp_router)
    app.include_router(owners.auth_router)
    app.include_router(owners.dashboard)
    app.include_router(setup.onboarding)
    app.include_router(setup.calendar)
    return app


app = create_app()
