"""Phase 7 routes: website draft, calendar connect, recordings, analytics (real Postgres/Redis)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from redis.asyncio import Redis

from receptionist_api import website_draft
from receptionist_api.calendar import FakeCalendar
from receptionist_api.main import Resources
from receptionist_api.ratelimit import RateLimiter
from receptionist_api.routes.owners import day_stats
from receptionist_api.testing import REPO, bearer, onboard
from receptionist_api.website_draft import DraftService, WebsiteDraft
from receptionist_shared.tenant import load_tenant_by_slug

LAHORE = ZoneInfo("Asia/Karachi")

PAGE = "<h1>Alpha Salon</h1><ul><li>Haircut - Rs. 2,500</li><li>Blow dry</li></ul>" * 3


class FakeDrafter:
    def __init__(self) -> None:
        self.pages: list[str] = []

    async def extract(self, page: str) -> WebsiteDraft:
        self.pages.append(page)
        return WebsiteDraft(
            business_name="Alpha Salon",
            services=[
                DraftService(name="Haircut", price=Decimal(2500), evidence="Haircut - Rs. 2,500"),
                DraftService(name="Blow dry", price=Decimal(1200), evidence="Blow dry - Rs. 1,200"),
            ],
        )


@pytest.fixture
def drafting(resources: Resources, redis: Redis, monkeypatch: pytest.MonkeyPatch) -> FakeDrafter:
    async def public(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(website_draft, "resolve_host", public)
    drafter = FakeDrafter()
    resources.drafter = drafter
    resources.web_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, html=PAGE))
    )
    resources.draft_limiter = RateLimiter(redis, prefix="draft-test", limit=2, window_secs=60)
    return drafter


# ---------------------------------------------------------------- website draft


async def test_draft_returns_only_grounded_facts(
    api: httpx.AsyncClient, drafting: FakeDrafter
) -> None:
    r = await api.post("/v1/onboarding/draft", json={"url": "alpha.example"})
    assert r.status_code == 200, r.text
    draft = r.json()
    assert [(s["name"], s["price"]) for s in draft["services"]] == [
        ("Haircut", "2500"),
        ("Blow dry", None),  # the LLM's 1,200 isn't on the page
    ]
    assert any("Blow dry" in d for d in draft["dropped"])
    assert "Haircut - Rs. 2,500" in drafting.pages[0]


async def test_draft_is_rate_limited(api: httpx.AsyncClient, drafting: FakeDrafter) -> None:
    for _ in range(2):
        assert (
            await api.post("/v1/onboarding/draft", json={"url": "a.example"})
        ).status_code == 200
    r = await api.post("/v1/onboarding/draft", json={"url": "a.example"})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


async def test_draft_refuses_private_addresses(
    api: httpx.AsyncClient, drafting: FakeDrafter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def internal(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr(website_draft, "resolve_host", internal)
    r = await api.post("/v1/onboarding/draft", json={"url": "http://localhost/admin"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "private_address"
    assert drafting.pages == []  # never reached the LLM


async def test_draft_without_llm_keys_is_switched_off(api: httpx.AsyncClient) -> None:
    r = await api.post("/v1/onboarding/draft", json={"url": "a.example"})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "drafting_disabled"


# ---------------------------------------------------------------- calendar


async def test_calendar_is_saved_only_once_we_can_read_it(
    api: httpx.AsyncClient, resources: Resources
) -> None:
    token = await onboard(api, "cal-salon", "cal@example.com")
    fake = FakeCalendar()
    resources.calendar, resources.calendar_account = fake, "bot@project.iam.gserviceaccount.com"

    status = (await api.get("/v1/dashboard/calendar", headers=bearer(token))).json()
    assert status["share_with"] == "bot@project.iam.gserviceaccount.com"

    fake.fail = True  # not shared yet
    r = await api.put(
        "/v1/dashboard/calendar", json={"calendar_id": "owner@gmail.com"}, headers=bearer(token)
    )
    assert r.status_code == 400 and r.json()["detail"]["code"] == "calendar_not_shared"
    assert "bot@project" in r.json()["detail"]["detail"]
    assert (await api.get("/v1/tenants/cal-salon/config")).json()["calendar_id"] is None

    fake.fail = False
    r = await api.put(
        "/v1/dashboard/calendar", json={"calendar_id": " owner@gmail.com "}, headers=bearer(token)
    )
    assert r.status_code == 200 and r.json()["calendar_id"] == "owner@gmail.com"
    assert (await api.get("/v1/tenants/cal-salon/config")).json()["calendar_id"] == (
        "owner@gmail.com"
    )
    r = await api.put("/v1/dashboard/calendar", json={"calendar_id": ""}, headers=bearer(token))
    assert r.json()["calendar_id"] is None  # disconnect


# ---------------------------------------------------------------- recordings


async def start_and_end(
    api: httpx.AsyncClient, slug: str, call_id: str, started: datetime, recording: str | None
) -> None:
    base = f"/v1/tenants/{slug}/calls"
    r = await api.post(base, json={"call_id": call_id, "started_at": started.isoformat()})
    assert r.status_code == 201, r.text
    end = {"ended_at": (started + timedelta(seconds=30)).isoformat(), "recording_path": recording}
    assert (await api.post(f"{base}/{call_id}/end", json=end)).status_code == 200


async def test_recording_is_served_only_from_the_tenants_folder(
    api: httpx.AsyncClient, resources: Resources, tmp_path: Path
) -> None:
    token = await onboard(api, "rec-salon", "rec@example.com")
    await onboard(api, "other-salon", "other@example.com")
    resources.storage_dir = tmp_path
    (tmp_path / "rec-salon").mkdir()
    (tmp_path / "other-salon").mkdir()
    mine = tmp_path / "rec-salon" / "c1.wav"
    mine.write_bytes(b"RIFF-mine")
    theirs = tmp_path / "other-salon" / "c9.wav"
    theirs.write_bytes(b"RIFF-theirs")

    now = datetime.now(UTC)
    await start_and_end(api, "rec-salon", "c1", now, str(mine))
    await start_and_end(api, "rec-salon", "c2", now, str(theirs))  # a bad row
    await start_and_end(api, "rec-salon", "c3", now, None)
    calls = {
        c["call_id"]: c["id"]
        for c in (await api.get("/v1/dashboard/calls", headers=bearer(token))).json()
    }
    ok = await api.get(f"/v1/dashboard/calls/{calls['c1']}/recording", headers=bearer(token))
    assert ok.status_code == 200 and ok.content == b"RIFF-mine"
    assert ok.headers["content-type"] == "audio/wav"
    for call in ("c2", "c3"):
        r = await api.get(f"/v1/dashboard/calls/{calls[call]}/recording", headers=bearer(token))
        assert r.status_code == 404
    detail = (await api.get(f"/v1/dashboard/calls/{calls['c3']}", headers=bearer(token))).json()
    assert detail["has_recording"] is False


# ---------------------------------------------------------------- analytics


def last(weekday: int, hour: int, now: datetime) -> datetime:
    """The most recent `weekday` (Mon=0) at `hour`:00 Lahore time that is before `now`."""
    local = now.astimezone(LAHORE).replace(hour=hour, minute=0, second=0, microsecond=0)
    local -= timedelta(days=(local.weekday() - weekday) % 7)
    return local if local < now else local - timedelta(days=7)


async def test_stats_per_day_and_after_hours(api: httpx.AsyncClient) -> None:
    token = await onboard(api, "stats-salon", "stats@example.com")  # demo-salon's hours
    now = datetime.now(UTC)
    await start_and_end(api, "stats-salon", "open", last(2, 12, now), None)  # Wed noon: open
    await start_and_end(api, "stats-salon", "closed", last(1, 12, now), None)  # Tuesday: closed
    stats = (await api.get("/v1/dashboard/stats?days=7", headers=bearer(token))).json()
    assert stats["calls"] == 2 and stats["after_hours"] == 1
    assert len(stats["per_day"]) == 7 and sum(d["calls"] for d in stats["per_day"]) == 2
    assert stats["per_day"][-1]["day"] == now.astimezone(LAHORE).date().isoformat()
    assert stats["booking_rate"] is None  # no outcome until the summary worker has run


def test_day_stats_counts_bookings_by_local_day() -> None:
    tenant = load_tenant_by_slug("demo-salon", REPO / "tenants")
    now = datetime(2026, 11, 18, 12, 0, tzinfo=LAHORE)  # Wednesday
    times = [
        (datetime(2026, 11, 17, 23, 30, tzinfo=LAHORE), "booked"),  # Tue, closed
        (datetime(2026, 11, 17, 19, 30, tzinfo=UTC), None),  # = Wed 00:30 Lahore, closed
        (datetime(2026, 11, 18, 11, 30, tzinfo=LAHORE), "booked"),  # Wed, open
    ]
    per_day, after_hours = day_stats(tenant, times, now=now, days=2)
    assert [(d.day.isoformat(), d.calls, d.booked) for d in per_day] == [
        ("2026-11-17", 1, 1),
        ("2026-11-18", 2, 1),
    ]
    assert after_hours == 2


# ---------------------------------------------------------------- WhatsApp numbers (Phase 8)


async def test_whatsapp_number_maps_to_one_business(api: httpx.AsyncClient) -> None:
    token_a = await onboard(api, "wa-one", "wa1@example.com")
    token_b = await onboard(api, "wa-two", "wa2@example.com")
    settings = (await api.get("/v1/dashboard/settings", headers=bearer(token_a))).json()
    settings["whatsapp_phone_number_id"] = "123456789012345"
    r = await api.put("/v1/dashboard/settings", json=settings, headers=bearer(token_a))
    assert r.status_code == 200, r.text

    owner = await api.get("/v1/whatsapp/numbers/123456789012345")  # the agent's lookup
    assert owner.status_code == 200 and owner.json() == {"tenant": "wa-one"}
    assert (await api.get("/v1/whatsapp/numbers/999")).status_code == 404
    no_token = await api.get("/v1/whatsapp/numbers/123456789012345", headers={"X-Agent-Token": ""})
    assert no_token.status_code == 401

    theirs = (await api.get("/v1/dashboard/settings", headers=bearer(token_b))).json()
    theirs["whatsapp_phone_number_id"] = "123456789012345"
    taken = await api.put("/v1/dashboard/settings", json=theirs, headers=bearer(token_b))
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "whatsapp_number_taken"

    # re-saving your own number is fine; so is giving it up
    assert (
        await api.put("/v1/dashboard/settings", json=settings, headers=bearer(token_a))
    ).status_code == 200
    settings["whatsapp_phone_number_id"] = None
    assert (
        await api.put("/v1/dashboard/settings", json=settings, headers=bearer(token_a))
    ).status_code == 200
    assert (await api.get("/v1/whatsapp/numbers/123456789012345")).status_code == 404
