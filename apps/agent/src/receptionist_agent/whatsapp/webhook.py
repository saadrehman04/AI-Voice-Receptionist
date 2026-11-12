"""Meta's webhooks: prove they come from Meta, and pull out the call events.

- `verify_signature`: Meta signs each POST body with the app secret (`X-Hub-Signature-256:
  sha256=<hex HMAC>`). Anything unsigned or mis-signed is rejected before it is parsed, so a
  stranger who finds the URL can't make the agent "answer" calls or run up LLM usage.
- `call_events`: a tolerant parser. The same webhook also carries message and status updates;
  those are ignored (and still answered 200, or Meta keeps retrying them).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Literal

CallEventKind = Literal["connect", "terminate"]


def sign(secret: str, body: bytes) -> str:
    """The header value Meta would send for `body` (also used by the simulator and tests)."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    return hmac.compare_digest(sign(secret, body), header)


def verify_subscription(params: dict[str, str], expected_token: str) -> str | None:
    """Meta's one-time GET when the webhook URL is saved: echo `hub.challenge` if the token
    matches, else None."""
    token = params.get("hub.verify_token", "")
    if params.get("hub.mode") != "subscribe" or not params.get("hub.challenge"):
        return None
    if not hmac.compare_digest(token, expected_token):
        return None
    return params["hub.challenge"]


@dataclass(frozen=True)
class CallEvent:
    kind: CallEventKind
    call_id: str
    phone_number_id: str  # the business number that was called
    caller: str  # the caller's WhatsApp id: their number, digits only ("923001234567")
    sdp: str | None = None  # connect: the caller's SDP offer
    status: str | None = None  # terminate: COMPLETED / FAILED


def call_events(payload: Any) -> list[CallEvent]:
    events: list[CallEvent] = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return events
    for entry in payload.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            if (change or {}).get("field") != "calls":
                continue
            value = change.get("value") or {}
            number = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for call in value.get("calls") or []:
                kind = call.get("event")
                if kind not in ("connect", "terminate") or not call.get("id") or not number:
                    continue
                session = call.get("session") or {}
                events.append(
                    CallEvent(
                        kind=kind,
                        call_id=str(call["id"]),
                        phone_number_id=number,
                        caller=str(call.get("from") or ""),
                        sdp=session.get("sdp") if session.get("sdp_type") == "offer" else None,
                        status=call.get("status"),
                    )
                )
    return events


__all__ = [
    "CallEvent",
    "call_events",
    "sign",
    "verify_signature",
    "verify_subscription",
]
