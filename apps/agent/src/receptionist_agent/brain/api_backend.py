"""`BookingBackend` over HTTP: the agent's tools call apps/api.

Errors come back from the api with stable codes and are turned into the exceptions the tools
already handle (`SlotTakenError`, `BookingNotFoundError`, `UnknownServiceError`). Anything else
(api down, calendar down, timeout) raises `BackendUnavailableError`; the tool dispatcher turns
that into "the system had a problem; offer to take a message", so the caller never hears silence.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime, time
from typing import Any

import httpx
from pydantic import TypeAdapter

from receptionist_agent.brain.backend import (
    Availability,
    Booking,
    BookingNotFoundError,
    SlotTakenError,
    UnknownServiceError,
)
from receptionist_shared.api_models import (
    AvailabilityRequest,
    AvailabilityResponse,
    BookingCreate,
    BookingOut,
    CancelRequest,
    HoldRequest,
    HoldResponse,
    LookupRequest,
    LookupResponse,
    MessageCreate,
    MessageKind,
    MessageOut,
    RescheduleRequest,
)
from receptionist_shared.knowledge import KnowledgeResult
from receptionist_shared.tenant import TenantConfig

_BOOKING_LIST = TypeAdapter(list[BookingOut])
DEFAULT_TIMEOUT_SECS = 8.0


class BackendUnavailableError(RuntimeError):
    pass


class ApiBackend:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_secs: float = DEFAULT_TIMEOUT_SECS,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_secs)
        self._headers = {"X-Agent-Token": token}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(
        self,
        method: str,
        slug: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        url = f"/v1/tenants/{slug}{path}"
        try:
            response = await self._client.request(
                method, url, json=json, params=params, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(f"api unreachable: {type(exc).__name__}") from exc
        if response.status_code < 400:
            return response.json()
        code = ""
        with contextlib.suppress(ValueError, AttributeError):
            code = response.json().get("detail", {}).get("code", "")
        if code == "slot_taken":
            raise SlotTakenError(code)
        if code == "booking_not_found":
            raise BookingNotFoundError(code)
        if code == "unknown_service":
            raise UnknownServiceError(code)
        raise BackendUnavailableError(f"api error {response.status_code} {code}".strip())

    async def availability(
        self,
        tenant: TenantConfig,
        service: str,
        day: date,
        *,
        preferred: time | None,
        call_id: str,
    ) -> Availability:
        req = AvailabilityRequest(
            service=service, day=day, preferred_time=preferred, call_id=call_id
        )
        data = await self._call(
            "POST", tenant.slug, "/availability", json=req.model_dump(mode="json")
        )
        resp = AvailabilityResponse.model_validate(data)
        return Availability(
            resp.day, [o.astimezone(tenant.tz) for o in resp.options], resp.requested_day_available
        )

    async def hold(
        self, tenant: TenantConfig, *, service: str, start: datetime, call_id: str
    ) -> bool:
        req = HoldRequest(service=service, start=start, call_id=call_id)
        data = await self._call("POST", tenant.slug, "/holds", json=req.model_dump(mode="json"))
        return HoldResponse.model_validate(data).held

    async def create_booking(
        self,
        tenant: TenantConfig,
        *,
        service: str,
        start: datetime,
        caller_name: str,
        phone: str,
        call_id: str,
        idempotency_key: str,
    ) -> Booking:
        req = BookingCreate(
            service=service,
            start=start,
            caller_name=caller_name,
            phone=phone,
            call_id=call_id,
            idempotency_key=idempotency_key,
        )
        data = await self._call("POST", tenant.slug, "/bookings", json=req.model_dump(mode="json"))
        return BookingOut.model_validate(data)

    async def find_bookings(self, tenant: TenantConfig, phone: str) -> list[Booking]:
        data = await self._call("GET", tenant.slug, "/bookings", params={"phone": phone})
        return _BOOKING_LIST.validate_python(data)

    async def reschedule_booking(
        self,
        tenant: TenantConfig,
        booking_id: str,
        *,
        start: datetime,
        call_id: str,
        idempotency_key: str,
    ) -> Booking:
        req = RescheduleRequest(start=start, call_id=call_id, idempotency_key=idempotency_key)
        data = await self._call(
            "POST",
            tenant.slug,
            f"/bookings/{booking_id}/reschedule",
            json=req.model_dump(mode="json"),
        )
        return BookingOut.model_validate(data)

    async def cancel_booking(
        self, tenant: TenantConfig, booking_id: str, *, call_id: str
    ) -> Booking:
        req = CancelRequest(call_id=call_id)
        data = await self._call(
            "POST", tenant.slug, f"/bookings/{booking_id}/cancel", json=req.model_dump(mode="json")
        )
        return BookingOut.model_validate(data)

    async def _message(
        self,
        tenant: TenantConfig,
        kind: MessageKind,
        body: str,
        caller_name: str | None,
        phone: str | None,
        call_id: str,
    ) -> str:
        req = MessageCreate(
            kind=kind, body=body, caller_name=caller_name, phone=phone, call_id=call_id
        )
        data = await self._call("POST", tenant.slug, "/messages", json=req.model_dump(mode="json"))
        return MessageOut.model_validate(data).id

    async def take_message(
        self,
        tenant: TenantConfig,
        *,
        caller_name: str | None,
        phone: str | None,
        message: str,
        call_id: str,
    ) -> str:
        return await self._message(
            tenant, MessageKind.MESSAGE, message, caller_name, phone, call_id
        )

    async def request_handoff(
        self,
        tenant: TenantConfig,
        *,
        reason: str,
        caller_name: str | None,
        phone: str | None,
        call_id: str,
    ) -> str:
        return await self._message(tenant, MessageKind.HANDOFF, reason, caller_name, phone, call_id)

    async def record(self, tenant: TenantConfig, path: str, body: dict[str, Any]) -> None:
        """Send one call record (see records.py). `path` is relative to /calls."""
        await self._call("POST", tenant.slug, f"/calls{path}", json=body)

    async def tenant_config(self, slug: str) -> TenantConfig:
        return TenantConfig.model_validate(await self._call("GET", slug, "/config"))

    async def whatsapp_tenant(self, phone_number_id: str) -> TenantConfig | None:
        """The business that owns a WhatsApp number (Phase 8), or None if no business does."""
        try:
            response = await self._client.get(
                f"/v1/whatsapp/numbers/{phone_number_id}", headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(f"api unreachable: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise BackendUnavailableError(f"api error {response.status_code}")
        return await self.tenant_config(str(response.json()["tenant"]))

    async def lookup(
        self, tenant: TenantConfig, question: str, *, language: str
    ) -> KnowledgeResult:
        req = LookupRequest(question=question, language=language)
        data = await self._call("POST", tenant.slug, "/knowledge/lookup", json=req.model_dump())
        resp = LookupResponse.model_validate(data)
        return KnowledgeResult(found=resp.found, answer=resp.answer, sources=resp.sources)
