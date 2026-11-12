"""When no LLM can answer: a scripted receptionist that takes a message (DECISIONS D37).

`FallbackTransport` calls `degraded_reply` once every LLM target has failed for a request
(rate-limited, down, timed out, or its key rejected). Instead of an error, which Pipecat would
turn into silence, the pipeline gets a normal streamed reply, so the caller hears:

1. "Sorry, I can't reach our booking system right now. Please tell me your name, your phone
   number and a short message..." (the ask);
2. after the caller answers: a `take_message` tool call with what they said, which runs through
   the normal tool code (saved for the owner, shown on the dashboard);
3. after the tool result: "Thank you, I've passed your message to the team. Goodbye." and an
   `end_call` tool call.

It is stateless: the step comes from the conversation in the request, so the same code works
whatever the transport saw before, and the real LLM can take over again mid-call if it comes
back (it sees the whole exchange in its context). It says nothing about the business, so it
cannot invent a fact; the business's hand-off details stay with the tools and the prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from receptionist_shared.phone import normalize_pk_phone

MESSAGE_CALL_PREFIX = "degraded_msg_"
END_CALL_PREFIX = "degraded_end_"

ASK = {
    "en": (
        "I'm sorry, I can't reach our booking system right now. Please tell me your name, "
        "your phone number and a short message, and someone from the team will call you back."
    ),
    "ur": (
        "معاف کیجیے، اس وقت ہمارا بکنگ سسٹم دستیاب نہیں ہے۔ براہِ کرم اپنا نام، فون نمبر "
        "اور مختصر پیغام بتا دیں، ٹیم میں سے کوئی آپ کو واپس کال کرے گا۔"
    ),
}
THANKS = {
    "en": "Thank you, I've passed your message to the team. Goodbye.",
    "ur": "شکریہ، آپ کا پیغام ٹیم تک پہنچا دیا ہے۔ خدا حافظ۔",
}
NOT_SAVED = {
    "en": "I'm sorry, I couldn't save your message. Please call again a little later. Goodbye.",
    "ur": (
        "معاف کیجیے، آپ کا پیغام محفوظ نہیں ہو سکا۔ "
        "براہِ کرم تھوڑی دیر بعد دوبارہ کال کریں۔ خدا حافظ۔"
    ),
}

_URDU = re.compile("[\u0600-\u06ff]")
# Digits (ASCII, Arabic-Indic, Urdu), at least 9 of them, with spaces or dashes between.
_DIGIT = r"\d" + "\u0660-\u0669\u06f0-\u06f9"
_PHONE = re.compile(rf"\+?[{_DIGIT}](?:[{_DIGIT}\s\-]{{7,}}[{_DIGIT}])")
_ASK_PREFIXES = tuple(text[:24] for text in ASK.values())


def _messages(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [m for m in body.get("messages") or [] if isinstance(m, dict)]


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # [{"type": "text", "text": ...}]
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def language_of(messages: Sequence[Mapping[str, Any]]) -> str:
    """Urdu if the caller's last words were in Urdu script, else English."""
    for message in reversed(messages):
        if message.get("role") == "user" and _text(message).strip():
            return "ur" if _URDU.search(_text(message)) else "en"
    return "en"


def is_ask(message: Mapping[str, Any]) -> bool:
    return message.get("role") == "assistant" and _text(message).strip().startswith(_ASK_PREFIXES)


def find_phone(text: str) -> str | None:
    for candidate in _PHONE.findall(text):
        phone = normalize_pk_phone(candidate)
        if phone is not None:
            return phone
    return None


def _call_id(prefix: str, messages: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False, default=str).encode())
    return prefix + digest.hexdigest()[:10]


def _tool_result(messages: Sequence[Mapping[str, Any]], prefix: str) -> dict[str, Any] | None:
    """The result of our own tool call, if the conversation ends with one."""
    last = messages[-1] if messages else None
    if last is None or last.get("role") != "tool":
        return None
    if not str(last.get("tool_call_id", "")).startswith(prefix):
        return None
    try:
        result = json.loads(_text(last))
    except ValueError:
        return {}
    return result if isinstance(result, dict) else {}


def plan(body: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """What the scripted receptionist says next: (text, tool calls)."""
    messages = _messages(body)
    lang = language_of(messages)
    if _tool_result(messages, END_CALL_PREFIX) is not None:
        return None, []  # hung up already; nothing more to say
    saved = _tool_result(messages, MESSAGE_CALL_PREFIX)
    if saved is not None:
        end = _function(_call_id(END_CALL_PREFIX, messages), "end_call", {"reason": "degraded"})
        return (THANKS if saved.get("ok") else NOT_SAVED)[lang], [end]
    asked_at = next((i for i in range(len(messages) - 1, -1, -1) if is_ask(messages[i])), None)
    if asked_at is not None:
        said = " ".join(
            _text(m).strip()
            for m in messages[asked_at + 1 :]
            if m.get("role") == "user" and _text(m).strip()
        )
        if said:
            args: dict[str, Any] = {"message": said}
            phone = find_phone(said)
            if phone is not None:
                args["phone"] = phone
            return None, [_function(_call_id(MESSAGE_CALL_PREFIX, messages), "take_message", args)]
    return ASK[lang], []


def _function(call_id: str, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def _chunk(delta: Mapping[str, Any], finish: str | None = None) -> bytes:
    chunk = {
        "id": "degraded",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "degraded",
        "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}],
    }
    return b"data: " + json.dumps(chunk, ensure_ascii=False).encode() + b"\n\n"


def degraded_reply(body: Mapping[str, Any]) -> bytes:
    """The scripted reply as an OpenAI-style server-sent event stream."""
    text, calls = plan(body)
    out = _chunk({"role": "assistant", "content": ""})
    if text:
        out += _chunk({"content": text})
    for index, call in enumerate(calls):
        out += _chunk({"tool_calls": [call | {"index": index}]})
    out += _chunk({}, "tool_calls" if calls else "stop")
    return out + b"data: [DONE]\n\n"


__all__ = [
    "ASK",
    "END_CALL_PREFIX",
    "MESSAGE_CALL_PREFIX",
    "NOT_SAVED",
    "THANKS",
    "degraded_reply",
    "find_phone",
    "language_of",
    "plan",
]
