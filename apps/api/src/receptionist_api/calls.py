"""Call records: written by the agent during a call, read by the owner dashboard.

Every query takes `tenant_id` and filters on it. Writes are idempotent (start is an upsert on
(tenant_id, call_id); turns are unique on (call, idx)), so the agent can safely retry them.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from receptionist_api.db.models import Booking, Call, Message, ToolCallRow, Turn
from receptionist_api.db.session import transaction
from receptionist_shared.api_models import CallEnd, CallStart, ToolCallIn, TurnIn


class CallNotFoundError(LookupError):
    pass


@dataclass
class CallDetail:
    call: Call
    turns: list[Turn] = field(default_factory=list)
    tool_calls: list[ToolCallRow] = field(default_factory=list)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))
    return ordered[int(rank) - 1]


class CallRecords:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # ------------------------------------------------------------ writes (agent)

    async def start(self, tenant_id: uuid.UUID, req: CallStart) -> uuid.UUID:
        async with transaction(self._sessions) as session:
            stmt = (
                insert(Call)
                .values(
                    tenant_id=tenant_id,
                    call_id=req.call_id,
                    channel=req.channel,
                    caller_phone=req.caller_phone,
                    started_at=req.started_at,
                    status="active",
                )
                .on_conflict_do_nothing(constraint="uq_calls_tenant_call")
            )
            await session.execute(stmt)
            pk = await session.scalar(
                select(Call.id).where(Call.tenant_id == tenant_id, Call.call_id == req.call_id)
            )
            if pk is None:  # unreachable: we just inserted it or it existed
                raise CallNotFoundError(req.call_id)
            return pk

    async def _pk(self, session: AsyncSession, tenant_id: uuid.UUID, call_id: str) -> uuid.UUID:
        pk = await session.scalar(
            select(Call.id).where(Call.tenant_id == tenant_id, Call.call_id == call_id)
        )
        if pk is None:
            raise CallNotFoundError(call_id)
        return pk

    async def add_turn(self, tenant_id: uuid.UUID, call_id: str, req: TurnIn) -> None:
        async with transaction(self._sessions) as session:
            pk = await self._pk(session, tenant_id, call_id)
            await session.execute(
                insert(Turn)
                .values(
                    tenant_id=tenant_id,
                    call_pk=pk,
                    idx=req.idx,
                    speaker=req.speaker.value,
                    text=req.text,
                    interrupted=req.interrupted,
                    timings=req.timings,
                    spoken_at=req.spoken_at,
                )
                .on_conflict_do_nothing(constraint="uq_turns_call_idx")
            )

    async def add_tool_call(self, tenant_id: uuid.UUID, call_id: str, req: ToolCallIn) -> None:
        async with transaction(self._sessions) as session:
            pk = await self._pk(session, tenant_id, call_id)
            session.add(
                ToolCallRow(
                    tenant_id=tenant_id,
                    call_pk=pk,
                    name=req.name,
                    arguments=req.arguments,
                    result=req.result,
                    ok=req.ok,
                    duration_ms=req.duration_ms,
                    called_at=req.called_at,
                )
            )

    async def end(self, tenant_id: uuid.UUID, call_id: str, req: CallEnd) -> uuid.UUID:
        async with transaction(self._sessions) as session:
            pk = await self._pk(session, tenant_id, call_id)
            await session.execute(
                update(Call)
                .where(Call.id == pk, Call.tenant_id == tenant_id)
                .values(
                    ended_at=req.ended_at,
                    status="ended",
                    language=req.language,
                    recording_path=req.recording_path,
                )
            )
            return pk

    async def close_abandoned(
        self, *, idle_for: timedelta, now: datetime
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """Close calls the agent stopped reporting on (it crashed or was restarted mid-call), so
        they don't stay "in progress" forever without a summary. A call counts as abandoned when
        it is still active and nothing was said for `idle_for`; it ends at its last turn.

        The one query that spans tenants: it's the worker's housekeeping, not a tenant request.
        Each row is updated with its own tenant_id. Returns (tenant_id, call_pk) of each closed
        call, for the summary job.
        """
        last_turn = (
            select(Turn.call_pk, func.max(Turn.spoken_at).label("at"))
            .group_by(Turn.call_pk)
            .subquery()
        )
        last_seen = func.coalesce(last_turn.c.at, Call.started_at)
        async with transaction(self._sessions) as session:
            rows = (
                await session.execute(
                    select(Call.id, Call.tenant_id, last_seen.label("last_seen"))
                    .outerjoin(last_turn, last_turn.c.call_pk == Call.id)
                    .where(Call.status == "active", last_seen < now - idle_for)
                )
            ).all()
            closed = []
            for pk, tenant_id, seen in rows:
                result = await session.execute(
                    update(Call)
                    .where(Call.id == pk, Call.tenant_id == tenant_id, Call.status == "active")
                    .values(ended_at=seen, status="ended")
                )
                if result.rowcount:  # type: ignore[attr-defined]  # the agent's /end can race us
                    closed.append((tenant_id, pk))
            return closed

    async def save_analysis(
        self,
        tenant_id: uuid.UUID,
        call_pk: uuid.UUID,
        *,
        summary: str,
        intent: str,
        outcome: str,
        sentiment: str,
        status: str = "done",
    ) -> None:
        async with transaction(self._sessions) as session:
            await session.execute(
                update(Call)
                .where(Call.id == call_pk, Call.tenant_id == tenant_id)
                .values(
                    summary=summary,
                    intent=intent,
                    outcome=outcome,
                    sentiment=sentiment,
                    summary_status=status,
                )
            )

    # ------------------------------------------------------------ reads (dashboard, worker)

    async def list_calls(
        self, tenant_id: uuid.UUID, *, limit: int = 50, before: datetime | None = None
    ) -> list[Call]:
        stmt = select(Call).where(Call.tenant_id == tenant_id)
        if before is not None:
            stmt = stmt.where(Call.started_at < before)
        async with self._sessions() as session:
            rows = await session.scalars(stmt.order_by(Call.started_at.desc()).limit(limit))
            return list(rows)

    async def detail(self, tenant_id: uuid.UUID, call_pk: uuid.UUID) -> CallDetail:
        async with self._sessions() as session:
            call = await session.scalar(
                select(Call).where(Call.id == call_pk, Call.tenant_id == tenant_id)
            )
            if call is None:
                raise CallNotFoundError(str(call_pk))
            turns = await session.scalars(
                select(Turn)
                .where(Turn.call_pk == call_pk, Turn.tenant_id == tenant_id)
                .order_by(Turn.idx)
            )
            tools = await session.scalars(
                select(ToolCallRow)
                .where(ToolCallRow.call_pk == call_pk, ToolCallRow.tenant_id == tenant_id)
                .order_by(ToolCallRow.called_at)
            )
            return CallDetail(call, list(turns), list(tools))

    async def bookings(
        self, tenant_id: uuid.UUID, *, start: datetime | None = None, limit: int = 200
    ) -> list[Booking]:
        stmt = select(Booking).where(Booking.tenant_id == tenant_id)
        if start is not None:
            stmt = stmt.where(Booking.ends_at >= start)
        async with self._sessions() as session:
            return list(await session.scalars(stmt.order_by(Booking.starts_at).limit(limit)))

    async def messages(
        self, tenant_id: uuid.UUID, *, status: str | None = None, limit: int = 200
    ) -> list[Message]:
        stmt = select(Message).where(Message.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(Message.status == status)
        async with self._sessions() as session:
            return list(
                await session.scalars(stmt.order_by(Message.created_at.desc()).limit(limit))
            )

    async def set_message_status(
        self, tenant_id: uuid.UUID, message_id: uuid.UUID, status: str
    ) -> Message:
        async with transaction(self._sessions) as session:
            row = await session.scalar(
                select(Message).where(Message.id == message_id, Message.tenant_id == tenant_id)
            )
            if row is None:
                raise CallNotFoundError(str(message_id))
            row.status = status
            return row

    async def latency_stats(
        self, tenant_id: uuid.UUID, *, since: datetime
    ) -> dict[str, dict[str, Any]]:
        """P50/P95 per pipeline stage over agent turns since `since`."""
        async with self._sessions() as session:
            rows = await session.scalars(
                select(Turn.timings).where(
                    Turn.tenant_id == tenant_id,
                    Turn.timings.is_not(None),
                    Turn.spoken_at >= since,
                )
            )
            by_stage: dict[str, list[float]] = defaultdict(list)
            for timings in rows:
                for stage, value in (timings or {}).items():
                    if isinstance(value, int | float):
                        by_stage[stage].append(float(value))
        return {
            stage: {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95)}
            for stage, v in sorted(by_stage.items())
        }

    async def counts(self, tenant_id: uuid.UUID, *, since: datetime) -> dict[str, Any]:
        """Calls per outcome since `since` (for the analytics page)."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Call.outcome, func.count())
                    .where(Call.tenant_id == tenant_id, Call.started_at >= since)
                    .group_by(Call.outcome)
                )
            ).all()
        by_outcome = {outcome or "pending": n for outcome, n in rows}
        return {"total": sum(by_outcome.values()), "by_outcome": by_outcome}

    async def call_times(
        self, tenant_id: uuid.UUID, *, since: datetime
    ) -> list[tuple[datetime, str | None]]:
        """(started_at, outcome) of every call since `since`, oldest first (analytics)."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    select(Call.started_at, Call.outcome)
                    .where(Call.tenant_id == tenant_id, Call.started_at >= since)
                    .order_by(Call.started_at)
                )
            ).all()
        return [(started, outcome) for started, outcome in rows]

    async def recording_path(self, tenant_id: uuid.UUID, call_pk: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            path = await session.scalar(
                select(Call.recording_path).where(Call.id == call_pk, Call.tenant_id == tenant_id)
            )
        return path


def since_days(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
