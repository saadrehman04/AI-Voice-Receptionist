"""Onboarding, login, the dashboard, and tenant isolation (Phase 4's main safety check).

Two businesses are created through the real onboarding endpoint; each gets calls, turns,
tool calls, bookings and messages through the agent endpoints. Owner A must never see or
change anything of business B's, through any route.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import yaml

from receptionist_api.jobs import JobQueue
from receptionist_api.main import Resources
from receptionist_api.testing import REPO

PASSWORD = "a-long-enough-password"


def tenant_payload(slug: str, name: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO / "tenants/demo-salon.yaml").read_text(encoding="utf-8"))
    data.update(slug=slug, business_name=name)
    return dict(data)


async def onboard(api: httpx.AsyncClient, slug: str, email: str) -> str:
    r = await api.post(
        "/v1/onboarding",
        json={
            "tenant": tenant_payload(slug, slug.title()),
            "owner_email": email,
            "password": PASSWORD,
        },
    )
    assert r.status_code == 201, r.text
    return str(r.json()["access_token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def make_call(api: httpx.AsyncClient, slug: str, call_id: str) -> None:
    base = f"/v1/tenants/{slug}/calls"
    t0 = datetime.now(UTC)
    assert (
        await api.post(base, json={"call_id": call_id, "started_at": t0.isoformat()})
    ).status_code == 201
    turns = [
        ("agent", "Hello!", None),
        ("caller", f"Hi, this is {slug}'s caller", None),
        ("agent", "Sure.", {"stt": 250.0, "llm": 400.0, "total": 900.0}),
    ]
    for i, (speaker, text, timings) in enumerate(turns):
        r = await api.post(
            f"{base}/{call_id}/turns",
            json={
                "idx": i,
                "speaker": speaker,
                "text": text,
                "timings": timings,
                "spoken_at": (t0 + timedelta(seconds=i)).isoformat(),
            },
        )
        assert r.status_code == 201, r.text
    await api.post(
        f"{base}/{call_id}/tool-calls",
        json={
            "name": "take_message",
            "arguments": {"message": "call me"},
            "result": {"ok": True},
            "ok": True,
            "duration_ms": 12,
            "called_at": t0.isoformat(),
        },
    )
    await api.post(
        f"/v1/tenants/{slug}/messages",
        json={"kind": "message", "body": f"message for {slug}", "call_id": call_id},
    )
    r = await api.post(
        f"{base}/{call_id}/end", json={"ended_at": (t0 + timedelta(seconds=40)).isoformat()}
    )
    assert r.status_code == 200, r.text


@pytest.fixture
async def two_tenants(api: httpx.AsyncClient) -> tuple[str, str]:
    token_a = await onboard(api, "alpha-salon", "a@example.com")
    token_b = await onboard(api, "beta-salon", "b@example.com")
    await make_call(api, "alpha-salon", "call-a1")
    await make_call(api, "beta-salon", "call-b1")
    return token_a, token_b


# ------------------------------------------------------------------ onboarding + auth


async def test_onboarding_conflicts_and_validation(api: httpx.AsyncClient) -> None:
    await onboard(api, "gamma-salon", "g@example.com")
    again = await api.post(
        "/v1/onboarding",
        json={
            "tenant": tenant_payload("gamma-salon", "G"),
            "owner_email": "new@example.com",
            "password": PASSWORD,
        },
    )
    assert again.json()["detail"]["code"] == "tenant_exists"
    email = await api.post(
        "/v1/onboarding",
        json={
            "tenant": tenant_payload("delta-salon", "D"),
            "owner_email": "G@example.com",
            "password": PASSWORD,
        },
    )
    assert email.json()["detail"]["code"] == "email_taken"  # emails are case-insensitive
    weak = await api.post(
        "/v1/onboarding",
        json={
            "tenant": tenant_payload("eps-salon", "E"),
            "owner_email": "e@example.com",
            "password": "short",
        },
    )
    assert weak.status_code == 422
    bad = tenant_payload("zeta-salon", "Z")
    bad["opening_hours"] = {"monday": ["18:00-09:00"]}
    invalid = await api.post(
        "/v1/onboarding", json={"tenant": bad, "owner_email": "z@example.com", "password": PASSWORD}
    )
    assert invalid.status_code == 422


async def test_login(api: httpx.AsyncClient) -> None:
    await onboard(api, "eta-salon", "owner@eta.example")
    ok = await api.post("/v1/auth/login", json={"email": "OWNER@eta.example", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["tenant_slug"] == "eta-salon"
    me = await api.get("/v1/dashboard/me", headers=bearer(ok.json()["access_token"]))
    assert me.json() == {
        "email": "owner@eta.example",
        "tenant_slug": "eta-salon",
        "business_name": "Eta-Salon",
    }
    for email, pw in (("owner@eta.example", "wrong-password!!"), ("nobody@x.example", PASSWORD)):
        bad = await api.post("/v1/auth/login", json={"email": email, "password": pw})
        assert bad.status_code == 401
        assert bad.json()["detail"]["code"] == "bad_credentials"  # same answer either way


async def test_new_tenant_is_served_to_the_agent(api: httpx.AsyncClient) -> None:
    await onboard(api, "theta-salon", "t@example.com")
    r = await api.get("/v1/tenants/theta-salon/config")
    assert r.status_code == 200 and r.json()["business_name"] == "Theta-Salon"


async def test_settings_update_reaches_the_agent(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, _ = two_tenants
    settings = (await api.get("/v1/dashboard/settings", headers=bearer(token_a))).json()
    settings["services"][0]["price"] = 3000
    r = await api.put("/v1/dashboard/settings", json=settings, headers=bearer(token_a))
    assert r.status_code == 200, r.text
    agent_view = (await api.get("/v1/tenants/alpha-salon/config")).json()
    assert agent_view["services"][0]["price"] == "3000"


# ------------------------------------------------------------------ dashboard content


async def test_dashboard_shows_own_calls_with_transcript(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, _ = two_tenants
    calls = (await api.get("/v1/dashboard/calls", headers=bearer(token_a))).json()
    assert [c["call_id"] for c in calls] == ["call-a1"]
    assert calls[0]["duration_secs"] == 40 and calls[0]["status"] == "ended"
    detail = (
        await api.get(f"/v1/dashboard/calls/{calls[0]['id']}", headers=bearer(token_a))
    ).json()
    assert [t["speaker"] for t in detail["turns"]] == ["agent", "caller", "agent"]
    assert detail["turns"][2]["timings"]["total"] == 900.0
    assert detail["tool_calls"][0]["name"] == "take_message"
    stats = (await api.get("/v1/dashboard/stats", headers=bearer(token_a))).json()
    assert stats["calls"] == 1
    assert stats["latency_ms"]["total"] == {"n": 1, "p50": 900.0, "p95": 900.0}


async def test_call_end_enqueues_summary(
    api: httpx.AsyncClient, two_tenants: tuple[str, str], resources: Resources
) -> None:
    queue: JobQueue = resources.queue
    assert await queue.length() == 2  # one per ended call


# ------------------------------------------------------------------ isolation


async def test_lists_only_show_own_tenant(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, token_b = two_tenants
    for path in ("/v1/dashboard/calls", "/v1/dashboard/messages"):
        a = (await api.get(path, headers=bearer(token_a))).json()
        b = (await api.get(path, headers=bearer(token_b))).json()
        assert a and b
        assert {x["id"] for x in a}.isdisjoint({x["id"] for x in b})
    msgs = (await api.get("/v1/dashboard/messages", headers=bearer(token_a))).json()
    assert [m["body"] for m in msgs] == ["message for alpha-salon"]


async def test_cannot_read_or_change_other_tenants_records(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, token_b = two_tenants
    b_call = (await api.get("/v1/dashboard/calls", headers=bearer(token_b))).json()[0]["id"]
    b_msg = (await api.get("/v1/dashboard/messages", headers=bearer(token_b))).json()[0]["id"]
    assert (
        await api.get(f"/v1/dashboard/calls/{b_call}", headers=bearer(token_a))
    ).status_code == 404
    r = await api.patch(
        f"/v1/dashboard/messages/{b_msg}", json={"status": "done"}, headers=bearer(token_a)
    )
    assert r.status_code == 404
    still_new = (await api.get("/v1/dashboard/messages", headers=bearer(token_b))).json()[0]
    assert still_new["status"] == "new"


async def test_cannot_overwrite_other_tenants_settings(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, token_b = two_tenants
    theirs = (await api.get("/v1/dashboard/settings", headers=bearer(token_b))).json()
    theirs["business_name"] = "Hacked"
    r = await api.put("/v1/dashboard/settings", json=theirs, headers=bearer(token_a))
    assert r.status_code == 403
    assert (await api.get("/v1/dashboard/me", headers=bearer(token_b))).json()[
        "business_name"
    ] == "Beta-Salon"


async def test_dashboard_needs_a_valid_owner_token(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    no_agent_header = {"X-Agent-Token": ""}
    assert (await api.get("/v1/dashboard/calls", headers=no_agent_header)).status_code == 401
    assert (await api.get("/v1/dashboard/calls", headers=bearer("garbage"))).status_code == 401


async def test_owner_token_cannot_call_agent_endpoints(
    api: httpx.AsyncClient, two_tenants: tuple[str, str]
) -> None:
    token_a, _ = two_tenants
    r = await api.get(
        "/v1/tenants/alpha-salon/config", headers={"X-Agent-Token": "", **bearer(token_a)}
    )
    assert r.status_code == 401
