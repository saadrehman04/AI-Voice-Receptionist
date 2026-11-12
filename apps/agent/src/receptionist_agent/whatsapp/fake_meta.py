"""A local stand-in for Meta, to test and demo WhatsApp calls with no Meta account.

Two halves, both speaking Meta's real wire formats so the agent can't tell the difference:

- `FakeGraph`: the Graph API endpoints the agent calls (`/{number}/calls` for pre_accept,
  accept, reject, terminate; `/{number}/messages` for the confirmation text). It checks the
  bearer token and records everything.
- `FakePhone`: the caller's WhatsApp app. A real WebRTC peer (aiortc, Opus audio) that makes
  an SDP offer, delivers it in a signed `connect` webhook, takes the SDP answer from the
  agent's `accept`, then talks (pre-recorded or synthesised speech) and listens.

Demo (two terminals; docs/WHATSAPP.md):
    WHATSAPP_GRAPH_URL=http://127.0.0.1:7870/v23.0 make agent
    make whatsapp-sim ARGS='--say "A haircut on Wednesday at 3 pm" --say "Sara, 0321 5550101"'
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fractions
import json
import time
import uuid
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame
from fastapi import FastAPI, Header, HTTPException
from pydantic import SecretStr

from receptionist_agent.whatsapp.webhook import sign

RATE = 16000  # what we feed aiortc (it resamples to 48 kHz Opus); also the recording rate
FRAME_SAMPLES = RATE // 50  # 20 ms
SPEECH_RMS = 300  # int16 RMS above this counts as the agent speaking


# ---------------------------------------------------------------- Graph API stand-in


@dataclass
class FakeGraph:
    token: str
    actions: list[dict[str, Any]] = field(default_factory=list)  # every /calls request, in order
    messages: list[dict[str, Any]] = field(default_factory=list)  # every /messages request
    _answers: dict[str, asyncio.Future[str]] = field(default_factory=dict)

    def answer_for(self, call_id: str) -> asyncio.Future[str]:
        """Resolves with the SDP answer from the agent's `accept` for this call."""
        if call_id not in self._answers:
            self._answers[call_id] = asyncio.get_running_loop().create_future()
        return self._answers[call_id]

    def app(self) -> FastAPI:
        app = FastAPI(title="Fake Meta Graph API")

        def check(authorization: str | None) -> None:
            if authorization != f"Bearer {self.token}":
                raise HTTPException(401, {"error": {"message": "bad token", "code": 190}})

        @app.post("/{version}/{phone_number_id}/calls")
        async def calls(
            version: str,
            phone_number_id: str,
            body: dict[str, Any],
            authorization: str | None = Header(None),
        ) -> dict[str, bool]:
            check(authorization)
            self.actions.append({"phone_number_id": phone_number_id, **body})
            if body.get("action") == "accept":
                future = self.answer_for(str(body["call_id"]))
                if not future.done():
                    future.set_result(str(body["session"]["sdp"]))
            return {"success": True}

        @app.post("/{version}/{phone_number_id}/messages")
        async def messages(
            version: str,
            phone_number_id: str,
            body: dict[str, Any],
            authorization: str | None = Header(None),
        ) -> dict[str, Any]:
            check(authorization)
            self.messages.append({"phone_number_id": phone_number_id, **body})
            return {
                "messaging_product": "whatsapp",
                "contacts": [{"input": body.get("to"), "wa_id": body.get("to")}],
                "messages": [{"id": f"wamid.fake{len(self.messages)}"}],
            }

        return app

    def did(self, call_id: str) -> list[str]:
        return [a["action"] for a in self.actions if a.get("call_id") == call_id]


# ---------------------------------------------------------------- the caller's phone


class CallerTrack(MediaStreamTrack):
    """What the caller says: queued speech, silence in between (like a real microphone)."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: list[np.ndarray] = []
        self._pts = 0
        self._start: float | None = None
        self.done = asyncio.Event()
        self.done.set()

    def say(self, pcm16: bytes) -> None:
        samples = np.frombuffer(pcm16, dtype=np.int16)
        for i in range(0, len(samples), FRAME_SAMPLES):
            chunk = samples[i : i + FRAME_SAMPLES]
            self._queue.append(np.pad(chunk, (0, FRAME_SAMPLES - len(chunk))))
        self.done.clear()

    async def recv(self) -> AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        wait = self._start + self._pts / RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)  # real time, 20 ms per frame
        if self._queue:
            samples = self._queue.pop(0)
            if not self._queue:
                self.done.set()
        else:
            samples = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        frame = AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, RATE)
        self._pts += FRAME_SAMPLES
        return frame


@dataclass
class Heard:
    """The agent's audio as the caller heard it (16 kHz mono), with simple speech detection."""

    pcm: bytearray = field(default_factory=bytearray)
    last_speech: float = 0.0
    first_speech: float | None = None

    def add(self, samples: np.ndarray) -> None:
        self.pcm.extend(samples.astype(np.int16).tobytes())
        if samples.size and float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) > SPEECH_RMS:
            now = time.monotonic()
            self.last_speech = now
            if self.first_speech is None:
                self.first_speech = now

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / RATE


