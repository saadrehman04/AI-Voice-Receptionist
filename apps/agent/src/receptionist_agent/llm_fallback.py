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
- Phase 9 (D37): a target that doesn't start answering in time (its `timeout_secs`) counts as
  down; a 401/403 (key pulled or revoked) cools it for minutes; a target that failed for a passing
  reason (timeout, connection, 5xx) is retried once. If nothing answers, `degrade=True` returns
  the scripted message-taking reply from `degraded.py` instead of an error, so the caller is
  never left in silence.

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

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx2
from loguru import logger

from receptionist_agent.degraded import degraded_reply

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"

DEFAULT_429_COOLDOWN_SECS = 20.0
ERROR_COOLDOWN_SECS = 5.0
REJECTED_KEY_COOLDOWN_SECS = 300.0  # 401/403: the key was pulled or revoked; check again later
# How long a target gets to start answering. Groq usually starts in ~0.5-1 s, Gemini in ~1-6 s
# (measured in Phase 5): the limits sit above a healthy slow answer, so a slow provider isn't
# cut off and retried for nothing, but a hung one doesn't keep the caller waiting.
GROQ_TIMEOUT_SECS = 6.0
GEMINI_TIMEOUT_SECS = 10.0
TARGET_TIMEOUT_SECS = 8.0  # targets that don't set their own
REQUEST_DEADLINE_SECS = 20.0  # then the scripted message-taker answers
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
    timeout_secs: float | None = None  # time to start answering; None = TARGET_TIMEOUT_SECS


def groq_target(model: str, api_key: str, *, reasoning_effort: str | None) -> LlmTarget:
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    drop = () if reasoning_effort else ("reasoning_effort",)
    return LlmTarget(
        f"groq:{model}", GROQ_CHAT_URL, api_key, model, extra, drop, timeout_secs=GROQ_TIMEOUT_SECS
    )


