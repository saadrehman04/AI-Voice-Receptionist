"""Incoming WhatsApp calls: answer, hand the audio to the receptionist, hang up cleanly.

One `WhatsAppCalls` per agent process serves every business:

1. **connect** (webhook): find the business that owns the called number, build our side of
   the WebRTC session from the caller's SDP offer (Pipecat's `SmallWebRTCConnection`, the same
   peer the browser test call uses), then `pre_accept` and `accept` with that SDP answer.
   Meta gives us roughly 30-60 s for this; it takes well under one.
2. The call runs through the normal pipeline (`run_bot`) with `channel="whatsapp"` and the
   caller's number, so it is recorded, summarised and shown on the dashboard like any other.
3. A booking, move or cancellation is confirmed by WhatsApp text (`confirm.py`), from the
   tool's stored result.
4. **terminate** (webhook): the caller hung up, so we close our side. If the agent hangs up
   first (`end_call`), we tell Meta with `terminate`, as Meta asks.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from loguru import logger
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from receptionist_agent.whatsapp.confirm import confirmation_text
from receptionist_agent.whatsapp.graph import GraphApi, GraphError
from receptionist_agent.whatsapp.webhook import CallEvent
from receptionist_shared import Settings
from receptionist_shared.tenant import TenantConfig

TenantResolver = Callable[[str], Awaitable[TenantConfig | None]]
DEFAULT_ICE = [IceServer(urls="stun:stun.l.google.com:19302")]


class CallRunner(Protocol):
    """Runs one call to completion. The real one is `run_receptionist`; tests pass a stub."""

    async def __call__(
        self, connection: SmallWebRTCConnection, call: LiveCall, hooks: CallHooks
    ) -> None: ...


@dataclass
class CallHooks:
    """What the running call reports back: each tool result, and the caller's language."""

    on_tool_call: Callable[[str, dict[str, Any], dict[str, Any], bool, int], None]
    language: Callable[[], str | None] = lambda: None


@dataclass
class LiveCall:
    event: CallEvent
    tenant: TenantConfig
    call_id: str  # ours (call records); Meta's id is event.call_id
    connection: SmallWebRTCConnection
    ended_by_caller: bool = False
    confirmations: list[str] = field(default_factory=list)  # texts sent (for logs and tests)

    @property
    def caller_e164(self) -> str:
        return f"+{self.event.caller}" if self.event.caller else ""


async def _disconnect(connection: SmallWebRTCConnection) -> None:
    await connection.disconnect()  # type: ignore[no-untyped-call]  # Pipecat leaves it untyped


def whatsapp_sdp(sdp: str) -> str:
    """Meta accepts only SHA-256 DTLS fingerprints; aiortc also offers 384/512."""
    lines = [
        line
        for line in sdp.splitlines()
        if not line.startswith("a=fingerprint:") or line.startswith("a=fingerprint:sha-256")
    ]
    return "\r\n".join(lines) + "\r\n"


async def run_receptionist(
    settings: Settings, connection: SmallWebRTCConnection, call: LiveCall, hooks: CallHooks
) -> None:
    """The real call: the same pipeline as a browser call, over the WhatsApp peer."""
    from receptionist_agent.bot import Call, run_bot  # heavy import, only when a call comes in

    def ready(c: Call) -> None:
        hooks.language = lambda: c.state.language

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    await run_bot(
        transport,
        settings,
        call_id=call.call_id,
        tenant_slug=call.tenant.slug,
        channel="whatsapp",
        caller_phone=call.caller_e164 or None,
        on_tool_call=hooks.on_tool_call,
        on_ready=ready,
    )


