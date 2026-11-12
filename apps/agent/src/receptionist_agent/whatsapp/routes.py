"""The webhook Meta calls: `GET /whatsapp` (subscription check) and `POST /whatsapp` (events).

Mounted on the agent's own FastAPI app (Pipecat's runner), because the call's audio has to end
up in this process. Meta wants a fast 200, so the POST only checks the signature, parses, and
hands each event to `WhatsAppCalls`, which works in the background.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from loguru import logger

from receptionist_agent.whatsapp.calls import CallRunner, WhatsAppCalls, run_receptionist
from receptionist_agent.whatsapp.graph import GraphApi
from receptionist_agent.whatsapp.webhook import call_events, verify_signature, verify_subscription
from receptionist_shared import Settings
from receptionist_shared.tenant import TenantConfig, list_tenants


def make_resolver(settings: Settings):  # type: ignore[no-untyped-def]  # returns a TenantResolver
    """WhatsApp number -> business: from the api (tenants made in the dashboard) when the
    agent uses it, else from the YAML files."""
    if settings.booking_backend == "api":
        from receptionist_agent.bot import default_backend
        from receptionist_agent.brain.api_backend import ApiBackend

        backend = default_backend(settings)
        assert isinstance(backend, ApiBackend)  # noqa: S101 - booking_backend == "api" above
        return backend.whatsapp_tenant

    async def from_files(phone_number_id: str) -> TenantConfig | None:
        tenants = list_tenants(Path(settings.tenants_dir))
        return next((t for t in tenants if t.whatsapp_phone_number_id == phone_number_id), None)

    return from_files


def install(app: FastAPI, settings: Settings, *, calls: WhatsAppCalls | None = None) -> None:
    """Add the webhook routes. Without WHATSAPP_APP_SECRET and WHATSAPP_VERIFY_TOKEN they
    answer 503: an unsigned webhook is never accepted."""
    secret = (
        settings.whatsapp_app_secret.get_secret_value() if settings.whatsapp_app_secret else None
    )
    verify_token = (
        settings.whatsapp_verify_token.get_secret_value()
        if settings.whatsapp_verify_token
        else None
    )
    token = (
        settings.whatsapp_access_token.get_secret_value()
        if settings.whatsapp_access_token
        else None
    )
    state: dict[str, WhatsAppCalls] = {}
    if calls is not None:
        state["calls"] = calls

    def manager() -> WhatsAppCalls:
        if "calls" not in state:
            if token is None:
                raise HTTPException(503, "set WHATSAPP_ACCESS_TOKEN")
            runner: CallRunner = partial(run_receptionist, settings)
            state["calls"] = WhatsAppCalls(
                GraphApi(settings.whatsapp_graph_url, token), make_resolver(settings), runner
            )
        return state["calls"]

    @app.get("/whatsapp", response_class=PlainTextResponse, include_in_schema=False)
    async def subscribe(request: Request) -> str:
        if verify_token is None:
            raise HTTPException(503, "set WHATSAPP_VERIFY_TOKEN")
        challenge = verify_subscription(dict(request.query_params), verify_token)
        if challenge is None:
            raise HTTPException(403, "verification failed")
        return challenge

    @app.post("/whatsapp", include_in_schema=False)
    async def events(request: Request) -> dict[str, int]:
        if secret is None:
            raise HTTPException(503, "set WHATSAPP_APP_SECRET")
        body = await request.body()
        if not verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
            logger.warning("whatsapp webhook with a bad or missing signature rejected")
            raise HTTPException(403, "bad signature")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "not JSON") from exc
        found = call_events(payload)
        for event in found:
            manager().handle(event)
        return {"call_events": len(found)}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if "calls" in state:
                await state["calls"].aclose()

    app.router.lifespan_context = _chain(app.router.lifespan_context, lifespan)


Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[Any]]


def _chain(first: Lifespan, second: Lifespan) -> Lifespan:
    """Run our lifespan inside the runner's, so ours cleans up first."""

    @asynccontextmanager
    async def both(app: FastAPI) -> AsyncIterator[None]:
        async with first(app), second(app):
            yield

    return both


__all__ = ["install", "make_resolver"]
