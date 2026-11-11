"""Onboarding helpers: draft a setup from the business's website, and connect a calendar.

- `POST /v1/onboarding/draft` is public (it runs before the account exists), so it is rate
  limited and its fetches are SSRF-checked (`website_draft`). It returns a *draft*: nothing is
  saved until the owner reviews it and submits `POST /v1/onboarding`.
- `GET/PUT /v1/dashboard/calendar` let a signed-in owner connect their Google Calendar: they
  share it with our service account, paste its id, and we check we can read it before saving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from receptionist_api.auth import OwnerDep
from receptionist_api.calendar import CalendarError, FakeCalendar
from receptionist_api.website_draft import DraftError, WebsiteDraft, draft_from_website

if TYPE_CHECKING:  # main imports the routers, so importing it at run time would be circular
    from receptionist_api.main import Resources

onboarding = APIRouter(prefix="/v1/onboarding", tags=["owners"])
calendar = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


def resources_of(request: Request) -> Resources:
    res: Resources = request.app.state.resources
    return res


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "detail": detail})


# ---------------------------------------------------------------- website draft


class DraftRequest(BaseModel):
    url: str = Field(min_length=4, max_length=500)


@onboarding.post("/draft", response_model=WebsiteDraft)
async def draft(req: DraftRequest, request: Request) -> WebsiteDraft:
    res = resources_of(request)
    if res.drafter is None or res.web_client is None:
        raise _error(503, "drafting_disabled", "set GROQ_API_KEY or GEMINI_API_KEY")
    client_key = request.client.host if request.client else "unknown"
    if res.draft_limiter is not None and not await res.draft_limiter.allow(client_key):
        raise _error(429, "rate_limited", "too many drafts; try again in an hour")
    url = req.url.strip()
    if "://" not in url:
        url = f"https://{url}"
    try:
        return await draft_from_website(url, client=res.web_client, drafter=res.drafter)
    except DraftError as exc:
        status = 503 if exc.code == "llm_unavailable" else 400
        raise _error(status, exc.code, exc.detail) from exc


# ---------------------------------------------------------------- calendar


class CalendarStatus(BaseModel):
    provider: Literal["google", "fake"]
    share_with: str | None  # the service-account email to share the calendar with
    calendar_id: str | None


class CalendarUpdate(BaseModel):
    calendar_id: str | None = Field(default=None, max_length=300)


def _status(request: Request, calendar_id: str | None) -> CalendarStatus:
    res = resources_of(request)
    provider: Literal["google", "fake"] = (
        "fake" if isinstance(res.calendar, FakeCalendar) else "google"
    )
    return CalendarStatus(
        provider=provider, share_with=res.calendar_account, calendar_id=calendar_id
    )


@calendar.get("/calendar", response_model=CalendarStatus)
async def get_calendar(owner: OwnerDep, request: Request) -> CalendarStatus:
    tenant = resources_of(request).store.registry.get(owner.tenant_slug)
    return _status(request, tenant.calendar_id if tenant else None)


@calendar.put("/calendar", response_model=CalendarStatus)
async def put_calendar(req: CalendarUpdate, owner: OwnerDep, request: Request) -> CalendarStatus:
    """Save the calendar id, but only after a read proves it is shared with us."""
    res = resources_of(request)
    tenant = res.store.registry.get(owner.tenant_slug)
    if tenant is None:
        raise _error(404, "tenant_not_found", owner.tenant_slug)
    calendar_id = (req.calendar_id or "").strip() or None
    if calendar_id is not None:
        now = datetime.now(UTC)
        try:
            await res.calendar.busy(calendar_id, now, now + timedelta(days=1))
        except CalendarError as exc:
            share = res.calendar_account or "the service account"
            raise _error(
                400,
                "calendar_not_shared",
                f"couldn't read that calendar; share it with {share} "
                "('Make changes to events') and try again",
            ) from exc
    await res.store.update(owner.tenant_id, tenant.model_copy(update={"calendar_id": calendar_id}))
    return _status(request, calendar_id)


__all__ = ["calendar", "onboarding"]
