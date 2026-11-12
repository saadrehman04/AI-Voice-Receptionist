"""WhatsApp calling (Phase 8), with the fake Meta: no network, no models, no Meta account.

The end-to-end test is a real WebRTC call: the fake phone (aiortc, Opus) sends a signed
`connect` webhook to the real routes, `WhatsAppCalls` answers through the fake Graph API, and a
small echo pipeline over Pipecat's WebRTC transport stands in for the receptionist, so the
caller must hear their own voice back.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

from receptionist_agent.whatsapp.calls import CallHooks, LiveCall, WhatsAppCalls, whatsapp_sdp
from receptionist_agent.whatsapp.confirm import confirmation_text
from receptionist_agent.whatsapp.fake_meta import RATE, FakeGraph, FakePhone
from receptionist_agent.whatsapp.graph import GraphApi, GraphError
from receptionist_agent.whatsapp.routes import install
from receptionist_agent.whatsapp.webhook import (
    call_events,
    sign,
    verify_signature,
    verify_subscription,
)
from receptionist_shared import Settings
from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

REPO = Path(__file__).resolve().parents[3]
SECRET = "test-app-secret"
TOKEN = "test-graph-token"
NUMBER = "100000000000001"  # demo-salon's (made-up) phone number id


@pytest.fixture
def salon() -> TenantConfig:
    return load_tenant_by_slug("demo-salon", REPO / "tenants")


# ---------------------------------------------------------------- webhook basics


def test_signature_must_match_the_exact_body() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    assert verify_signature(SECRET, body, sign(SECRET, body))
    assert not verify_signature(SECRET, body + b" ", sign(SECRET, body))
    assert not verify_signature("other-secret", body, sign(SECRET, body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "md5=abc")


def test_subscription_handshake() -> None:
    ok = {"hub.mode": "subscribe", "hub.challenge": "1158201444", "hub.verify_token": "vt"}
    assert verify_subscription(ok, "vt") == "1158201444"
    assert verify_subscription(ok | {"hub.verify_token": "nope"}, "vt") is None
    assert verify_subscription(ok | {"hub.mode": "unsubscribe"}, "vt") is None


async def test_parser_keeps_call_events_and_ignores_the_rest() -> None:
    phone = FakePhone()
    connect = json.loads(
        phone._webhook(NUMBER, {"event": "connect", "session": {"sdp_type": "offer", "sdp": "v=0"}})
    )
    messages = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {"messages": [{"id": "x"}]}}]}],
    }
    events = call_events(connect)
    assert len(events) == 1
    assert (events[0].kind, events[0].phone_number_id, events[0].sdp) == ("connect", NUMBER, "v=0")
    assert events[0].caller == phone.caller
    assert call_events(messages) == []
    assert call_events({"object": "page"}) == []
    await phone.close()


def test_answer_sdp_keeps_only_sha256_fingerprints() -> None:
    sdp = (
        "v=0\r\na=fingerprint:sha-256 AA\r\n"
        "a=fingerprint:sha-384 BB\r\na=fingerprint:sha-512 CC\r\n"
    )
    assert whatsapp_sdp(sdp) == "v=0\r\na=fingerprint:sha-256 AA\r\n"


# ---------------------------------------------------------------- confirmation texts


def test_confirmation_texts_come_from_the_stored_booking(salon: TenantConfig) -> None:
    booked = {
        "ok": True,
        "booked": True,
        "booking_id": "K7Q2XD",
        "service": "Haircut",
        "start": "2026-11-18T14:00:00+05:00",
    }
    en = confirmation_text(salon, "create_booking", booked, "en")
    assert en == (
        "Noor Beauty Salon: your Haircut is booked for Wednesday 18 November, 2:00 PM. "
        "Booking code: K7Q2XD. To change it, just call us again on WhatsApp."
    )
    ur = confirmation_text(salon, "create_booking", booked, "ur")
    assert ur is not None and ur.startswith("نور بیوٹی سیلون:")
    assert "ہیئر کٹ" in ur and "بدھ 18 نومبر، دوپہر 2:00 بجے" in ur and "K7Q2XD" in ur
    cancelled = confirmation_text(salon, "cancel_booking", booked | {"cancelled": True}, "en")
    assert cancelled is not None and "has been cancelled" in cancelled
    # read-backs (not yet booked), failures and other tools send nothing
    assert confirmation_text(salon, "create_booking", booked | {"booked": False}, "en") is None
    assert confirmation_text(salon, "check_availability", {"ok": True}, "en") is None


# ---------------------------------------------------------------- Graph API client


async def test_graph_client_sends_metas_request_shapes() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
        if seen[-1][1].get("call_id") == "bad":
            return httpx.Response(400, json={"error": {"message": "nope", "code": 138006}})
        return httpx.Response(200, json={"success": True})

    graph = GraphApi(
        "https://graph.example/v23.0/",
        TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await graph.answer_call(NUMBER, "c1", "pre_accept", "v=0")
    await graph.terminate_call(NUMBER, "c1")
    assert await graph.send_text(NUMBER, "923001234567", "hi") == "wamid.1"
    with pytest.raises(GraphError) as err:
        await graph.answer_call(NUMBER, "bad", "accept", "v=0")
    assert err.value.code == 138006 and err.value.status == 400
    assert seen[0] == (
        f"/v23.0/{NUMBER}/calls",
        {
            "messaging_product": "whatsapp",
            "call_id": "c1",
            "action": "pre_accept",
            "session": {"sdp_type": "answer", "sdp": "v=0"},
        },
    )
    assert seen[1][1]["action"] == "terminate"
    assert seen[2][1]["text"] == {"preview_url": False, "body": "hi"}
    await graph.aclose()


# ---------------------------------------------------------------- end to end


class Echo(FrameProcessor):
    """Plays the caller's audio straight back: proof that audio flows both ways."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            await self.push_frame(
                OutputAudioRawFrame(frame.audio, frame.sample_rate, frame.num_channels)
            )
        else:
            await self.push_frame(frame, direction)


