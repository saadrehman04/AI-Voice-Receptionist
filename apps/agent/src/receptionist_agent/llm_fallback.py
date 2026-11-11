"""LLM fallback across free-tier models, below the OpenAI client (DECISIONS D23).

Groq's free tier allows 8,000 tokens per minute *per model*. One LLM request of ours is about
1,500 tokens (prompt + tools) plus the conversation, and a turn with a tool call makes two or
three requests, so a single model runs out after one or two caller turns a minute. When that
happens the OpenAI client waits out `retry-after` (we measured 16 s stalls): dead air on a call.

`FallbackTransport` is an `httpx2` transport (the OpenAI SDK's HTTP library) that the Groq
client sends through. For each chat
completion it tries a list of targets in order (for example Groq gpt-oss-120b -> Groq
gpt-oss-20b -> Gemini Flash-Lite through Google's OpenAI-compatible endpoint):

- A 429, a 5xx or a connection error moves on to the next target at once. This happens before
  any response bytes reach Pipecat, so the pipeline sees one normal streamed reply and no caller
  turn is lost.
- A target that said "slow down" is skipped until its cooldown ends (`retry-after`, or Groq's
  `x-ratelimit-reset-tokens` when the remaining token budget is too small for this request), so
  we don't pay a round trip to hear "429" again.
- If every target is cooling down we still try them all, soonest-ready first, rather than fail.

Gemini's OpenAI-compatible endpoint needs two repairs (`gemini_compat=True`), both found live:

- Gemini 3 rejects earlier tool calls that lack a `thought_signature`, which the OpenAI client
  does not keep (and a Groq tool call never had). Google documents a placeholder signature for
  exactly this case; we add it to outgoing messages.
- Its streamed tool calls carry no `index`. Pipecat's parser starts a new call whenever the
  index changes, so `None` produced an empty-named phantom call; Gemini then rejected the next
  request (400) and the call went silent. `GeminiStreamFix` rewrites the SSE stream so every
  tool call has an index and each chunk holds one call. (We ask for an uncompressed stream,
  since a transport sees the bytes before the client decompresses them.)
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
from loguru import logger

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"

DEFAULT_429_COOLDOWN_SECS = 20.0
ERROR_COOLDOWN_SECS = 5.0
CHARS_PER_TOKEN = 4  # rough, for "will this request fit in the remaining budget?"


@dataclass(frozen=True)
class LlmTarget:
    name: str  # for logs, e.g. "groq:openai/gpt-oss-120b"
    url: str  # full chat-completions URL
    api_key: str = field(repr=False)
    model: str
    extra: Mapping[str, Any] = field(default_factory=dict)  # params set on every request
    drop: tuple[str, ...] = ()  # params this provider rejects
    gemini_compat: bool = False  # repairs for Gemini's OpenAI-compatible endpoint


def groq_target(model: str, api_key: str, *, reasoning_effort: str | None) -> LlmTarget:
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    drop = () if reasoning_effort else ("reasoning_effort",)
    return LlmTarget(f"groq:{model}", GROQ_CHAT_URL, api_key, model, extra, drop)


def gemini_target(model: str, api_key: str) -> LlmTarget:
    # "low" measured faster to first token than the default on 3.5 Flash-Lite; "none" is rejected.
    return LlmTarget(
        f"gemini:{model}",
        GEMINI_CHAT_URL,
        api_key,
        model,
        {"reasoning_effort": "low"},
        gemini_compat=True,
    )


def parse_duration(raw: str | None) -> float | None:
    """`retry-after: 7` or Groq's `x-ratelimit-reset-tokens: 1m2.5s` / `32.1s` / `577ms`."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        pass
    total, matched = 0.0, False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", raw):
        matched = True
        total += float(value) * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]
    return total if matched else None


