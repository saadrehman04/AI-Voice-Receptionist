"""Run one scenario: the simulated caller talks to the real agent brain, in text.

Same prompt, tools, CallState and LLM (with its fallback) as a phone call; only STT/TTS are
skipped. The clock is frozen at the scenario's `now`, and the in-memory backend is seeded with
its bookings, so every run starts from the same world.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from receptionist_agent.brain.backend import Booking, InMemoryBackend
from receptionist_agent.brain.prompt import build_system_prompt
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.text_session import TextSession
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_agent.providers import build_llm
from receptionist_evals.caller import caller_turn
from receptionist_evals.llm import EvalLLM, RateLimitedError
from receptionist_evals.scenario import Scenario
from receptionist_shared import Settings
from receptionist_shared.api_models import BookingStatus
from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

AGENT_TURN_LIMIT_SECS = 90.0


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    ok: bool


@dataclass
class CallRun:
    scenario: Scenario
    tenant: TenantConfig
    transcript: list[tuple[str, str]] = field(default_factory=list)  # (agent|caller, text)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    bookings: list[Booking] = field(default_factory=list)  # backend state at the end
    seeded: set[str] = field(default_factory=set)  # booking codes that existed before the call
    caller_turns: int = 0
    ended_by: str = "max_turns"  # caller | agent | max_turns | error
    error: str | None = None
    inconclusive: str | None = None  # free-tier limits got in the way; not the agent's fault
    seconds: float = 0.0
    llm_targets: list[str] = field(default_factory=list)
    agent_tokens_estimate: int = 0
    heard: list[str] = field(default_factory=list)  # audio mode: what STT made of each line

    @property
    def agent_text(self) -> str:
        return "\n".join(text for speaker, text in self.transcript if speaker == "agent")


def seed(backend: InMemoryBackend, scenario: Scenario, tenant: TenantConfig) -> set[str]:
    codes: set[str] = set()
    for i, b in enumerate(scenario.seed_bookings):
        service = tenant.find_service(b.service)
        if service is None:
            raise ValueError(f"{scenario.id}: seed booking for unknown service {b.service!r}")
        code = b.code or f"SEED{i:04d}"
        start = b.start.astimezone(tenant.tz)
        backend.bookings[code] = Booking(
            id=code,
            tenant_slug=tenant.slug,
            service=service.name,
            start=start,
            end=start + timedelta(minutes=service.duration_min),
            caller_name=b.caller_name,
            phone=b.phone,
            status=BookingStatus.CONFIRMED,
        )
        codes.add(code)
    return codes


async def run_scenario(
    scenario: Scenario, settings: Settings, caller_llm: EvalLLM, *, tenants_dir: Path
) -> CallRun:
    tenant = load_tenant_by_slug(scenario.tenant, tenants_dir)
    now = scenario.now.astimezone(tenant.tz)

    def clock() -> Any:
        return now

    run = CallRun(scenario=scenario, tenant=tenant)
    backend = InMemoryBackend(clock=clock)
    run.seeded = seed(backend, scenario, tenant)

    def on_tool(
        name: str, args: dict[str, Any], result: dict[str, Any], ok: bool, _ms: int
    ) -> None:
        run.tool_calls.append(ToolCallRecord(name, args, result, ok))

    state = CallState()
    tools = ReceptionistTools(
        tenant,
        state,
        backend,
        call_id=f"eval-{uuid.uuid4().hex[:8]}",
        clock=clock,
        on_tool_call=on_tool,
    )
    llm = build_llm(settings, build_system_prompt(tenant, now))
    fallback = getattr(llm, "fallback", None)
    if fallback is not None:

        def served(target: Any, _status: int) -> None:
            run.llm_targets.append(target.name)

        fallback.on_served = served

    started = time.perf_counter()
    try:
        async with TextSession(tenant=tenant, llm=llm, tools=tools, state=state) as session:
            run.transcript.append(("agent", session.greeting))
            for _ in range(scenario.expect.max_turns):
                line, done = await caller_turn(caller_llm, scenario, tenant, run.transcript)
                if line:
                    run.transcript.append(("caller", line))
                    run.caller_turns += 1
                    reply = await session.send(line, limit_secs=AGENT_TURN_LIMIT_SECS)
                    run.transcript.append(("agent", reply.text))
                    if reply.ended:
                        run.ended_by = "agent"
                        break
                if done:
                    run.ended_by = "caller"
                    break
    except RateLimitedError as exc:
        run.inconclusive = f"caller: {exc}"
        run.ended_by = "error"
    except Exception as exc:  # one broken scenario must not stop the suite
        logger.exception("scenario failed to run", scenario=scenario.id)
        run.error = f"{type(exc).__name__}: {exc}"
        run.ended_by = "error"
    run.seconds = round(time.perf_counter() - started, 1)
    run.bookings = list(backend.bookings.values())
    if fallback is not None:
        run.agent_tokens_estimate = getattr(fallback, "estimated_tokens", 0)
        if getattr(fallback, "exhausted", 0) and run.inconclusive is None:
            run.inconclusive = f"agent: every LLM was rate-limited {fallback.exhausted} time(s)"
    return run


# ---------------------------------------------------------------- audio mode


async def run_scenario_audio(
    scenario: Scenario, settings: Settings, caller_llm: EvalLLM, *, tenants_dir: Path, synth: Any
) -> CallRun:
    """The same scenario as speech: caller text -> TTS -> the full voice pipeline (VAD, Groq
    STT, LLM, TTS) -> what the agent said. Tests that names and numbers survive STT."""
    from receptionist_agent.bench.run import BenchCall

    tenant = load_tenant_by_slug(scenario.tenant, tenants_dir)
    now = scenario.now.astimezone(tenant.tz)
    run = CallRun(scenario=scenario, tenant=tenant)
    backend = InMemoryBackend(clock=lambda: now)
    run.seeded = seed(backend, scenario, tenant)

    def on_tool(
        name: str, args: dict[str, Any], result: dict[str, Any], ok: bool, _ms: int
    ) -> None:
        run.tool_calls.append(ToolCallRecord(name, args, result, ok))

    started = time.perf_counter()
    try:
        async with BenchCall(
            settings, scenario.tenant, label=f"eval-{scenario.id}", backend=backend, now=now,
            on_tool_call=on_tool,
        ) as bench:  # fmt: skip
            run.transcript.append(("agent", _last_agent_line(bench) or "(greeting)"))
            for n in range(scenario.expect.max_turns):
                line, done = await caller_turn(caller_llm, scenario, tenant, run.transcript)
                if line:
                    pcm = synth.urdu(line) if scenario.language == "ur" else synth.english(line)
                    turn = await bench.turn(f"{scenario.id}-{n}", "eval", scenario.language, pcm)
                    run.transcript.append(("caller", line))
                    run.heard.append(turn.transcript)
                    run.caller_turns += 1
                    run.transcript.append(("agent", turn.reply))
                    if bench.ended:
                        run.ended_by = "agent"
                        break
                if done:
                    run.ended_by = "caller"
                    break
    except Exception as exc:
        logger.exception("audio scenario failed to run", scenario=scenario.id)
        run.error = f"{type(exc).__name__}: {exc}"
        run.ended_by = "error"
    run.seconds = round(time.perf_counter() - started, 1)
    run.bookings = list(backend.bookings.values())
    return run


def _last_agent_line(bench: Any) -> str:
    context = bench.context
    if context is None:
        return ""
    for m in reversed(context.get_messages()):
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and isinstance(m.get("content"), str)
        ):
            return str(m["content"])
    return ""
