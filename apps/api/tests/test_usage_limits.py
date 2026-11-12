"""Phase 9 per-business limits and usage counters (DECISIONS D38), on real Postgres + Redis."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import update

from receptionist_api.db.models import Tenant
from receptionist_api.main import Resources
from receptionist_api.ratelimit import RateLimiter
from receptionist_api.testing import bearer, onboard
from receptionist_api.usage import Limits, UsageService, month_start

SALON = "/v1/tenants/demo-salon"
WORKSHOP = "/v1/tenants/demo-workshop"
NOW = datetime(2026, 11, 20, 10, 30, tzinfo=UTC)


@pytest.fixture
def usage(resources: Resources, redis: Redis) -> UsageService:
    service = UsageService(
        resources.bookings._sessions,
        redis,
        Limits(calls_per_hour=2, monthly_minutes=10),
        clock=lambda: NOW,
    )
    resources.usage = service
    return service


async def admit(api: httpx.AsyncClient, base: str = SALON) -> dict[str, object]:
    r = await api.post(f"{base}/calls/admit")
    assert r.status_code == 200, r.text
    return dict(r.json())


async def record_call(
    api: httpx.AsyncClient, call_id: str, minutes: float, base: str = SALON
) -> None:
    start = NOW - timedelta(hours=1)
    r = await api.post(f"{base}/calls", json={"call_id": call_id, "started_at": start.isoformat()})
    assert r.status_code == 201, r.text
    end = start + timedelta(minutes=minutes)
    r = await api.post(f"{base}/calls/{call_id}/end", json={"ended_at": end.isoformat()})
    assert r.status_code == 200, r.text


def tenant_id(resources: Resources, slug: str = "demo-salon") -> uuid.UUID:
    found = resources.tenants.id_of(slug)
    assert found is not None
    return found


async def test_calls_per_hour_refuses_the_extra_call_per_business(
    api: httpx.AsyncClient, usage: UsageService, resources: Resources
) -> None:
    assert await admit(api) == {"allowed": True, "reason": None}
    assert await admit(api) == {"allowed": True, "reason": None}
    assert await admit(api) == {"allowed": False, "reason": "calls_per_hour"}
    # Another business has its own counter.
    assert (await admit(api, WORKSHOP))["allowed"] is True
    # A refused call isn't counted as taken.
    assert (await usage.usage(tenant_id(resources), "Asia/Karachi")).calls_this_hour == 2


async def test_a_business_can_have_its_own_limit(
    api: httpx.AsyncClient, usage: UsageService, resources: Resources
) -> None:
    async with resources.bookings._sessions() as session, session.begin():
        await session.execute(
            update(Tenant).where(Tenant.slug == "demo-salon").values(calls_per_hour_limit=1)
        )
    assert (await admit(api))["allowed"] is True
    assert await admit(api) == {"allowed": False, "reason": "calls_per_hour"}


async def test_monthly_minutes_are_summed_from_call_records(
    api: httpx.AsyncClient, usage: UsageService
) -> None:
    await record_call(api, "c1", minutes=6)
    assert (await admit(api))["allowed"] is True
    await record_call(api, "c2", minutes=4.5)
    assert await admit(api) == {"allowed": False, "reason": "monthly_minutes"}
    assert (await admit(api, WORKSHOP))["allowed"] is True  # the workshop used nothing


async def test_redis_down_lets_the_call_through(resources: Resources) -> None:
    class Down:
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis is down")

    service = UsageService(resources.bookings._sessions, Down(), Limits(1, 1))  # type: ignore[arg-type]
    assert (await service.admit(tenant_id(resources), "Asia/Karachi")).allowed


async def test_owner_sees_this_months_usage_and_only_their_own(
    api: httpx.AsyncClient, usage: UsageService, resources: Resources
) -> None:
    await record_call(api, "c1", minutes=3)
    await record_call(api, "w1", minutes=9, base=WORKSHOP)
    await admit(api)
    token = await onboard(api, "usage-test", "owner@usage.example.com")
    own = await api.get("/v1/dashboard/usage", headers=bearer(token))
    assert own.status_code == 200, own.text
    assert own.json()["calls"] == 0  # a new business: none of the demo tenants' calls
    numbers = (await usage.usage(tenant_id(resources), "Asia/Karachi")).model_dump()
    assert numbers == {
        "month": "2026-11",
        "calls": 1,
        "minutes": 3.0,
        "monthly_minutes_limit": 10,
        "calls_this_hour": 1,
        "calls_per_hour_limit": 2,
    }
    assert (await api.get("/v1/dashboard/usage")).status_code == 401


def test_month_starts_in_the_business_time_zone() -> None:
    from zoneinfo import ZoneInfo

    # 21:00 UTC on 31 Oct is already 1 Nov in Karachi (UTC+5).
    start = month_start(datetime(2026, 10, 31, 21, 0, tzinfo=UTC), ZoneInfo("Asia/Karachi"))
    assert (start.month, start.day, start.hour) == (11, 1, 0)


async def test_bookings_and_messages_are_capped_per_business(
    api: httpx.AsyncClient, resources: Resources, redis: Redis
) -> None:
    resources.message_limiter = RateLimiter(redis, prefix="m-test", limit=1, window_secs=60)
    body = {"kind": "message", "body": "call me", "call_id": "c"}
    assert (await api.post(f"{SALON}/messages", json=body)).status_code == 201
    second = await api.post(f"{SALON}/messages", json=body)
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "rate_limited"
    assert (await api.post(f"{WORKSHOP}/messages", json=body)).status_code == 201

    resources.booking_limiter = RateLimiter(redis, prefix="b-test", limit=0, window_secs=60)
    booking = {
        "service": "Haircut",
        "start": "2026-11-18T11:00:00+05:00",
        "caller_name": "Sara",
        "phone": "+923001234567",
        "call_id": "c",
        "idempotency_key": "key-00001",
    }
    refused = await api.post(f"{SALON}/bookings", json=booking)
    assert refused.status_code == 429, refused.text
