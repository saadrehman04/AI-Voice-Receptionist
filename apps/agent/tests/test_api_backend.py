"""End to end, in one process: the agent's tools -> ApiBackend (HTTP) -> api -> Postgres/Redis.

Reuses the api test fixtures (real database, fake calendar), so it is skipped when
`make up` isn't running, like the api tests.
"""

from datetime import datetime

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
