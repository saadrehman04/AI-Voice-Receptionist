"""End to end, in one process: the agent's tools -> ApiBackend (HTTP) -> api -> Postgres/Redis.

Reuses the api test fixtures (real database, fake calendar), so it is skipped when
`make up` isn't running, like the api tests.
"""

from datetime import datetime
from typing import Any

import httpx
import pytest

from receptionist_agent.brain.api_backend import ApiBackend, BackendUnavailableError
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_api.testing import TOKEN
from receptionist_shared.tenant import TenantConfig


def tools_for(
    api: httpx.AsyncClient, salon: TenantConfig, now: datetime, call_id: str
) -> ReceptionistTools:
    backend = ApiBackend("http://api", TOKEN, client=api)
    return ReceptionistTools(salon, CallState(), backend, call_id=call_id, clock=lambda: now)


async def test_full_booking_through_the_api(
    api: httpx.AsyncClient, salon: TenantConfig, monday_9am: datetime
) -> None:
    tools = tools_for(api, salon, monday_9am, "e2e-1")
    offered = await tools.run(
        "check_availability",
        {"service": "Haircut", "date": "2026-11-18", "preferred_time": "14:00"},
    )
    start = offered["options"][0]["start"]
    args = {"service": "Haircut", "start": start, "caller_name": "Sara", "phone": "0300 1234567"}
    readback = await tools.run("create_booking", args | {"caller_confirmed": False})
    assert readback["booked"] is False

    rival = tools_for(api, salon, monday_9am, "e2e-2")
    await rival.run(
        "check_availability",
        {"service": "Haircut", "date": "2026-11-18", "preferred_time": "14:00"},
    )
    blocked = await rival.run(
        "create_booking",
        args | {"caller_name": "Bilal", "phone": "0301 7654321", "caller_confirmed": False},
    )
    assert blocked["ok"] is False  # held by e2e-1 during its read-back

    tools.state.heard_caller(tools.state.caller_turns + 1)  # the caller says yes
    done = await tools.run("create_booking", args | {"caller_confirmed": True})
    assert done["booked"] is True

    info = await tools.run("lookup_business_info", {"question": "do I need a patch test?"})
    assert info["found"] is True and "48 hours" in info["answer"]

    msg = await tools.run("take_message", {"message": "call me back", "phone": "03001234567"})
    assert msg["ok"] is True


async def test_api_down_becomes_a_spoken_fallback(
    salon: TenantConfig, monday_9am: datetime
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://api")
    backend = ApiBackend("http://api", "t", client=client)
    with pytest.raises(BackendUnavailableError):
        await backend.find_bookings(salon, "+923001234567")
    tools = ReceptionistTools(salon, CallState(), backend, call_id="x", clock=lambda: monday_9am)
    result = await tools.run("check_availability", {"service": "Haircut", "date": "2026-11-18"})
    assert result["ok"] is False
    assert "take a message" in result["error"]


async def test_call_is_recorded_and_summarised_end_to_end(
    api: httpx.AsyncClient, salon: TenantConfig, monday_9am: datetime, resources: object
) -> None:
    """Agent tools + recorder -> api -> Postgres -> job queue -> worker -> owner dashboard."""
    from receptionist_agent.records import ApiCallRecorder
    from receptionist_api.main import Resources
    from receptionist_api.summary import HeuristicSummarizer
    from receptionist_api.worker import make_handlers

    assert isinstance(resources, Resources)
    backend = ApiBackend("http://api", TOKEN, client=api)

    async def send(path: str, body: dict[str, object]) -> None:
        await backend.record(salon, path, body)

    recorder = ApiCallRecorder("e2e-call", send)

    def on_tool(name: str, args: dict, result: dict, ok: bool, ms: int) -> None:  # type: ignore[type-arg]
        recorder.tool_call(name, args, result, ok=ok, duration_ms=ms)

    tools = ReceptionistTools(
        salon,
        CallState(),
        backend,
        call_id="e2e-call",
        clock=lambda: monday_9am,
        on_tool_call=on_tool,
    )
    await recorder.start()
    recorder.agent_turn("Hello, Noor Beauty Salon.", interrupted=False)
    recorder.caller_turn("Can I leave a message for Ayesha?")
    await tools.run("take_message", {"message": "Call me about bridal", "phone": "03001234567"})
    recorder.timing({"stages": {"total": 700.0}})
    recorder.agent_turn("Done, she will call you back.", interrupted=False)
    await recorder.end(language="en", recording_path=None)

    handlers = make_handlers(resources.bookings._sessions, resources.calls, HeuristicSummarizer())
    assert await resources.queue.run_one(handlers, wait_secs=1)

    onboard = await api.post(
        "/v1/onboarding",
        json={
            "tenant": {
                **salon.model_dump(mode="json"),
                "slug": "other-salon",
                "whatsapp_phone_number_id": None,  # numbers are unique per business
            },
            "owner_email": "o@example.com",
            "password": "a-long-enough-password",
        },
    )
    assert onboard.status_code == 201
    tenant_id = resources.tenants.id_of("demo-salon")
    assert tenant_id is not None
    (call,) = await resources.calls.list_calls(tenant_id)
    detail = await resources.calls.detail(tenant_id, call.id)
    assert call.outcome == "message"
    assert [t.speaker for t in detail.turns] == ["agent", "caller", "agent"]
    assert detail.turns[2].timings == {"total": 700.0}
    assert detail.tool_calls[0].name == "take_message"


async def test_admission_through_the_api(
    api: httpx.AsyncClient, salon: TenantConfig, resources: Any, redis: Any
) -> None:
    """Phase 9 (D38): the agent asks before answering; over the limit it is told no."""
    from receptionist_api.usage import Limits, UsageService

    resources.usage = UsageService(resources.bookings._sessions, redis, Limits(1, 1000))
    backend = ApiBackend("http://api", TOKEN, client=api)
    assert (await backend.admit_call(salon)).allowed
    refused = await backend.admit_call(salon)
    assert (refused.allowed, refused.reason) == (False, "calls_per_hour")


async def test_admission_fails_open_when_the_api_is_down(salon: TenantConfig) -> None:
    down = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503)), base_url="http://api"
    )
    assert (await ApiBackend("http://api", TOKEN, client=down).admit_call(salon)).allowed