class WhatsAppCalls:
    def __init__(
        self,
        graph: GraphApi,
        resolve_tenant: TenantResolver,
        runner: CallRunner,
        *,
        ice_servers: list[IceServer] | None = None,
    ) -> None:
        self._graph = graph
        self._resolve = resolve_tenant
        self._runner = runner
        self._ice = DEFAULT_ICE if ice_servers is None else ice_servers
        self.live: dict[str, LiveCall] = {}  # by Meta's call id
        self._tasks: set[asyncio.Task[Any]] = set()

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def handle(self, event: CallEvent) -> None:
        """Called from the webhook: answer Meta at once, do the work in the background."""
        if event.kind == "connect":
            self._spawn(self.connect(event), f"wa-connect-{event.call_id}")
        else:
            self._spawn(self.caller_hung_up(event), f"wa-terminate-{event.call_id}")

    async def connect(self, event: CallEvent) -> None:
        with logger.contextualize(wa_call=event.call_id):
            if event.call_id in self.live:
                logger.info("duplicate connect webhook ignored")  # Meta retries on slow 200s
                return
            tenant = await self._resolve(event.phone_number_id)
            if tenant is None or not event.sdp:
                logger.warning("call to an unknown number, or no SDP offer; rejecting")
                await self._quietly(self._graph.reject_call(event.phone_number_id, event.call_id))
                return
            connection = SmallWebRTCConnection(self._ice)
            try:
                await connection.initialize(sdp=event.sdp, type="offer")
                answer = connection.get_answer()  # type: ignore[no-untyped-call]
                if not answer or not answer.get("sdp"):
                    raise RuntimeError("no SDP answer")
                sdp = whatsapp_sdp(str(answer["sdp"]))
                await self._graph.answer_call(
                    event.phone_number_id, event.call_id, "pre_accept", sdp
                )
                await self._graph.answer_call(event.phone_number_id, event.call_id, "accept", sdp)
            except (GraphError, RuntimeError, ValueError) as exc:
                logger.warning("could not answer the call", error=str(exc))
                await _disconnect(connection)
                return
            call = LiveCall(event, tenant, f"wa-{uuid.uuid4().hex}", connection)
            self.live[event.call_id] = call
            logger.info("whatsapp call answered", tenant=tenant.slug, call_id=call.call_id)
            self._spawn(self._run(call), f"wa-call-{event.call_id}")

    async def _run(self, call: LiveCall) -> None:
        hooks = CallHooks(on_tool_call=lambda *a: None)

        def on_tool_call(
            name: str, args: dict[str, Any], result: dict[str, Any], ok: bool, ms: int
        ) -> None:
            text = confirmation_text(call.tenant, name, result, hooks.language())
            if text and call.event.caller:
                self._spawn(self._confirm(call, text), f"wa-confirm-{call.event.call_id}")

        hooks.on_tool_call = on_tool_call
        try:
            await self._runner(call.connection, call, hooks)
        except Exception:
            logger.exception("whatsapp call failed")
        finally:
            self.live.pop(call.event.call_id, None)
            if not call.ended_by_caller:  # the agent hung up (end_call) or crashed
                await self._quietly(
                    self._graph.terminate_call(call.event.phone_number_id, call.event.call_id)
                )
            await _disconnect(call.connection)
            logger.info("whatsapp call ended", by_caller=call.ended_by_caller)

    async def _confirm(self, call: LiveCall, text: str) -> None:
        try:
            await self._graph.send_text(call.event.phone_number_id, call.event.caller, text)
            call.confirmations.append(text)
            logger.info("whatsapp confirmation sent")
        except GraphError as exc:
            # The call itself still succeeded; the booking is on the dashboard either way.
            logger.warning("whatsapp confirmation failed", error=str(exc), code=exc.code)

    async def caller_hung_up(self, event: CallEvent) -> None:
        call = self.live.get(event.call_id)
        if call is None:
            return  # unknown, already ended, or never answered
        call.ended_by_caller = True
        await _disconnect(call.connection)  # the transport sees it and the pipeline stops

    async def _quietly(self, request: Awaitable[None]) -> None:
        try:
            await request
        except GraphError as exc:
            logger.warning("graph request failed", error=str(exc))

    async def aclose(self) -> None:
        for call in list(self.live.values()):
            await self._quietly(
                self._graph.terminate_call(call.event.phone_number_id, call.event.call_id)
            )
            await _disconnect(call.connection)
        for task in list(self._tasks):
            task.cancel()


__all__ = [
    "CallHooks",
    "CallRunner",
    "LiveCall",
    "WhatsAppCalls",
    "run_receptionist",
    "whatsapp_sdp",
]