def with_placeholder_signatures(messages: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                google = (call.get("extra_content") or {}).get("google") or {}
                if not google.get("thought_signature"):
                    call = call | {
                        "extra_content": {
                            "google": google | {"thought_signature": GEMINI_PLACEHOLDER_SIGNATURE}
                        }
                    }
                calls.append(call)
            message = message | {"tool_calls": calls}
        out.append(message)
    return out


class GeminiStreamFix(httpx2.AsyncByteStream):
    """Give every streamed tool call an `index`, one call per SSE event (see module doc)."""

    def __init__(self, inner: httpx2.AsyncByteStream) -> None:
        self._inner = inner
        self._index_by_id: dict[str, int] = {}
        self._last_index = -1

    def _index_for(self, call: dict[str, Any]) -> int:
        call_id = call.get("id")
        if call_id is None:  # a continuation chunk of the call before it
            return max(self._last_index, 0)
        if call_id not in self._index_by_id:
            self._index_by_id[call_id] = len(self._index_by_id)
        self._last_index = self._index_by_id[call_id]
        return self._last_index

    def fix_line(self, line: bytes) -> bytes:
        if not line.startswith(b"data: ") or line.strip() == b"data: [DONE]":
            return line
        try:
            chunk = json.loads(line[6:])
            delta = chunk["choices"][0]["delta"]
            calls = delta["tool_calls"]
        except (ValueError, KeyError, IndexError, TypeError):
            return line
        if not isinstance(calls, list) or not calls:
            return line
        events = []
        for call in calls:
            if call.get("index") is None:
                call = call | {"index": self._index_for(call)}
            choice = chunk["choices"][0] | {"delta": delta | {"tool_calls": [call]}}
            one = chunk | {"choices": [choice]}
            events.append(b"data: " + json.dumps(one).encode())
        return b"\n\n".join(events)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        buffer = b""
        async for part in self._inner:
            buffer += part
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                yield self.fix_line(line) + b"\n"
        if buffer:
            yield self.fix_line(buffer)

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class _Budget:
    ready_at: float = 0.0  # monotonic time before which we skip this target
    remaining_tokens: int | None = None
    tokens_reset_at: float = 0.0


class FallbackTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        targets: Sequence[LlmTarget],
        *,
        inner: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_served: Callable[[LlmTarget, int], None] | None = None,
    ) -> None:
        if not targets:
            raise ValueError("FallbackTransport needs at least one target")
        self.targets = list(targets)
        self._inner = inner or httpx2.AsyncHTTPTransport()
        self._clock = clock
        self._budgets = {t.name: _Budget() for t in self.targets}
        self._on_served = on_served

    # ------------------------------------------------------------ ordering

    def _estimate_tokens(self, body: Mapping[str, Any]) -> int:
        size = len(json.dumps(body.get("messages", []))) + len(json.dumps(body.get("tools", [])))
        return size // CHARS_PER_TOKEN

    def _usable_now(self, target: LlmTarget, tokens: int, now: float) -> bool:
        b = self._budgets[target.name]
        if now < b.ready_at:
            return False
        budget_known = b.remaining_tokens is not None and now < b.tokens_reset_at
        return not (budget_known and (b.remaining_tokens or 0) < tokens)

    def order(self, body: Mapping[str, Any]) -> list[LlmTarget]:
        """Targets to try: usable ones in configured order, then the rest by soonest ready."""
        now, tokens = self._clock(), self._estimate_tokens(body)
        ready = [t for t in self.targets if self._usable_now(t, tokens, now)]

        def ready_at(t: LlmTarget) -> float:
            b = self._budgets[t.name]
            return max(b.ready_at, b.tokens_reset_at)

        return ready + sorted((t for t in self.targets if t not in ready), key=ready_at)

    # ------------------------------------------------------------ bookkeeping

    def _note_headers(self, target: LlmTarget, response: httpx2.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        reset = parse_duration(response.headers.get("x-ratelimit-reset-tokens"))
        if remaining is not None and remaining.isdigit() and reset is not None:
            b = self._budgets[target.name]
            b.remaining_tokens = int(remaining)
            b.tokens_reset_at = self._clock() + reset

    def _cool(self, target: LlmTarget, secs: float) -> None:
        self._budgets[target.name].ready_at = self._clock() + secs

    # ------------------------------------------------------------ requests

    def _request_for(
        self, original: httpx2.Request, target: LlmTarget, body: dict[str, Any]
    ) -> httpx2.Request:
        payload = {k: v for k, v in body.items() if k not in target.drop}
        payload.update(target.extra)
        payload["model"] = target.model
        if target.gemini_compat and "messages" in payload:
            payload["messages"] = with_placeholder_signatures(payload["messages"])
        headers = {
            k: v
            for k, v in original.headers.items()
            if k.lower() not in ("authorization", "host", "content-length")
        }
        headers["authorization"] = f"Bearer {target.api_key}"
        if target.gemini_compat:
            headers["accept-encoding"] = "identity"  # GeminiStreamFix reads raw bytes
        return httpx2.Request(
            original.method,
            target.url,
            headers=headers,
            content=json.dumps(payload).encode(),
            extensions=original.extensions,
        )

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return await self._inner.handle_async_request(request)
        body = json.loads(await request.aread())
        last: httpx2.Response | None = None
        tried: list[str] = []
        for target in self.order(body):
            tried.append(target.name)
            try:
                response = await self._inner.handle_async_request(
                    self._request_for(request, target, body)
                )
            except httpx2.TransportError as exc:
                logger.warning(
                    "llm target unreachable", target=target.name, error=type(exc).__name__
                )
                self._cool(target, ERROR_COOLDOWN_SECS)
                continue
            self._note_headers(target, response)
            if response.status_code == 429 or response.status_code >= 500:
                await response.aread()
                await response.aclose()
                wait = parse_duration(response.headers.get("retry-after"))
                if wait is None:
                    limited = response.status_code == 429
                    wait = DEFAULT_429_COOLDOWN_SECS if limited else ERROR_COOLDOWN_SECS
                self._cool(target, wait)
                logger.warning(
                    "llm target busy, trying next",
                    target=target.name,
                    status=response.status_code,
                    cooldown_secs=wait,
                )
                last = response
                continue
            if len(tried) > 1 or target is not self.targets[0]:
                logger.info("llm served by fallback", target=target.name, tried=tried)
            if self._on_served is not None:
                self._on_served(target, response.status_code)
            if target.gemini_compat and isinstance(response.stream, httpx2.AsyncByteStream):
                response = httpx2.Response(
                    response.status_code,
                    headers=[
                        (k, v) for k, v in response.headers.items() if k.lower() != "content-length"
                    ],
                    stream=GeminiStreamFix(response.stream),
                    request=request,
                    extensions=response.extensions,
                )
            return response
        if last is not None:
            return httpx2.Response(
                last.status_code, headers=last.headers, content=last.content, request=request
            )
        return httpx2.Response(
            503,
            json={"error": {"message": f"no LLM reachable (tried {', '.join(tried)})"}},
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = [
    "GEMINI_CHAT_URL",
    "GROQ_CHAT_URL",
    "FallbackTransport",
    "GeminiStreamFix",
    "LlmTarget",
    "gemini_target",
    "groq_target",
    "parse_duration",
    "with_placeholder_signatures",
]
