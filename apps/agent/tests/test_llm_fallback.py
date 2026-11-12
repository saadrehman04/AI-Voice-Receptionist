"""The free-tier LLM fallback: no network, a mock transport plays the providers.

The OpenAI SDK uses `httpx2` (its own fork of httpx), so the transport and these tests do too.
"""

import json
from typing import Any

import httpx2
import pytest

from receptionist_agent.groq_pool import PooledGroqLLMService
from receptionist_agent.llm_fallback import (
    GEMINI_CHAT_URL,
    GEMINI_PLACEHOLDER_SIGNATURE,
    GROQ_CHAT_URL,
    SHARED_BUDGETS,
    BudgetBook,
    FallbackTransport,
    GeminiStreamFix,
    gemini_target,
    groq_target,
    is_simple_turn,
    parse_duration,
)
from receptionist_agent.providers import ProviderConfigError, build_llm, llm_targets
from receptionist_shared import Settings

BIG = groq_target("openai/gpt-oss-120b", "gsk_big", reasoning_effort="low")
SMALL = groq_target("openai/gpt-oss-20b", "gsk_big", reasoning_effort="low")
GEMINI = gemini_target("gemini-3.5-flash-lite", "AIza_test")

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "check_availability", "arguments": "{}"},
}
BODY: dict[str, Any] = {
    "model": "openai/gpt-oss-120b",
    "stream": True,
    "reasoning_effort": "low",
    "messages": [
        {"role": "user", "content": "haircut tomorrow?"},
        {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
    ],
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Providers:
    """Answers per model with a scripted status; records every request it saw."""

    def __init__(self, statuses: dict[str, list[int]], headers: dict[str, dict[str, str]]) -> None:
        self.statuses = statuses
        self.headers = headers
        self.seen: list[tuple[str, dict[str, Any], httpx2.Headers]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            self.seen.append((str(request.url), {}, request.headers))
            return httpx2.Response(200, json={"data": []})
        body = json.loads(request.content)
        model = body["model"]
        self.seen.append((str(request.url), body, request.headers))
        queue = self.statuses.get(model, [200])
        status = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx2.Response(
            status,
            headers=self.headers.get(model, {}),
            json={"model": model} if status == 200 else {"error": {"message": "slow down"}},
        )


def make(
    statuses: dict[str, list[int]],
    headers: dict[str, dict[str, str]] | None = None,
    targets: list[Any] | None = None,
) -> tuple[httpx2.AsyncClient, Providers, Clock]:
    providers = Providers(statuses, headers or {})
    clock = Clock()
    transport = FallbackTransport(
        targets or [BIG, SMALL, GEMINI],
        inner=httpx2.MockTransport(providers),
        clock=clock,
        budgets=BudgetBook(),  # each test starts with nothing known
    )
    return httpx2.AsyncClient(transport=transport), providers, clock


async def post(client: httpx2.AsyncClient) -> httpx2.Response:
    return await client.post(GROQ_CHAT_URL, json=BODY, headers={"Authorization": "Bearer gsk_big"})


def models(providers: Providers) -> list[str]:
    return [body["model"] for _, body, _ in providers.seen]


async def test_main_model_serves_when_healthy() -> None:
    client, providers, _ = make({})
    response = await post(client)
    assert response.json()["model"] == "openai/gpt-oss-120b"
    assert models(providers) == ["openai/gpt-oss-120b"]


async def test_rate_limit_moves_to_the_next_model_at_once() -> None:
    client, providers, _ = make(
        {"openai/gpt-oss-120b": [429]}, {"openai/gpt-oss-120b": {"retry-after": "12"}}
    )
    response = await post(client)
    assert response.status_code == 200
    assert models(providers) == ["openai/gpt-oss-120b", "openai/gpt-oss-20b"]


async def test_cooling_target_is_skipped_until_its_cooldown_ends() -> None:
    client, providers, clock = make(
        {"openai/gpt-oss-120b": [429, 200]}, {"openai/gpt-oss-120b": {"retry-after": "12"}}
    )
    await post(client)
    providers.seen.clear()
    await post(client)  # 120b is cooling: no wasted round trip
    assert models(providers) == ["openai/gpt-oss-20b"]
    clock.now += 13
    providers.seen.clear()
    await post(client)
    assert models(providers) == ["openai/gpt-oss-120b"]


async def test_budget_refills_continuously_not_only_at_reset() -> None:
    """Groq's bucket refills steadily: 10 of 8000 left, full in 60 s = 133 tokens/s back."""
    headers = {
        "openai/gpt-oss-120b": {
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "10",
            "x-ratelimit-reset-tokens": "60s",
        }
    }
    client, providers, clock = make({}, headers)
    await post(client)
    needed = FallbackTransport([BIG], budgets=BudgetBook())._estimate_tokens(BODY)
    providers.seen.clear()
    await post(client)  # not enough yet
    assert models(providers) == ["openai/gpt-oss-20b"]
    clock.now += (needed - 10) / (7990 / 60) + 0.1  # just enough has refilled, long before 60 s
    providers.seen.clear()
    await post(client)
    assert models(providers)[0] == "openai/gpt-oss-120b"


def test_budgets_are_shared_across_calls_by_default() -> None:
    a, b = FallbackTransport([BIG]), FallbackTransport([BIG])
    assert a._budgets is b._budgets is SHARED_BUDGETS
    assert BudgetBook()[BIG] is not SHARED_BUDGETS[BIG]


def test_urdu_is_not_overcounted() -> None:
    urdu = {"messages": [{"role": "user", "content": "جی ہاں، ٹھیک ہے" * 20}]}
    english = {"messages": [{"role": "user", "content": "yes that's ok" * 20}]}
    t = FallbackTransport([BIG], budgets=BudgetBook())
    assert t._estimate_tokens(urdu) < 2 * t._estimate_tokens(english)


async def test_low_token_budget_skips_ahead_before_a_429() -> None:
    headers = {
        "openai/gpt-oss-120b": {
            "x-ratelimit-remaining-tokens": "10",
            "x-ratelimit-reset-tokens": "30.5s",
        }
    }
    client, providers, clock = make({}, headers)
    await post(client)  # served, but the headers say the budget is nearly gone
    providers.seen.clear()
    await post(client)
    assert models(providers) == ["openai/gpt-oss-20b"]
    clock.now += 31
    providers.seen.clear()
    await post(client)
    assert models(providers)[0] == "openai/gpt-oss-120b"


async def test_gemini_gets_its_key_model_params_and_placeholder_signatures() -> None:
    client, providers, _ = make({"openai/gpt-oss-120b": [429], "openai/gpt-oss-20b": [503]})
    response = await post(client)
    assert response.status_code == 200
    url, body, headers = providers.seen[-1]
    assert url == GEMINI_CHAT_URL
    assert headers["authorization"] == "Bearer AIza_test"
    assert body["model"] == "gemini-3.5-flash-lite"
    assert body["reasoning_effort"] == "low"
    call = body["messages"][1]["tool_calls"][0]
    assert call["extra_content"]["google"]["thought_signature"] == GEMINI_PLACEHOLDER_SIGNATURE
    # Groq requests went out with the Groq key and without Google-only fields.
    _, groq_body, groq_headers = providers.seen[0]
    assert groq_headers["authorization"] == "Bearer gsk_big"
    assert "extra_content" not in groq_body["messages"][1]["tool_calls"][0]


async def test_existing_signature_is_kept() -> None:
    client, providers, _ = make({}, targets=[GEMINI])
    signed = TOOL_CALL | {"extra_content": {"google": {"thought_signature": "real"}}}
    body = BODY | {"messages": [BODY["messages"][0], {"role": "assistant", "tool_calls": [signed]}]}
    await client.post(GROQ_CHAT_URL, json=body)
    sent = providers.seen[0][1]["messages"][1]["tool_calls"][0]
    assert sent["extra_content"]["google"]["thought_signature"] == "real"


async def test_everything_busy_still_tries_all_and_returns_the_last_error() -> None:
    client, providers, _ = make(
        {"openai/gpt-oss-120b": [429], "openai/gpt-oss-20b": [429], "gemini-3.5-flash-lite": [503]}
    )
    response = await post(client)
    assert response.status_code == 503
    # Each once, and Gemini (a passing 5xx, not a rate limit) retried once.
    assert models(providers) == [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
    ]
    # Next time everything is cooling: still tried, soonest-ready first, never refused outright.
    providers.seen.clear()
    await post(client)
    assert len(providers.seen) == 4


async def test_connection_error_falls_through() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if "groq" in str(request.url):
            raise httpx2.ConnectError("no route")
        return httpx2.Response(200, json={"ok": True})

    transport = FallbackTransport(
        [BIG, GEMINI], inner=httpx2.MockTransport(handler), budgets=BudgetBook()
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        assert (await post(client)).json() == {"ok": True}


async def test_non_chat_requests_pass_straight_through() -> None:
    client, providers, _ = make({})
    response = await client.get("https://api.groq.com/openai/v1/models")
    assert response.json() == {"data": []}
    assert providers.seen[0][0] == "https://api.groq.com/openai/v1/models"


@pytest.mark.parametrize(
    ("raw", "secs"),
    [("7", 7.0), ("32.1s", 32.1), ("577ms", 0.577), ("1m2.5s", 62.5), ("", None), ("soon", None)],
)
def test_parse_duration(raw: str, secs: float | None) -> None:
    got = parse_duration(raw)
    if secs is None:
        assert got is None
    else:
        assert got == pytest.approx(secs)


def settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"groq_api_key": "gsk_test", "gemini_api_key": "AIza_test"}
    return Settings(_env_file=None, **(base | over))


def test_targets_follow_config_order() -> None:
    names = [t.name for t in llm_targets(settings())]
    assert names == [
        "groq:openai/gpt-oss-120b",
        "groq:openai/gpt-oss-20b",
        "gemini:gemini-3.5-flash-lite",
    ]


def test_gemini_fallback_skipped_without_its_key() -> None:
    names = [t.name for t in llm_targets(settings(gemini_api_key=None))]
    assert names == ["groq:openai/gpt-oss-120b", "groq:openai/gpt-oss-20b"]


def test_fallbacks_can_be_turned_off_and_bad_entries_fail_fast() -> None:
    assert len(llm_targets(settings(llm_fallbacks=""))) == 1
    with pytest.raises(ProviderConfigError, match="provider:model"):
        llm_targets(settings(llm_fallbacks="gpt-oss-20b"))
    with pytest.raises(ProviderConfigError, match="not groq or gemini"):
        llm_targets(settings(llm_fallbacks="openai:gpt-5"))


def test_groq_llm_sends_through_the_fallback_transport() -> None:
    llm = build_llm(settings(), "prompt")
    assert isinstance(llm, PooledGroqLLMService)
    assert llm.fallback.targets[0].model == "openai/gpt-oss-120b"
    assert "gsk_test" not in repr(llm.fallback.targets[0])  # keys never in reprs/logs


# ---------------------------------------------------------------- Gemini stream repair


def sse(*chunks: dict[str, Any]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + (
        b"data: [DONE]\n\n"
    )


def gemini_tool_chunk(*calls: dict[str, Any]) -> dict[str, Any]:
    # As Gemini's OpenAI-compatible endpoint sends them: no "index" on the calls.
    return {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": list(calls)}}]}


def gemini_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
        "extra_content": {"google": {"thought_signature": "sig"}},
    }


class Chunks(httpx2.AsyncByteStream):
    def __init__(self, data: bytes, size: int) -> None:
        self.parts = [data[i : i + size] for i in range(0, len(data), size)]

    async def __aiter__(self) -> Any:
        for part in self.parts:
            yield part


@pytest.mark.parametrize("size", [7, 64, 100_000])  # events split across reads, or not
async def test_stream_fix_gives_each_tool_call_an_index(size: int) -> None:
    data = sse(
        gemini_tool_chunk(gemini_call("a", "check_availability", {}), gemini_call("b", "x", {})),
        gemini_tool_chunk(gemini_call("c", "lookup_business_info", {})),
        {"choices": [{"index": 0, "delta": {"content": "hello"}}]},
    )
    fixed = b"".join([part async for part in GeminiStreamFix(Chunks(data, size))])
    events = [
        json.loads(line[6:])
        for line in fixed.split(b"\n")
        if line.startswith(b"data: ") and line != b"data: [DONE]"
    ]
    deltas = [e["choices"][0]["delta"] for e in events]
    calls = [d["tool_calls"] for d in deltas if "tool_calls" in d]
    assert [(c[0]["index"], c[0]["id"]) for c in calls] == [(0, "a"), (1, "b"), (2, "c")]
    assert all(len(c) == 1 for c in calls)
    assert deltas[-1]["content"] == "hello"  # text chunks pass through untouched
    assert fixed.endswith(b"data: [DONE]\n\n")


async def test_gemini_fallback_through_real_openai_sdk_and_pipecat() -> None:
    """The bug this guards against was only visible end to end: OpenAI SDK + Pipecat's stream
    parser turned Gemini's index-less tool call into an extra empty-named call."""
    from datetime import datetime
    from pathlib import Path

    from receptionist_agent.brain.backend import InMemoryBackend
    from receptionist_agent.brain.state import CallState
    from receptionist_agent.brain.text_session import TextSession
    from receptionist_agent.brain.tools import ReceptionistTools
    from receptionist_shared.tenant import load_tenant_by_slug

    seen: list[dict[str, Any]] = []

    def network(request: httpx2.Request) -> httpx2.Response:
        if "groq" in str(request.url):
            return httpx2.Response(429, headers={"retry-after": "30"}, json={"error": {}})
        body = json.loads(request.content)
        seen.append(body)
        if body["messages"][-1]["role"] == "tool":
            reply = sse({"choices": [{"index": 0, "delta": {"content": "Wednesday works."}}]})
        else:
            args = {"service": "Haircut", "date": "2026-11-18"}
            reply = sse(gemini_tool_chunk(gemini_call("g1", "check_availability", args)))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=reply)

    tenant = load_tenant_by_slug("demo-salon", Path(__file__).parents[3] / "tenants")
    now = datetime(2026, 11, 16, 9, 0, tzinfo=tenant.tz)
    llm = PooledGroqLLMService(
        targets=[BIG, GEMINI],
        network=httpx2.MockTransport(network),
        api_key="gsk_big",
        settings=PooledGroqLLMService.Settings(model=BIG.model, system_instruction="test"),
    )
    state = CallState()
    tools = ReceptionistTools(tenant, state, InMemoryBackend(clock=lambda: now), call_id="g")
    async with TextSession(tenant=tenant, llm=llm, tools=tools, state=state) as session:
        reply = await session.send("A haircut on Wednesday?")
    assert [c.name for c in reply.tool_calls] == ["check_availability"]  # no phantom "" call
    assert reply.text == "Wednesday works."
    follow_up = seen[-1]["messages"]
    assistant = next(m for m in follow_up if m.get("tool_calls"))
    assert assistant["tool_calls"][0]["extra_content"]["google"]["thought_signature"]


# ---------------------------------------------------------------- simple-turn routing


def caller_says(text: str) -> dict[str, Any]:
    return BODY | {"messages": [*BODY["messages"], {"role": "user", "content": text}]}


@pytest.mark.parametrize(
    ("text", "simple"),
    [
        ("Yes, that's right.", True),
        ("Great, thank you.", True),
        ("جی ہاں، ٹھیک ہے۔", True),
        ("بہت شکریہ", True),
        ("No.", True),
        ("Yes, but can I make it four instead?", False),  # has content, and too long
        ("How much is a haircut?", False),
        ("", False),
    ],
)
def test_is_simple_turn(text: str, simple: bool) -> None:
    assert is_simple_turn(caller_says(text)) is simple


async def test_simple_turns_try_the_small_model_first() -> None:
    providers = Providers({}, {})
    transport = FallbackTransport(
        [BIG, SMALL, GEMINI],
        inner=httpx2.MockTransport(providers),
        simple_target=SMALL.name,
        budgets=BudgetBook(),
    )
    async with httpx2.AsyncClient(transport=transport) as client:
        await client.post(GROQ_CHAT_URL, json=caller_says("Yes, that's right."))
        await client.post(GROQ_CHAT_URL, json=caller_says("How much is a haircut?"))
    assert models(providers) == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]


def test_simple_target_must_be_a_target() -> None:
    with pytest.raises(ValueError, match="simple_target"):
        FallbackTransport([BIG], simple_target="groq:nope")


def test_simple_turn_model_is_wired_from_settings() -> None:
    llm = build_llm(settings(), "prompt")
    assert isinstance(llm, PooledGroqLLMService)
    assert llm.fallback.simple_target == "groq:openai/gpt-oss-20b"
    off = build_llm(settings(llm_simple_turn_model=""), "prompt")
    assert isinstance(off, PooledGroqLLMService)
    assert off.fallback.simple_target is None
