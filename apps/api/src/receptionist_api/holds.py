"""Short-lived slot holds in Redis, so no one else takes a slot while it is read back.

A booking (plus buffer) covers several 15-minute buckets; a hold claims all of them at once with
a Lua script (all-or-nothing, atomic in Redis). Each bucket key stores the owning call_id and
expires on its own (`hold_secs`, default 2 minutes), so a dropped call never blocks a slot for
long. A call holds at most one slot: a new hold releases the call's previous one.

Keys:
  hold:{tenant}:{bucket ISO}   -> call_id   (PX = hold time)
  holds-of:{tenant}:{call_id}  -> set of the bucket keys this call holds
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from redis.asyncio import Redis

from receptionist_shared.availability import HOLD_BUCKET_MIN, Interval, hold_buckets
from receptionist_shared.tenant import TenantConfig

# KEYS = bucket keys; ARGV[1] = call_id, ARGV[2] = ttl ms, ARGV[3] = owner-set key
_ACQUIRE = """
for i, key in ipairs(KEYS) do
  local owner = redis.call('GET', key)
  if owner and owner ~= ARGV[1] then
    return 0
  end
end
local previous = redis.call('SMEMBERS', ARGV[3])
for i, key in ipairs(previous) do
  if redis.call('GET', key) == ARGV[1] then
    redis.call('DEL', key)
  end
end
redis.call('DEL', ARGV[3])
for i, key in ipairs(KEYS) do
  redis.call('SET', key, ARGV[1], 'PX', ARGV[2])
  redis.call('SADD', ARGV[3], key)
end
redis.call('PEXPIRE', ARGV[3], ARGV[2])
return 1
"""

# KEYS[1] = owner-set key; ARGV[1] = call_id
_RELEASE = """
local keys = redis.call('SMEMBERS', KEYS[1])
for i, key in ipairs(keys) do
  if redis.call('GET', key) == ARGV[1] then
    redis.call('DEL', key)
  end
end
redis.call('DEL', KEYS[1])
return #keys
"""


class SlotHolds:
    def __init__(self, redis: Redis, *, hold_secs: int = 120) -> None:
        self._redis = redis
        self.hold_secs = hold_secs
        self._acquire = redis.register_script(_ACQUIRE)
        self._release = redis.register_script(_RELEASE)

    @staticmethod
    def _key(tenant: TenantConfig, bucket: datetime) -> str:
        return f"hold:{tenant.slug}:{bucket.astimezone(tenant.tz).isoformat()}"

    @staticmethod
    def _owner_key(tenant: TenantConfig, call_id: str) -> str:
        return f"holds-of:{tenant.slug}:{call_id}"

    async def acquire(
        self, tenant: TenantConfig, start: datetime, end: datetime, call_id: str
    ) -> bool:
        keys = [self._key(tenant, b) for b in hold_buckets(tenant, start, end)]
        result = await self._acquire(
            keys=keys,
            args=[call_id, self.hold_secs * 1000, self._owner_key(tenant, call_id)],
        )
        return bool(result)

    async def owns(
        self, tenant: TenantConfig, start: datetime, end: datetime, call_id: str
    ) -> bool:
        keys = [self._key(tenant, b) for b in hold_buckets(tenant, start, end)]
        owners = await self._redis.mget(keys)
        return all(o is not None and _text(o) == call_id for o in owners)

    async def release(self, tenant: TenantConfig, call_id: str) -> None:
        await self._release(keys=[self._owner_key(tenant, call_id)], args=[call_id])

    async def held_by_others(self, tenant: TenantConfig, day: date, call_id: str) -> list[Interval]:
        """Buckets on `day` held by other calls, as busy intervals for availability."""
        pattern = f"hold:{tenant.slug}:{day.isoformat()}T*"
        width = timedelta(minutes=HOLD_BUCKET_MIN)
        busy: list[Interval] = []
        async for raw_key in self._redis.scan_iter(match=pattern, count=200):
            key = _text(raw_key)
            owner = await self._redis.get(key)
            if owner is None or _text(owner) == call_id:
                continue
            bucket = datetime.fromisoformat(key.split(":", 2)[2])
            busy.append(Interval(bucket, bucket + width))
        return busy


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value
