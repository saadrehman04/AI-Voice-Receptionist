"""A small, reliable Redis job queue for background work (post-call summaries).

Why not arq/saq: both pin the Redis client to an older major version than the one the api uses.
The pattern here is the standard "reliable queue":

- `enqueue` LPUSHes a JSON job onto `jobs:<queue>`.
- A worker BLMOVEs it atomically onto `jobs:<queue>:processing`, runs it, then LREMs it (ack).
- If the handler raises, the job is retried (up to `max_attempts`) and finally moved to
  `jobs:<queue>:dead` for inspection. Nothing is silently dropped.
- If a worker dies mid-job, the job is still in `processing`; `recover()` (run at worker start)
  puts such jobs back on the queue. Jobs must therefore be idempotent (ours are).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from redis.asyncio import Redis

Handler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Job:
    name: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    attempts: int = 0

    def dumps(self) -> str:
        return json.dumps(
            {"id": self.id, "name": self.name, "payload": self.payload, "attempts": self.attempts}
        )

    @classmethod
    def loads(cls, raw: bytes | str) -> Job:
        data = json.loads(raw)
        return cls(data["name"], data["payload"], data["id"], data["attempts"])


class JobQueue:
    def __init__(self, redis: Redis, name: str = "default", *, max_attempts: int = 3) -> None:
        self._redis = redis
        self.key = f"jobs:{name}"
        self.processing = f"{self.key}:processing"
        self.dead = f"{self.key}:dead"
        self.max_attempts = max_attempts

    async def enqueue(self, name: str, payload: dict[str, Any]) -> str:
        job = Job(name, payload)
        await self._redis.lpush(self.key, job.dumps())
        return job.id

    async def recover(self) -> int:
        """Requeue jobs a dead worker left in `processing`. Call before starting workers."""
        moved = 0
        while await self._redis.lmove(self.processing, self.key, "RIGHT", "LEFT"):
            moved += 1
        if moved:
            logger.warning("requeued unfinished jobs", count=moved, queue=self.key)
        return moved

    async def run_one(self, handlers: dict[str, Handler], *, wait_secs: int = 5) -> bool:
        """Take one job and run it. Returns False if the queue stayed empty for `wait_secs`."""
        popped = await self._redis.blmove(self.key, self.processing, wait_secs, "RIGHT", "LEFT")
        if popped is None:
            return False
        raw = popped.decode() if isinstance(popped, bytes) else str(popped)
        job = Job.loads(raw)
        try:
            handler = handlers[job.name]
            await handler(job.payload)
        except Exception as exc:
            job.attempts += 1
            target = self.dead if job.attempts >= self.max_attempts else self.key
            logger.warning(
                "job failed",
                job=job.name,
                id=job.id,
                attempts=job.attempts,
                error=f"{type(exc).__name__}: {exc}",
                dead=target == self.dead,
            )
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.lrem(self.processing, 1, raw)
                pipe.lpush(target, job.dumps())
                await pipe.execute()
            return True
        await self._redis.lrem(self.processing, 1, raw)
        logger.info("job done", job=job.name, id=job.id)
        return True

    async def length(self) -> int:
        return int(await self._redis.llen(self.key))

    async def dead_jobs(self) -> list[Job]:
        return [Job.loads(r) for r in await self._redis.lrange(self.dead, 0, -1)]
