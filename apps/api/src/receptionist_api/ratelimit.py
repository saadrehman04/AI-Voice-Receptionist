"""Fixed-window rate limits in Redis, for public endpoints that cost us something.

`POST /v1/onboarding/draft` fetches a stranger's URL and spends free-tier LLM tokens, so it gets
a cap per client address. In dev the dashboard's server calls the API, so every visitor shares
one address and the cap is effectively global: that is fine, it protects the daily LLM quota.
"""

from __future__ import annotations

from redis.asyncio import Redis


class RateLimiter:
    def __init__(self, redis: Redis, *, prefix: str, limit: int, window_secs: int) -> None:
        self._redis = redis
        self._prefix = prefix
        self.limit = limit
        self.window_secs = window_secs

    async def allow(self, key: str) -> bool:
        """Count one request for `key`; False once the window's limit is used up."""
        name = f"ratelimit:{self._prefix}:{key}"
        count = int(await self._redis.incr(name))
        if count == 1:
            await self._redis.expire(name, self.window_secs)
        return count <= self.limit


__all__ = ["RateLimiter"]
