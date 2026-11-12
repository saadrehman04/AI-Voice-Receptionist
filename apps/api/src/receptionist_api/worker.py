"""Background worker: `make worker`. Runs post-call analysis jobs from the Redis queue.

The api enqueues `summarize_call` when the agent reports a call ended. The job is idempotent
(it just recomputes and overwrites the analysis), so retries and recovery are safe.

Once a minute the worker also closes calls the agent stopped reporting on (it crashed or was
restarted mid-call) and queues their summary, so none stays "in progress" forever.
"""

from __future__ import annotations

import asyncio
import signal
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist_api.calls import CallRecords
from receptionist_api.db.models import Tenant
from receptionist_api.db.session import make_engine, make_sessionmaker
from receptionist_api.jobs import Handler, JobQueue
from receptionist_api.summary import (
    GroqSummarizer,
    HeuristicSummarizer,
    Summarizer,
    ToolOutcome,
    TranscriptLine,
)
from receptionist_shared import Settings, get_settings
from receptionist_shared.logging import configure_logging

SUMMARIZE_CALL = "summarize_call"


def make_summarizer(settings: Settings) -> Summarizer:
    if settings.groq_api_key is not None:
        return GroqSummarizer(
            settings.groq_api_key.get_secret_value(),
            settings.groq_llm_model,
            reasoning_effort=settings.groq_reasoning_effort,
        )
    logger.info("no GROQ_API_KEY: call summaries use the heuristic summarizer")
    return HeuristicSummarizer()


# A call with no new turn for this long has lost its agent. Long enough for any real pause (the
# agent itself ends a silent call well before this), short enough that the owner soon sees it.
ABANDONED_AFTER = timedelta(minutes=10)
SWEEP_EVERY_SECS = 60.0


async def close_abandoned_calls(
    records: CallRecords, queue: JobQueue, *, now: datetime | None = None
) -> int:
    """Close calls whose agent went away, and queue their summaries. Returns how many."""
    closed = await records.close_abandoned(idle_for=ABANDONED_AFTER, now=now or datetime.now(UTC))
    for tenant_id, call_pk in closed:
        await queue.enqueue(SUMMARIZE_CALL, {"tenant_id": str(tenant_id), "call_pk": str(call_pk)})
        logger.warning("closed a call its agent stopped reporting on", call_pk=str(call_pk))
    return len(closed)


def make_handlers(
    sessions: async_sessionmaker[AsyncSession], records: CallRecords, summarizer: Summarizer
) -> dict[str, Handler]:
    async def summarize_call(payload: dict[str, Any]) -> None:
        tenant_id = uuid.UUID(payload["tenant_id"])
        call_pk = uuid.UUID(payload["call_pk"])
        detail = await records.detail(tenant_id, call_pk)
        async with sessions() as session:
            tenant = await session.get(Tenant, tenant_id)
        business = tenant.name if tenant else "the business"
        lines = [TranscriptLine(t.speaker, t.text) for t in detail.turns]
        tools = [ToolOutcome(t.name, t.ok, t.result) for t in detail.tool_calls]
        analysis = await summarizer.analyze(business, lines, tools)
        await records.save_analysis(
            tenant_id,
            call_pk,
            summary=analysis.summary,
            intent=analysis.intent,
            outcome=analysis.outcome,
            sentiment=analysis.sentiment,
        )
        logger.info("call analysed", call_pk=str(call_pk), outcome=analysis.outcome)

    return {SUMMARIZE_CALL: summarize_call}


async def run_worker(settings: Settings, stop: asyncio.Event) -> None:
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    redis = Redis.from_url(settings.redis_url)
    queue = JobQueue(redis, "default")
    records = CallRecords(sessions)
    handlers = make_handlers(sessions, records, make_summarizer(settings))
    await queue.recover()
    logger.info("worker started", queue=queue.key)
    next_sweep = 0.0
    try:
        while not stop.is_set():
            if time.monotonic() >= next_sweep:
                next_sweep = time.monotonic() + SWEEP_EVERY_SECS
                try:
                    await close_abandoned_calls(records, queue)
                except Exception:  # housekeeping must never stop the summaries
                    logger.exception("closing abandoned calls failed")
            await queue.run_one(handlers, wait_secs=2)
    finally:
        await redis.aclose()
        await engine.dispose()
        logger.info("worker stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    stop = asyncio.Event()

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(settings, stop)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