class FakePhone:
    """One WhatsApp user calling one business number."""

    def __init__(self, *, caller: str = "923215550101") -> None:
        self.caller = caller  # wa_id: the caller's number, digits only
        self.call_id = f"wacid.FAKE{uuid.uuid4().hex[:24]}"
        self.mic = CallerTrack()
        self.heard = Heard()
        self._pc = RTCPeerConnection()
        self._pc.addTrack(self.mic)
        self._listener: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind == "audio":
                self._listener = asyncio.ensure_future(self._listen(track))

        @self._pc.on("connectionstatechange")
        async def on_state() -> None:
            if self._pc.connectionState == "connected":
                self.connected.set()

    async def _listen(self, track: MediaStreamTrack) -> None:
        from av.audio.resampler import AudioResampler

        resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
        try:
            while True:
                frame = await track.recv()
                if not isinstance(frame, AudioFrame):
                    continue
                for out in resampler.resample(frame):
                    self.heard.add(out.to_ndarray().reshape(-1))
        except Exception:  # the track ends when the call does
            return

    def _webhook(self, phone_number_id: str, call: dict[str, Any]) -> bytes:
        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "WABA_FAKE",
                    "changes": [
                        {
                            "field": "calls",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "15550000000",
                                    "phone_number_id": phone_number_id,
                                },
                                "contacts": [{"wa_id": self.caller, "profile": {"name": "Sim"}}],
                                "calls": [{"id": self.call_id, "from": self.caller, **call}],
                            },
                        }
                    ],
                }
            ],
        }
        return json.dumps(payload).encode()

    async def _post(
        self, client: httpx.AsyncClient, url: str, secret: str, body: bytes
    ) -> httpx.Response:
        return await client.post(
            url,
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sign(secret, body)},
        )

    async def ring(
        self,
        client: httpx.AsyncClient,
        webhook_url: str,
        secret: str,
        phone_number_id: str,
        graph: FakeGraph,
        *,
        answer_timeout: float = 30.0,
    ) -> float:
        """Call the business; returns seconds until the agent accepted (SDP answer back)."""
        await self._pc.setLocalDescription(await self._pc.createOffer())
        # aiortc finishes gathering ICE candidates inside setLocalDescription, so the offer
        # already carries them (Meta needs them in the offer: there's no trickle ICE).
        started = time.monotonic()
        body = self._webhook(
            phone_number_id,
            {
                "event": "connect",
                "timestamp": str(int(time.time())),
                "direction": "USER_INITIATED",
                "session": {"sdp_type": "offer", "sdp": self._pc.localDescription.sdp},
            },
        )
        response = await self._post(client, webhook_url, secret, body)
        response.raise_for_status()
        answer = await asyncio.wait_for(graph.answer_for(self.call_id), answer_timeout)
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        return time.monotonic() - started

    async def say(self, pcm16: bytes) -> None:
        self.mic.say(pcm16)
        await self.mic.done.wait()

    async def wait_for_reply(self, *, quiet_secs: float = 2.5, within: float = 40.0) -> bool:
        """Wait until the agent has spoken and then gone quiet. False if it hasn't `within`."""
        asked = time.monotonic()
        deadline = asked + within
        while time.monotonic() < deadline:
            spoke_since = self.heard.last_speech > asked
            if spoke_since and time.monotonic() - self.heard.last_speech > quiet_secs:
                return True
            await asyncio.sleep(0.1)
        return False

    async def hang_up(
        self, client: httpx.AsyncClient, webhook_url: str, secret: str, phone_number_id: str
    ) -> None:
        body = self._webhook(
            phone_number_id,
            {
                "event": "terminate",
                "timestamp": str(int(time.time())),
                "direction": "USER_INITIATED",
                "status": "COMPLETED",
            },
        )
        await self._post(client, webhook_url, secret, body)
        await self.close()

    async def close(self) -> None:
        await self._pc.close()
        if self._listener is not None:
            self._listener.cancel()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(self.heard.pcm))


