"""The post-call job, end to end: call recorded -> job queued -> worker -> analysis saved."""

from datetime import UTC, datetime

from receptionist_api.calls import CallRecords
from receptionist_api.main import Resources
from receptionist_api.summary import HeuristicSummarizer
from receptionist_api.worker import make_handlers
from receptionist_shared.api_models import CallEnd, CallStart, Speaker, ToolCallIn, TurnIn


async def test_worker_saves_analysis(resources: Resources) -> None:
    calls: CallRecords = resources.calls
    tenant_id = resources.tenants.id_of("demo-salon")
    assert tenant_id is not None
    now = datetime.now(UTC)
    pk = await calls.start(tenant_id, CallStart(call_id="w-1", started_at=now))
    await calls.add_turn(
        tenant_id,
        "w-1",
        TurnIn(idx=0, speaker=Speaker.CALLER, text="Book me a facial", spoken_at=now),
    )
    await calls.add_tool_call(
        tenant_id,
        "w-1",
        ToolCallIn(
            name="create_booking",
            arguments={},
            result={"ok": True, "booked": True},
            ok=True,
            duration_ms=80,
            called_at=now,
        ),
    )
    await calls.end(tenant_id, "w-1", CallEnd(ended_at=now))
    await resources.queue.enqueue(
        "summarize_call", {"tenant_id": str(tenant_id), "call_pk": str(pk)}
    )

    handlers = make_handlers(resources.bookings._sessions, calls, HeuristicSummarizer())
    assert await resources.queue.run_one(handlers, wait_secs=1)

    detail = await calls.detail(tenant_id, pk)
    assert detail.call.outcome == "booked"
    assert detail.call.intent == "book"
    assert detail.call.summary is not None and "Book me a facial" in detail.call.summary
    assert detail.call.summary_status == "done"


async def test_turn_writes_are_idempotent(resources: Resources) -> None:
    calls: CallRecords = resources.calls
    tenant_id = resources.tenants.id_of("demo-salon")
    assert tenant_id is not None
    now = datetime.now(UTC)
    await calls.start(tenant_id, CallStart(call_id="w-2", started_at=now))
    pk = await calls.start(tenant_id, CallStart(call_id="w-2", started_at=now))  # retried
    turn = TurnIn(idx=0, speaker=Speaker.CALLER, text="hi", spoken_at=now)
    await calls.add_turn(tenant_id, "w-2", turn)
    await calls.add_turn(tenant_id, "w-2", turn)  # retried
    assert len((await calls.detail(tenant_id, pk)).turns) == 1
