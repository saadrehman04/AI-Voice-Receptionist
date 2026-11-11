"""Calendars: one interface, a fake for dev/tests and Google Calendar for real use.

Google is called over its REST API with async httpx (the official client library is synchronous
and would block the event loop). Auth is a service account; the business shares its calendar
with the service account's email. Event ids are derived from our idempotency key, so a retried
insert hits HTTP 409 instead of creating a duplicate event.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from receptionist_shared.availability import Interval

GOOGLE_API = "https://www.googleapis.com/calendar/v3"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


class CalendarError(RuntimeError):
    """The calendar could not be read or written; callers should fail safe (no booking)."""


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    start: datetime
    end: datetime
    summary: str


def event_id_for(idempotency_key: str) -> str:
    """Google event ids must be base32hex ([a-v0-9]); lowercase hex is a valid subset."""
    return "rcp" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]


class Calendar(Protocol):
    async def busy(self, calendar_id: str, start: datetime, end: datetime) -> list[Interval]: ...

    async def create_event(
        self,
        calendar_id: str,
        *,
        event_id: str,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        timezone: str,
    ) -> str: ...

    async def move_event(
        self, calendar_id: str, event_id: str, *, start: datetime, end: datetime, timezone: str
    ) -> None: ...

    async def cancel_event(self, calendar_id: str, event_id: str) -> None: ...


@dataclass
class FakeCalendar:
    """In-memory calendar. `fail` makes every call raise, to test fail-safe behaviour."""

    events: dict[str, dict[str, CalendarEvent]] = field(default_factory=dict)
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def _check(self, op: str) -> None:
        self.calls.append(op)
        if self.fail:
            raise CalendarError(f"fake calendar failure during {op}")

    def add_external_event(self, calendar_id: str, start: datetime, end: datetime) -> None:
        """An appointment the owner put in the calendar by hand (not via us)."""
        eid = f"ext{len(self.events.get(calendar_id, {}))}"
        self.events.setdefault(calendar_id, {})[eid] = CalendarEvent(eid, start, end, "external")

    async def busy(self, calendar_id: str, start: datetime, end: datetime) -> list[Interval]:
        self._check("busy")
        window = Interval(start, end)
        return [
            Interval(e.start, e.end)
            for e in self.events.get(calendar_id, {}).values()
            if Interval(e.start, e.end).overlaps(window)
        ]

    async def create_event(
        self,
        calendar_id: str,
        *,
        event_id: str,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        timezone: str,
    ) -> str:
        self._check("create")
        self.events.setdefault(calendar_id, {})[event_id] = CalendarEvent(
            event_id, start, end, summary
        )
        return event_id

    async def move_event(
        self, calendar_id: str, event_id: str, *, start: datetime, end: datetime, timezone: str
    ) -> None:
        self._check("move")
        event = self.events.get(calendar_id, {}).get(event_id)
        if event is None:
            raise CalendarError(f"no event {event_id}")
        self.events[calendar_id][event_id] = CalendarEvent(event_id, start, end, event.summary)

    async def cancel_event(self, calendar_id: str, event_id: str) -> None:
        self._check("cancel")
        self.events.get(calendar_id, {}).pop(event_id, None)


TokenProvider = Callable[[], str]


class ServiceAccountTokens:
    """Access tokens from a service-account JSON file, refreshed (in a thread) when stale."""

    def __init__(self, key_file: Path) -> None:
        from google.oauth2 import service_account

        self._credentials: Any = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(key_file), scopes=[CALENDAR_SCOPE]
        )
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                await asyncio.to_thread(self._credentials.refresh, Request())
            return str(self._credentials.token)


class GoogleCalendar:
    def __init__(
        self,
        tokens: Callable[[], Any],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._tokens = tokens  # async () -> str
        self._client = client or httpx.AsyncClient(base_url=GOOGLE_API, timeout=10.0)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        token = await self._tokens()
        try:
            response = await self._client.request(
                method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
            )
        except httpx.HTTPError as exc:
            raise CalendarError(f"calendar request failed: {type(exc).__name__}") from exc
        return response

    @staticmethod
    def _raise(response: httpx.Response, what: str) -> None:
        raise CalendarError(f"{what} failed with HTTP {response.status_code}")

    async def busy(self, calendar_id: str, start: datetime, end: datetime) -> list[Interval]:
        response = await self._request(
            "POST",
            "/freeBusy",
            json={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": calendar_id}],
            },
        )
        if response.status_code != 200:
            self._raise(response, "freeBusy")
        calendar = response.json().get("calendars", {}).get(calendar_id, {})
        if calendar.get("errors"):
            raise CalendarError(f"freeBusy error for calendar: {calendar['errors'][0]}")
        return [
            Interval(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
            for b in calendar.get("busy", [])
        ]

    async def create_event(
        self,
        calendar_id: str,
        *,
        event_id: str,
        summary: str,
        description: str,
        start: datetime,
        end: datetime,
        timezone: str,
    ) -> str:
        body = {
            "id": event_id,
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        }
        response = await self._request("POST", f"/calendars/{calendar_id}/events", json=body)
        if response.status_code in (200, 201):
            return str(response.json()["id"])
        if response.status_code == 409:
            return event_id  # already created by an earlier attempt of the same request
        self._raise(response, "event insert")
        return event_id  # unreachable

    async def move_event(
        self, calendar_id: str, event_id: str, *, start: datetime, end: datetime, timezone: str
    ) -> None:
        body = {
            "start": {"dateTime": start.isoformat(), "timeZone": timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone},
        }
        response = await self._request(
            "PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=body
        )
        if response.status_code != 200:
            self._raise(response, "event update")

    async def cancel_event(self, calendar_id: str, event_id: str) -> None:
        response = await self._request("DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        if response.status_code not in (200, 204, 404, 410):
            self._raise(response, "event delete")

    async def aclose(self) -> None:
        await self._client.aclose()