# ---------------------------------------------------------------- CLI demo


async def demo(args: argparse.Namespace) -> int:
    import uvicorn

    from receptionist_agent.bench.synth import Synth, caller_audio
    from receptionist_shared import get_settings

    settings = get_settings()
    secret = _plain(settings.whatsapp_app_secret)
    token = _plain(settings.whatsapp_access_token)
    number = args.number or settings.whatsapp_phone_number_id or ""
    if not (secret and token and number):
        print("Set WHATSAPP_APP_SECRET, WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID")
        print("(any values work with the simulator; see docs/WHATSAPP.md), or pass --number.")
        return 2

    graph = FakeGraph(token=token)
    server = uvicorn.Server(
        uvicorn.Config(graph.app(), host="127.0.0.1", port=args.graph_port, log_level="warning")
    )
    serving = asyncio.ensure_future(server.serve())
    await _until(lambda: server.started)
    print(
        f"fake Graph API on http://127.0.0.1:{args.graph_port}/v23.0 "
        "(start the agent with WHATSAPP_GRAPH_URL pointing here)"
    )

    synth = Synth()
    lines = [
        caller_audio(f"wa-sim-{i}", text, args.language, Path("data/bench"), synth)
        for i, text in enumerate(args.say)
    ]
    phone = FakePhone(caller=args.caller)
    webhook = f"{args.agent.rstrip('/')}/whatsapp"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            answered = await phone.ring(client, webhook, secret, number, graph)
        except (TimeoutError, httpx.HTTPError) as exc:
            print(f"the agent didn't answer: {type(exc).__name__} {exc}")
            server.should_exit = True
            await serving
            return 1
        print(f"answered in {answered * 1000:.0f} ms ({', '.join(graph.did(phone.call_id))})")
        await asyncio.wait_for(phone.connected.wait(), 15)
        await phone.wait_for_reply()
        print(f"  agent greeted ({phone.heard.seconds:.1f} s of audio so far)")
        for text, pcm in zip(args.say, lines, strict=True):
            print(f"  caller: {text}")
            await phone.say(pcm)
            replied = await phone.wait_for_reply()
            print("  agent replied" if replied else "  (no reply)")
            if "terminate" in graph.did(phone.call_id):
                print("  the agent hung up")
                break
        with contextlib.suppress(TimeoutError):  # after a goodbye, the agent may hang up first
            await asyncio.wait_for(_until(lambda: "terminate" in graph.did(phone.call_id)), 5)
        if "terminate" not in graph.did(phone.call_id):
            await phone.hang_up(client, webhook, secret, number)
        else:
            await phone.close()
        await asyncio.sleep(2)  # let a confirmation message arrive

    out = Path("data/whatsapp-sim") / f"{phone.call_id}.wav"
    phone.save(out)
    print(f"Graph API calls: {', '.join(graph.did(phone.call_id))}")
    for message in graph.messages:
        print(f"WhatsApp message to +{message['to']}: {message['text']['body']}")
    if not graph.messages:
        print("no WhatsApp message was sent (nothing was booked, moved or cancelled)")
    print(f"what the caller heard: {out}  (the call is on the dashboard if the api is running)")
    server.should_exit = True
    await serving
    return 0


def _plain(value: SecretStr | None) -> str:
    return value.get_secret_value() if value else ""


async def _until(ready: Callable[[], bool], every: float = 0.05) -> None:
    """Poll a flag another library owns (uvicorn's `started` has no event to wait on)."""
    while not ready():  # noqa: ASYNC110
        await asyncio.sleep(every)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Call the agent over a simulated WhatsApp")
    parser.add_argument("--say", action="append", default=[], help="a caller line (repeatable)")
    parser.add_argument("--language", choices=["en", "ur"], default="en")
    parser.add_argument("--agent", default="http://127.0.0.1:7860", help="the agent's base URL")
    parser.add_argument("--number", help="the business's phone number id (default from .env)")
    parser.add_argument("--caller", default="923215550101", help="the caller's number, digits")
    parser.add_argument("--graph-port", type=int, default=7870)
    raise SystemExit(asyncio.run(demo(parser.parse_args(argv))))


if __name__ == "__main__":
    main()
