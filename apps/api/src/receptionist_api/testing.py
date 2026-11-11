"""Pytest fixtures for tests that need real Postgres and Redis (docker-compose, or CI services).

Registered for the whole repo by the root conftest.py (`pytest_plugins`), so both the api tests
and the agent's end-to-end tests can use them.

- A separate database, `<name>_test`, is created if missing and migrated with Alembic.
- Redis uses database 15. Tables and keys are wiped before every test.
- If the services are not reachable the tests are skipped, unless REQUIRE_SERVICES=1 (CI),
  in which case they fail: a green CI run must have really exercised the database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg  # type: ignore[import-untyped]
import httpx
import pytest
import yaml
from redis.asyncio import Redis
from sqlalchemy.engine import make_url

from receptionist_api.booking_service import BookingService
from receptionist_api.calendar import FakeCalendar
from receptionist_api.calls import CallRecords
from receptionist_api.db.session import make_engine, make_sessionmaker
from receptionist_api.holds import SlotHolds
from receptionist_api.jobs import JobQueue
from receptionist_api.knowledge_base import KnowledgeBase
from receptionist_api.main import Resources, create_app
from receptionist_api.tenants import TenantRegistry, TenantStore
from receptionist_shared import get_settings
from receptionist_shared.tenant import list_tenants

REPO = Path(__file__).resolve().parents[4]
TOKEN = "test-agent-token"  # noqa: S105 - test-only value, never a real secret
JWT_SECRET = "test-jwt-secret-not-for-production-use-0123456789"  # noqa: S105
REDIS_TEST_DB = 15


def _test_db_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _redis_url() -> str:
    url = os.environ.get("TEST_REDIS_URL") or get_settings().redis_url
    base = url.rsplit("/", 1)[0] if url.count("/") > 2 else url
    return f"{base}/{REDIS_TEST_DB}"


def _unavailable(reason: str) -> None:
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(f"services required but unavailable: {reason}")
    pytest.skip(f"Postgres/Redis not available ({reason}); run `make up`")


async def _ensure_database(url: str) -> None:
    sa_url = make_url(url)
    admin = sa_url.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(admin.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "select 1 from pg_database where datname = $1", sa_url.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{sa_url.database}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = _test_db_url()
    try:
        asyncio.run(asyncio.wait_for(_ensure_database(url), 5))
    except Exception as exc:
        _unavailable(f"postgres: {type(exc).__name__}")
    from alembic import command
    from alembic.config import Config

    os.environ["MIGRATIONS_DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(REPO / "apps/api/alembic.ini")), "head")
    finally:
        os.environ.pop("MIGRATIONS_DATABASE_URL", None)
    yield url


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(_redis_url())
    try:
        await asyncio.wait_for(client.ping(), 3)
    except Exception as exc:
        await client.aclose()
        _unavailable(f"redis: {type(exc).__name__}")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def calendar() -> FakeCalendar:
    return FakeCalendar()


@pytest.fixture
async def bookings(
    database_url: str, redis: Redis, calendar: FakeCalendar, monday_9am: datetime
) -> AsyncIterator[BookingService]:
    engine = make_engine(database_url)
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text(
                "TRUNCATE tool_calls, turns, calls, users, services, bookings, messages, "
                "tenants CASCADE"
            )
        )
    service = BookingService(
        make_sessionmaker(engine),
        SlotHolds(redis, hold_secs=120),
        calendar,
        clock=lambda tenant: monday_9am.astimezone(tenant.tz),
    )
    yield service
    await engine.dispose()


@pytest.fixture
async def resources(bookings: BookingService, redis: Redis) -> Resources:
    registry = TenantRegistry()
    store = TenantStore(bookings._sessions, registry)
    await store.seed(list_tenants(REPO / "tenants"))
    await store.load()
    return Resources(
        tenants=registry,
        store=store,
        bookings=bookings,
        calls=CallRecords(bookings._sessions),
        queue=JobQueue(redis, "test"),
        kb=KnowledgeBase(REPO / "tenants/kb", embedder=None),
        agent_token=TOKEN,
        jwt_secret=JWT_SECRET,
    )


@pytest.fixture
def registry(resources: Resources) -> TenantRegistry:
    return resources.tenants


@pytest.fixture
async def api(resources: Resources) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(resources)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api", headers={"X-Agent-Token": TOKEN}
    ) as client:
        yield client


# ---------------------------------------------------------------- owner helpers

PASSWORD = "a-long-enough-password"  # noqa: S105 - test-only value


def tenant_payload(slug: str, name: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO / "tenants/demo-salon.yaml").read_text(encoding="utf-8"))
    data.update(slug=slug, business_name=name)
    return dict(data)


async def onboard(api: httpx.AsyncClient, slug: str, email: str) -> str:
    r = await api.post(
        "/v1/onboarding",
        json={
            "tenant": tenant_payload(slug, slug.title()),
            "owner_email": email,
            "password": PASSWORD,
        },
    )
    if r.status_code != 201:
        raise AssertionError(r.text)
    return str(r.json()["access_token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
