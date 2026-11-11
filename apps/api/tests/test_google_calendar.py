"""Google Calendar client against mocked HTTP (respx): request shapes and error handling."""

from datetime import datetime

import httpx
import pytest
import respx

from receptionist_api.calendar import GOOGLE_API, CalendarError, GoogleCalendar, event_id_for

CAL = "salon@group.calendar.google.com"
START = datetime.fromisoformat("2026-11-18T14:00:00+05:00")
END = datetime.fromisoformat("2026-11-18T14:45:00+05:00")


async def token() -> str:
    return "ya29.test"


@pytest.fixture
def google() -> GoogleCalendar:
    return GoogleCalendar(token, client=httpx.AsyncClient(base_url=GOOGLE_API))


@respx.mock
async def test_busy_parses_freebusy(google: GoogleCalendar) -> None:
    route = respx.post(f"{GOOGLE_API}/freeBusy").respond(
        200,
        json={
            "calendars": {
                CAL: {"busy": [{"start": "2026-11-18T09:00:00Z", "end": "2026-11-18T10:00:00Z"}]}
            }
        },
    )
    busy = await google.busy(CAL, START, END)
    assert busy[0].start == datetime.fromisoformat("2026-11-18T09:00:00+00:00")
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer ya29.test"
    assert b'"items":[{"id":"salon@group.calendar.google.com"}]' in sent.content.replace(b" ", b"")


@respx.mock
async def test_busy_calendar_error_raises(google: GoogleCalendar) -> None:
    respx.post(f"{GOOGLE_API}/freeBusy").respond(
        200, json={"calendars": {CAL: {"errors": [{"reason": "notFound"}]}}}
    )
    with pytest.raises(CalendarError):
        await google.busy(CAL, START, END)


@respx.mock
async def test_create_event_and_retry_conflict(google: GoogleCalendar) -> None:
    eid = event_id_for("salon:key-1")
    route = respx.post(f"{GOOGLE_API}/calendars/{CAL}/events")
    route.side_effect = [httpx.Response(200, json={"id": eid}), httpx.Response(409)]
    kwargs = dict(
        event_id=eid,
        summary="Haircut: Sara",
        description="",
        start=START,
        end=END,
        timezone="Asia/Karachi",
    )
    assert await google.create_event(CAL, **kwargs) == eid  # type: ignore[arg-type]
    assert await google.create_event(CAL, **kwargs) == eid  # type: ignore[arg-type]


@respx.mock
async def test_server_error_and_network_error(google: GoogleCalendar) -> None:
    respx.patch(f"{GOOGLE_API}/calendars/{CAL}/events/e1").respond(500)
    with pytest.raises(CalendarError, match="500"):
        await google.move_event(CAL, "e1", start=START, end=END, timezone="Asia/Karachi")
    respx.delete(f"{GOOGLE_API}/calendars/{CAL}/events/e2").mock(
        side_effect=httpx.ConnectTimeout("slow")
    )
    with pytest.raises(CalendarError, match="ConnectTimeout"):
        await google.cancel_event(CAL, "e2")


@respx.mock
async def test_cancel_tolerates_already_gone(google: GoogleCalendar) -> None:
    respx.delete(f"{GOOGLE_API}/calendars/{CAL}/events/e3").respond(410)
    await google.cancel_event(CAL, "e3")


def test_event_ids_are_valid_base32hex() -> None:
    eid = event_id_for("anything")
    assert 5 <= len(eid) <= 1024
    assert set(eid) <= set("0123456789abcdefghijklmnopqrstuv")
    assert event_id_for("anything") == eid