def tone(seconds: float, hz: float = 440.0) -> bytes:
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16).tobytes()


class Rig:
    """Fake Meta + the real webhook routes + `WhatsAppCalls` with a pluggable call runner."""

    def __init__(self, salon: TenantConfig, runner: Any) -> None:
        self.graph = FakeGraph(token=TOKEN)
        graph_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.graph.app()), base_url="http://graph"
        )

        async def resolve(number: str) -> TenantConfig | None:
            return salon if number == NUMBER else None

        self.calls = WhatsAppCalls(
            GraphApi("http://graph/v23.0", TOKEN, client=graph_client),
            resolve,
            runner,
            ice_servers=[],  # local host candidates only: no STUN, no network
        )
        app = FastAPI()
        settings = Settings(
            _env_file=None,
            whatsapp_app_secret=SecretStr(SECRET),
            whatsapp_verify_token=SecretStr("vt"),
            whatsapp_access_token=SecretStr(TOKEN),
        )
        install(app, settings, calls=self.calls)
        self.agent = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        )

    async def aclose(self) -> None:
        await self.calls.aclose()
        await self.agent.aclose()


async def echo_runner(connection: SmallWebRTCConnection, call: LiveCall, hooks: CallHooks) -> None:
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    worker = PipelineWorker(Pipeline([transport.input(), Echo(), transport.output()]))

    async def gone(*_: Any) -> None:
        await worker.cancel()

    transport.add_event_handler("on_client_disconnected", gone)

    # A booking made during the call, as the receptionist's tool would report it.
    hooks.on_tool_call(
        "create_booking",
        {},
        {
            "ok": True,
            "booked": True,
            "booking_id": "K7Q2XD",
            "service": "Haircut",
            "start": "2026-11-18T14:00:00+05:00",
        },
        True,
        12,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def test_whatsapp_call_end_to_end(salon: TenantConfig) -> None:
    rig = Rig(salon, echo_runner)
    phone = FakePhone(caller="923215550101")
    try:
        answered = await phone.ring(rig.agent, "http://agent/whatsapp", SECRET, NUMBER, rig.graph)
        assert answered < 5
        assert rig.graph.did(phone.call_id) == ["pre_accept", "accept"]
        await asyncio.wait_for(phone.connected.wait(), 10)

        await phone.say(tone(1.0))
        heard = await phone.wait_for_reply(quiet_secs=0.5, within=10)
        assert heard, "the caller never heard the echo"

        # the booking was confirmed by WhatsApp text, to the caller's number
        await asyncio.sleep(0.2)
        assert [m["to"] for m in rig.graph.messages] == ["923215550101"]
        assert "K7Q2XD" in rig.graph.messages[0]["text"]["body"]

        await phone.hang_up(rig.agent, "http://agent/whatsapp", SECRET, NUMBER)
        for _ in range(50):  # the pipeline stops once the caller has gone
            if not rig.calls.live:
                break
            await asyncio.sleep(0.1)
        assert rig.calls.live == {}
        # the caller hung up, so we don't send Meta a terminate of our own
        assert rig.graph.did(phone.call_id) == ["pre_accept", "accept"]
    finally:
        await phone.close()
        await rig.aclose()


async def test_agent_hanging_up_tells_meta(salon: TenantConfig) -> None:
    async def hang_up_at_once(
        conn: SmallWebRTCConnection, call: LiveCall, hooks: CallHooks
    ) -> None:
        return  # like end_call: the call is over from our side

    rig = Rig(salon, hang_up_at_once)
    phone = FakePhone()
    try:
        await phone.ring(rig.agent, "http://agent/whatsapp", SECRET, NUMBER, rig.graph)
        for _ in range(50):
            if "terminate" in rig.graph.did(phone.call_id):
                break
            await asyncio.sleep(0.05)
        assert rig.graph.did(phone.call_id) == ["pre_accept", "accept", "terminate"]
    finally:
        await phone.close()
        await rig.aclose()


async def test_unsigned_or_unknown_calls_are_refused(salon: TenantConfig) -> None:
    rig = Rig(salon, echo_runner)
    phone = FakePhone()
    try:
        offer = phone._webhook(
            NUMBER, {"event": "connect", "session": {"sdp_type": "offer", "sdp": "v=0"}}
        )
        unsigned = await rig.agent.post("/whatsapp", content=offer)
        assert unsigned.status_code == 403
        forged = await rig.agent.post(
            "/whatsapp", content=offer, headers={"X-Hub-Signature-256": sign("wrong", offer)}
        )
        assert forged.status_code == 403

        unknown = phone._webhook(
            "999", {"event": "connect", "session": {"sdp_type": "offer", "sdp": "v=0"}}
        )
        r = await rig.agent.post(
            "/whatsapp", content=unknown, headers={"X-Hub-Signature-256": sign(SECRET, unknown)}
        )
        assert r.status_code == 200
        for _ in range(50):
            if rig.graph.actions:
                break
            await asyncio.sleep(0.02)
        assert [a["action"] for a in rig.graph.actions] == ["reject"]  # nobody owns that number

        sub = await rig.agent.get(
            "/whatsapp",
            params={"hub.mode": "subscribe", "hub.challenge": "42", "hub.verify_token": "vt"},
        )
        assert sub.status_code == 200 and sub.text == "42"
    finally:
        await phone.close()
        await rig.aclose()


def test_tenant_number_must_be_digits(salon: TenantConfig) -> None:
    assert salon.whatsapp_phone_number_id == NUMBER
    with pytest.raises(ValueError):
        TenantConfig.model_validate(salon.model_dump() | {"whatsapp_phone_number_id": "+92 300"})
