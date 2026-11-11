"""Scorers, scorecard and the CI gate, on hand-made runs; plus one full offline run."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from receptionist_agent.brain.backend import Booking
from receptionist_agent.llm_fallback import gemini_target
from receptionist_agent.testing import CallTools, Say, ScriptedLLM, Step, last_tool_result
from receptionist_evals.__main__ import gate_on_baseline
from receptionist_evals.llm import EvalLLM
from receptionist_evals.report import ScenarioResult, metrics, to_markdown
from receptionist_evals.runner import CallRun, ToolCallRecord, run_scenario
from receptionist_evals.scenario import Scenario
from receptionist_evals.scorers import (
    check_booking,
    check_mentions,
    check_no_double_booking,
    check_outcome,
    check_price_numbers,
    check_slots_free,
    rule_checks,
)
from receptionist_shared.api_models import BookingStatus
from receptionist_shared.tenant import TenantConfig

TENANTS = Path(__file__).resolve().parents[2] / "tenants"
WED_14 = datetime.fromisoformat("2026-11-18T14:00:00+05:00")


def scenario(**expect: Any) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "t",
            "title": "test",
            "tenant": "demo-salon",
            "now": "2026-11-16T10:00:00+05:00",
            "persona": "p",
            "goal": "g",
            "expect": {"outcome": "booked", **expect},
        }
    )


def booking(
    code: str, start: datetime, phone: str = "+923215550101", name: str = "Sara"
) -> Booking:
    return Booking(
        id=code,
        tenant_slug="demo-salon",
        service="Haircut",
        start=start,
        end=start + timedelta(minutes=45),
        caller_name=name,
        phone=phone,
        status=BookingStatus.CONFIRMED,
    )


def run(s: Scenario, salon: TenantConfig, **kw: Any) -> CallRun:
    return CallRun(scenario=s, tenant=salon, ended_by="caller", **kw)


def booked() -> ToolCallRecord:
    return ToolCallRecord("create_booking", {}, {"ok": True, "booked": True}, True)


def test_booking_check_compares_backend_state(salon: TenantConfig) -> None:
    s = scenario(
        booking={"service": "Haircut", "start": WED_14.isoformat(), "phone": "+923215550101"}
    )
    good = run(s, salon, bookings=[booking("A", WED_14)], tool_calls=[booked()])
    assert check_booking(good).passed is True
    wrong_time = run(s, salon, bookings=[booking("A", WED_14 + timedelta(hours=1))])
    assert check_booking(wrong_time).passed is False
    assert "start" in check_booking(wrong_time).detail
    wrong_phone = run(s, salon, bookings=[booking("A", WED_14, phone="+923215550110")])
    assert check_booking(wrong_phone).passed is False


def test_day_only_booking(salon: TenantConfig) -> None:
    s = scenario(booking={"service": "Haircut", "day": "2026-11-18", "phone": "+923215550101"})
    assert check_booking(run(s, salon, bookings=[booking("A", WED_14 + timedelta(hours=3))])).passed


def test_outcome_comes_from_tool_calls_not_words(salon: TenantConfig) -> None:
    s = scenario()
    talked_only = run(s, salon, transcript=[("agent", "Your appointment is booked!")])
    assert check_outcome(talked_only).passed is False


def test_double_booking_and_free_slots(salon: TenantConfig) -> None:
    s = scenario(no_booking_at=[WED_14.isoformat()])
    clash = run(
        s, salon, bookings=[booking("A", WED_14), booking("B", WED_14 + timedelta(minutes=30))]
    )
    assert check_no_double_booking(clash).passed is False
    assert check_slots_free(clash).passed is False
    seeded = run(s, salon, bookings=[booking("SEED0000", WED_14)], seeded={"SEED0000"})
    assert check_slots_free(seeded).passed is True  # it was there before the call


def test_mentions_are_case_insensitive_regexes(salon: TenantConfig) -> None:
    s = scenario(must_mention=["2,?500|two thousand"], must_not_mention=["discount"])
    ok = run(s, salon, transcript=[("agent", "A haircut is Two thousand five hundred rupees.")])
    assert check_mentions(ok).passed is True
    bad = run(s, salon, transcript=[("agent", "Sure, a DISCOUNT for you.")])
    assert check_mentions(bad).passed is False


def test_price_numbers_must_come_from_the_facts(salon: TenantConfig) -> None:
    s = scenario()
    real = run(s, salon, transcript=[("agent", "A haircut is 2,500 rupees and colour from 8,000.")])
    assert check_price_numbers(real).passed is True
    phone = run(s, salon, transcript=[("agent", "Your number is 0300 123 4567, booked for 2026.")])
    assert check_price_numbers(phone).passed is True
    invented = run(s, salon, transcript=[("agent", "Bridal makeup is about 25,000 rupees.")])
    check = check_price_numbers(invented)
    assert check.passed is False and "25000" in check.detail


def result(id: str, passed: bool, *, booking_case: bool = True) -> ScenarioResult:
    from receptionist_evals.scorers import Check

    return ScenarioResult(
        id=id, title=id, tags=["booking"], language="en", caller_turns=3, seconds=1.0,
        ended_by="caller", booking_case=booking_case,
        checks=[Check("outcome", passed), Check("booking", passed)],
    )  # fmt: skip


def test_metrics_and_markdown_diff() -> None:
    now = [result("a", True), result("b", False), result("c", True), result("d", True)]
    m = metrics(now)
    assert m["booking_accuracy"] == 75.0 and m["pass_rate"] == 75.0
    previous = {"metrics": {"booking_accuracy": 100.0}, "scenarios": [{"id": "b", "passed": True}]}
    md = to_markdown(now, previous=previous, title="t")
    assert "| Booking accuracy | 75.0% | -25.0 |" in md
    assert "`b`: pass -> fail" in md


def test_gate_allows_two_points(tmp_path: Path) -> None:
    base = tmp_path / "baseline.json"
    base.write_text(json.dumps({"booking_accuracy": 80.0}))
    assert gate_on_baseline({"booking_accuracy": 78.0}, base, 2.0) is True
    assert gate_on_baseline({"booking_accuracy": 77.9}, base, 2.0) is False


# ---------------------------------------------------------------- one scenario, fully offline


def agent_script(messages: Any) -> Step:
    last = messages[-1]
    if last.get("role") == "tool":
        result = json.loads(last_tool_result(messages) or "{}")
        if "options" in result:
            return Say("Wednesday at two is free. Your name and number?")
        if result.get("booked") is False:
            return Say("Haircut, Wednesday 2 pm, Sara, 0321 555 0101. Shall I book that?")
        return Say("Booked. Anything else?")
    user = str(last.get("content", "")).lower()
    if "haircut" in user:
        return CallTools(
            [
                (
                    "check_availability",
                    {"service": "Haircut", "date": "2026-11-18", "preferred_time": "14:00"},
                )
            ]
        )
    if "sara" in user:
        args = {
            "service": "Haircut",
            "start": WED_14.isoformat(),
            "caller_name": "Sara Ahmed",
            "phone": "0321 5550101",
        }
        return CallTools([("create_booking", args | {"caller_confirmed": False})])
    if user.startswith("yes"):
        args = {
            "service": "Haircut",
            "start": WED_14.isoformat(),
            "caller_name": "Sara Ahmed",
            "phone": "0321 5550101",
        }
        return CallTools([("create_booking", args | {"caller_confirmed": True})])
    return Say("How can I help?")


async def test_full_offline_run_books_and_scores(monkeypatch: Any) -> None:
    import receptionist_evals.runner as runner_mod
    from receptionist_shared import Settings

    monkeypatch.setattr(runner_mod, "build_llm", lambda *_: ScriptedLLM(agent_script))
    lines = iter(
        [
            "I'd like a haircut on Wednesday at 2 pm.",
            "Sara Ahmed, 0321 5550101.",
            "Yes please. Thanks, bye! <END>",
        ]
    )

    def caller_api(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"choices": [{"message": {"content": next(lines)}}]})

    caller = EvalLLM([gemini_target("m", "k")], network=httpx2.MockTransport(caller_api))
    s = scenario(
        booking={
            "service": "Haircut",
            "start": WED_14.isoformat(),
            "caller_name": "Sara",
            "phone": "+923215550101",
        }
    )
    done = await run_scenario(s, Settings(_env_file=None), caller, tenants_dir=TENANTS)
    await caller.aclose()
    assert done.error is None and done.ended_by == "caller"
    failed = [c for c in rule_checks(done) if c.passed is False]
    assert failed == [], failed
