"""Per-business usage counters and call limits (Phase 9, DECISIONS D38).

Two limits are checked when a call starts (`admit`):

- calls per hour: a Redis counter per business per clock hour. It stops a flood (a script
  dialling the number, a viral post) from spending the whole free LLM quota in an afternoon.
- minutes per month: summed from the call records in Postgres (ended calls, the business's
  own month), the durable number a plan would be billed on.

Each business gets the defaults from Settings unless its tenant row sets its own. A refused
call is not dropped: the agent tells the caller who to call instead (the business's hand-off
contact) and hangs up politely. If Redis or Postgres can't answer, the call is let through:
a live caller matters more than a limit.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist_api.db.models import Call, Tenant
from receptionist_shared.api_models import Admission

HOUR_KEY_TTL_SECS = 2 * 3600


@dataclass(frozen=True)
class Limits:
    calls_per_hour: int
    monthly_minutes: int


class Usage(BaseModel):
    month: str  # "2026-11", in the business's time zone
    calls: int
    minutes: float
    monthly_minutes_limit: int
    calls_this_hour: int
    calls_per_hour_limit: int


def utcnow() -> datetime:
    return datetime.now(UTC)


def month_start(now: datetime, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class UsageService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: Redis,
        defaults: Limits,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._redis = redis
        self.defaults = defaults
        self._now = clock

    def _hour_key(self, tenant_id: uuid.UUID, now: datetime) -> str:
        return f"usage:calls:{tenant_id}:{now.astimezone(UTC):%Y%m%d%H}"

    async def limits(self, tenant_id: uuid.UUID) -> Limits:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(Tenant.calls_per_hour_limit, Tenant.monthly_minutes_limit).where(
                        Tenant.id == tenant_id
                    )
                )
            ).first()
        per_hour, minutes = row if row is not None else (None, None)
        return Limits(
            calls_per_hour=per_hour or self.defaults.calls_per_hour,
            monthly_minutes=minutes or self.defaults.monthly_minutes,
        )

    async def month_totals(self, tenant_id: uuid.UUID, tz: tzinfo) -> tuple[int, float]:
        """(calls started, minutes of ended calls) this month."""
        since = month_start(self._now(), tz)
        seconds = func.coalesce(func.sum(func.extract("epoch", Call.ended_at - Call.started_at)), 0)
        async with self._sessions() as session:
            calls, secs = (
                await session.execute(
                    select(func.count(), seconds).where(
                        Call.tenant_id == tenant_id, Call.started_at >= since
                    )
                )
            ).one()
        return int(calls), round(float(secs) / 60, 1)

    async def admit(self, tenant_id: uuid.UUID, timezone: str) -> Admission:
        """May this business take one more call now? Counts it if so."""
        try:
            limits = await self.limits(tenant_id)
            _calls, minutes = await self.month_totals(tenant_id, ZoneInfo(timezone))
            if minutes >= limits.monthly_minutes:
                return Admission(allowed=False, reason="monthly_minutes")
            key = self._hour_key(tenant_id, self._now())
            count = int(await self._redis.incr(key))
            if count == 1:
                await self._redis.expire(key, HOUR_KEY_TTL_SECS)
            if count > limits.calls_per_hour:
                await self._redis.decr(key)  # count calls taken, not calls refused
                return Admission(allowed=False, reason="calls_per_hour")
        except Exception as exc:  # fail open: a limit is not worth a dropped caller
            logger.warning("usage check failed; letting the call through", error=str(exc))
            return Admission(allowed=True, reason=None)
        return Admission(allowed=True, reason=None)

    async def usage(self, tenant_id: uuid.UUID, timezone: str) -> Usage:
        tz = ZoneInfo(timezone)
        now = self._now()
        limits = await self.limits(tenant_id)
        calls, minutes = await self.month_totals(tenant_id, tz)
        this_hour = await self._redis.get(self._hour_key(tenant_id, now))
        return Usage(
            month=f"{now.astimezone(tz):%Y-%m}",
            calls=calls,
            minutes=minutes,
            monthly_minutes_limit=limits.monthly_minutes,
            calls_this_hour=int(this_hour or 0),
            calls_per_hour_limit=limits.calls_per_hour,
        )


__all__ = ["Limits", "Usage", "UsageService", "month_start"]