def gemini_target(model: str, api_key: str) -> LlmTarget:
    # "low" measured faster to first token than the default on 3.5 Flash-Lite; "none" is rejected.
    return LlmTarget(
        f"gemini:{model}",
        GEMINI_CHAT_URL,
        api_key,
        model,
        {"reasoning_effort": "low"},
        gemini_compat=True,
        timeout_secs=GEMINI_TIMEOUT_SECS,
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


# Words a short "simple" caller turn is made of (English and Urdu, as Whisper writes them).
SIMPLE_WORDS = frozenset(
    {
        # English
        *("yes", "yeah", "yep", "yup", "no", "nope", "ok", "okay", "sure", "alright", "all"),
        *("thanks", "thank", "you", "correct", "right", "that's", "that", "is"),
        *("great", "fine", "good", "perfect", "please", "bye", "goodbye"),
        # Urdu
        *("جی", "ہاں", "نہیں", "ٹھیک", "ہے", "شکریہ", "بہت", "اچھا", "درست", "بالکل"),
        *("خدا", "حافظ", "اوکے"),
    }
)
MAX_SIMPLE_WORDS = 5
_WORD = re.compile(r"[\w']+", re.UNICODE)


def last_caller_text(body: Mapping[str, Any]) -> str | None:
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else None
    return None


def is_simple_turn(body: Mapping[str, Any]) -> bool:
    """A yes/no/thanks-type caller turn: short, and only made of SIMPLE_WORDS."""
    text = last_caller_text(body)
    if not text:
        return False
    words = [w.lower() for w in _WORD.findall(text)]
    return 0 < len(words) <= MAX_SIMPLE_WORDS and all(w in SIMPLE_WORDS for w in words)


@dataclass
class _Budget:
    """What we know about one target's rate limits.

    Groq's per-minute token bucket refills continuously; its headers give what is left and
    how long a full refill takes, so the refill rate follows. A 429 (e.g. the 200k tokens/day
    limit) sets `ready_at` from `retry-after`.
    """

    ready_at: float = 0.0  # monotonic time before which we skip this target
    rejected_until: float = 0.0  # its key was refused (401/403): leave it out until then
    remaining_tokens: float | None = None
    limit_tokens: float | None = None  # None: provider didn't say; assume no refill until reset
    noted_at: float = 0.0
    full_at: float = 0.0  # when the bucket is full again
    refill_per_sec: float = 0.0

    def available(self, now: float) -> float | None:
        """Tokens we can spend now (None = unknown, so assume enough)."""
        if self.remaining_tokens is None or now >= self.full_at:
            return None
        if self.limit_tokens is None:
            return self.remaining_tokens
        refilled = self.remaining_tokens + (now - self.noted_at) * self.refill_per_sec
        return min(self.limit_tokens, refilled)

    def enough_at(self, tokens: int) -> float:
        """When this target can take a request of `tokens` (now or later)."""
        if self.remaining_tokens is None or self.remaining_tokens >= tokens:
            return self.ready_at
        if self.limit_tokens is None or self.refill_per_sec <= 0:
            return max(self.ready_at, self.full_at)
        when = self.noted_at + (tokens - self.remaining_tokens) / self.refill_per_sec
        return max(self.ready_at, min(when, self.full_at))


def _provider(target: LlmTarget) -> str:
    """The provider part of a target name ("groq" for "groq:openai/gpt-oss-120b")."""
    return target.name.split(":", 1)[0]


class BudgetBook:
    """Rate-limit state per (target, API key). Limits are per account, not per call, so every
    call in the process shares one book (`SHARED_BUDGETS`) and learns from the others."""

    def __init__(self) -> None:
        self._budgets: dict[tuple[str, str], _Budget] = {}

    def __getitem__(self, target: LlmTarget) -> _Budget:
        key_id = hashlib.sha256(target.api_key.encode()).hexdigest()[:12]  # never the key itself
        return self._budgets.setdefault((target.name, key_id), _Budget())


SHARED_BUDGETS = BudgetBook()


@dataclass(frozen=True)
class _Failure:
    kind: str  # "passing" (retry once), "busy" (429), "refused" (other 4xx, incl. a bad key)
    response: httpx2.Response | None = None


class FallbackTransport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        targets: Sequence[LlmTarget],
        *,
        inner: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_served: Callable[[LlmTarget, int], None] | None = None,
        simple_target: str | None = None,
        budgets: BudgetBook | None = None,  # default: shared by every call in the process
        degrade: bool = False,  # answer with the scripted message-taker when nothing answers
        timeout_secs: float | None = None,  # overrides every target's own (evals: patient)
        deadline_secs: float = REQUEST_DEADLINE_SECS,
    ) -> None:
        if not targets and not degrade:
            raise ValueError("FallbackTransport needs at least one target")
        self.targets = list(targets)
        if simple_target is not None and simple_target not in {t.name for t in self.targets}:
            raise ValueError(f"simple_target {simple_target!r} is not one of the targets")
        self.simple_target = simple_target  # tried first for yes/no/thanks turns
        self._inner = inner or httpx2.AsyncHTTPTransport()
        self._clock = clock
        self._budgets = budgets if budgets is not None else SHARED_BUDGETS
        self.on_served = on_served  # (target, status) for each answered request
        self.degrade = degrade
        self.timeout_secs = timeout_secs
        self.deadline_secs = deadline_secs
        self.estimated_tokens = 0  # prompt tokens sent in answered requests (chars / 4)
        self.exhausted = 0  # requests where every target was busy or down
        self.degraded = 0  # of those, answered by the scripted message-taker

    # ------------------------------------------------------------ ordering

    def _estimate_tokens(self, body: Mapping[str, Any]) -> int:
        # ensure_ascii=False: escaped Urdu ("\\u06a9") would count six times its real length.
        text = json.dumps(body.get("messages", []), ensure_ascii=False)
        text += json.dumps(body.get("tools", []), ensure_ascii=False)
        return len(text) // CHARS_PER_TOKEN

    def _usable_now(self, target: LlmTarget, tokens: int, now: float) -> bool:
        b = self._budgets[target]
        if now < b.ready_at:
            return False
        available = b.available(now)
        return available is None or available >= tokens

    def order(self, body: Mapping[str, Any]) -> list[LlmTarget]:
        """Targets to try, best first. A target whose key was just refused is left out: asking
        again can't help.

        Only a real refusal (a 429 or an error, which sets a cool-down) moves a target to the
        back. A budget that only *looks* short from the headers is an estimate, so it reorders
        targets of the same provider but never puts another provider first: a Groq 429 comes
        back in a fraction of a second, while Gemini takes seconds to answer. Seen live: three
        quick requests made Groq look short, Gemini went first and the caller waited 10 s twice,
        then Groq answered in 0.6 s.
        """
        now, tokens = self._clock(), self._estimate_tokens(body)
        live = [t for t in self.targets if now >= self._budgets[t].rejected_until]
        providers = list(dict.fromkeys(_provider(t) for t in self.targets))
        simple = self.simple_target is not None and is_simple_turn(body)

        def rank(t: LlmTarget) -> tuple[bool, int, bool, int]:
            usable = self._usable_now(t, tokens, now)
            small_first = simple and usable and t.name == self.simple_target  # D26
            return (not small_first, providers.index(_provider(t)), not usable, live.index(t))

        def ready_at(t: LlmTarget) -> float:
            return self._budgets[t].enough_at(tokens)

        open_ = sorted((t for t in live if now >= self._budgets[t].ready_at), key=rank)
        cooling = sorted((t for t in live if t not in open_), key=ready_at)
        return open_ + cooling

    # ------------------------------------------------------------ bookkeeping

    def _note_headers(self, target: LlmTarget, response: httpx2.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining-tokens")
        reset = parse_duration(response.headers.get("x-ratelimit-reset-tokens"))
        if remaining is None or not remaining.isdigit() or reset is None:
            return
        b = self._budgets[target]
        limit = response.headers.get("x-ratelimit-limit-tokens")
        b.limit_tokens = float(limit) if limit and limit.isdigit() else None
        b.remaining_tokens = float(remaining)
        b.noted_at = self._clock()
        b.full_at = b.noted_at + reset
        missing = (b.limit_tokens or b.remaining_tokens) - b.remaining_tokens
        # A full refill takes `reset` seconds, so tokens come back at missing / reset per second.
        b.refill_per_sec = missing / reset if reset > 0 else 0.0

    def _cool(self, target: LlmTarget, secs: float) -> None:
        self._budgets[target].ready_at = self._clock() + secs

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

    async def _attempt(
        self, request: httpx2.Request, target: LlmTarget, body: dict[str, Any], limit_secs: float
    ) -> httpx2.Response | _Failure:
        """One try at one target: its response, or why it failed ("passing" can be retried)."""
        try:
            response = await asyncio.wait_for(
                self._inner.handle_async_request(self._request_for(request, target, body)),
                timeout=limit_secs,
            )
        except (httpx2.TransportError, TimeoutError) as exc:
            logger.warning("llm target unreachable", target=target.name, error=type(exc).__name__)
            self._cool(target, ERROR_COOLDOWN_SECS)
            return _Failure("passing")
        self._note_headers(target, response)
        status = response.status_code
        if status < 400:
            return response
        await response.aread()
        await response.aclose()
        if status == 429 or status >= 500:
            wait = parse_duration(response.headers.get("retry-after"))
            if wait is None:
                wait = DEFAULT_429_COOLDOWN_SECS if status == 429 else ERROR_COOLDOWN_SECS
            self._cool(target, wait)
            logger.warning(
                "llm target busy, trying next",
                target=target.name,
                status=status,
                cooldown_secs=wait,
            )
            return _Failure("busy" if status == 429 else "passing", response)
        if status in (401, 403):
            self._budgets[target].rejected_until = self._clock() + REJECTED_KEY_COOLDOWN_SECS
            logger.error(
                "llm key rejected, skipping this target", target=target.name, status=status
            )
        else:
            logger.warning("llm target refused the request", target=target.name, status=status)
        return _Failure("refused", response)

    def _served(
        self,
        request: httpx2.Request,
        target: LlmTarget,
        body: dict[str, Any],
        tried: list[str],
        response: httpx2.Response,
    ) -> httpx2.Response:
        if len(tried) > 1 or (target is not self.targets[0] and target.name != self.simple_target):
            logger.info("llm served by fallback", target=target.name, tried=tried)
        self.estimated_tokens += self._estimate_tokens(body)
        if self.on_served is not None:
            self.on_served(target, response.status_code)
        if target.gemini_compat and isinstance(response.stream, httpx2.AsyncByteStream):
            return httpx2.Response(
                response.status_code,
                headers=[
                    (k, v) for k, v in response.headers.items() if k.lower() != "content-length"
                ],
                stream=GeminiStreamFix(response.stream),
                request=request,
                extensions=response.extensions,
            )
        return response

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method != "POST" or not request.url.path.endswith("/chat/completions"):
            return await self._inner.handle_async_request(request)
        body = json.loads(await request.aread())
        last: httpx2.Response | None = None
        tried: list[str] = []
        deadline = self._clock() + self.deadline_secs
        order = self.order(body) if self.targets else []
        retry: list[LlmTarget] = []
        # Every target once, then one more try for the first that failed for a passing reason.
        for round_targets in (order, retry):
            for target in round_targets:
                left = deadline - self._clock()
                if left <= 0:
                    logger.warning("llm request deadline passed", tried=tried)
                    break
                tried.append(target.name)
                own = self.timeout_secs or target.timeout_secs or TARGET_TIMEOUT_SECS
                outcome = await self._attempt(request, target, body, min(own, left))
                if isinstance(outcome, httpx2.Response):
                    return self._served(request, target, body, tried, outcome)
                last = outcome.response or last
                if outcome.kind == "passing" and not retry and round_targets is order:
                    retry.append(target)
        self.exhausted += 1
        if self.degrade and body.get("stream"):
            self.degraded += 1
            logger.error("no LLM answered; the scripted receptionist takes a message", tried=tried)
            return httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=degraded_reply(body),
                request=request,
            )
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
    "SHARED_BUDGETS",
    "BudgetBook",
    "FallbackTransport",
    "GeminiStreamFix",
    "LlmTarget",
    "gemini_target",
    "groq_target",
    "is_simple_turn",
    "parse_duration",
    "with_placeholder_signatures",
]
