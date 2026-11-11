"""Scorers: did the call do the right thing? Checked against state, not against the agent's words.

Every check returns pass / fail / not applicable, with a short reason. A scenario passes when
none of its applicable checks fail.

- outcome: what the tool calls achieved (`outcome_from_tools`, the same rule the dashboard uses).
- booking: the caller's confirmed booking in the backend has the right service, time (or day),
  name and exact phone number.
- slots_free / no_new_booking: nothing was booked where it shouldn't be.
- no_double_booking: no two confirmed bookings overlap (with the tenant's buffer), ever.
- tools / mentions: required and forbidden tool calls and phrases.
- price_numbers: every price-like number the agent said comes from the tenant's facts (a rule
  check; the LLM judge covers numbers said as words).
- judge: Gemini, strict rubric: statements not supported by the facts or tool results.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from receptionist_agent.brain.backend import Booking
from receptionist_evals.llm import EvalLLM, EvalLLMError
from receptionist_evals.runner import CallRun
from receptionist_shared.api_models import BookingStatus
from receptionist_shared.outcome import ToolOutcome, outcome_from_tools
from receptionist_shared.tenant import TenantConfig


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool | None  # None = not applicable to this scenario
    detail: str = ""


def _confirmed(run: CallRun) -> list[Booking]:
    return [b for b in run.bookings if b.status == BookingStatus.CONFIRMED]


def check_ran(run: CallRun) -> Check:
    if run.error:
        return Check("ran", False, run.error[:200])
    done = run.ended_by in ("caller", "agent")
    return Check("ran", done, f"ended by {run.ended_by} after {run.caller_turns} caller turns")


def check_outcome(run: CallRun) -> Check:
    got = outcome_from_tools([ToolOutcome(t.name, t.ok, t.result) for t in run.tool_calls])
    want = run.scenario.expect.outcomes
    return Check("outcome", got in want, f"got {got}, want {' or '.join(want)}")


def check_booking(run: CallRun) -> Check:
    want = run.scenario.expect.booking
    if want is None:
        return Check("booking", None)
    tz = run.tenant.tz
    mine = [b for b in _confirmed(run) if want.phone is None or b.phone == want.phone]
    if want.phone is None:
        mine = [b for b in mine if b.id not in run.seeded]
    if not mine:
        found = ", ".join(f"{b.service} {b.start:%a %H:%M} {b.phone}" for b in _confirmed(run))
        return Check(
            "booking", False, f"no confirmed booking for {want.phone}; have: {found or 'none'}"
        )
    problems: list[str] = []
    b = mine[-1]
    if len(mine) > 1:
        problems.append(f"{len(mine)} bookings for this caller, want 1")
    if b.service.lower() != want.service.lower():
        problems.append(f"service {b.service!r}, want {want.service!r}")
    start = b.start.astimezone(tz)
    if want.start is not None and start != want.start.astimezone(tz):
        problems.append(f"start {start:%a %d %H:%M}, want {want.start.astimezone(tz):%a %d %H:%M}")
    if want.day is not None and start.date() != want.day:
        problems.append(f"day {start:%a %d}, want {want.day:%a %d}")
    if want.caller_name and want.caller_name.lower() not in b.caller_name.lower():
        problems.append(f"name {b.caller_name!r}, want {want.caller_name!r}")
    return Check("booking", not problems, "; ".join(problems) or f"{b.service} {start:%a %d %H:%M}")


def check_no_new_booking(run: CallRun) -> Check:
    """When no booking is expected, none may be created."""
    if run.scenario.expect.booking is not None or "booked" in run.scenario.expect.outcomes:
        return Check("no_new_booking", None)
    new = [b for b in _confirmed(run) if b.id not in run.seeded]
    return Check(
        "no_new_booking", not new, ", ".join(f"{b.service} {b.start:%a %H:%M}" for b in new)
    )


def check_slots_free(run: CallRun) -> Check:
    slots = run.scenario.expect.no_booking_at
    if not slots:
        return Check("slots_free", None)
    taken = [
        s for s in slots if any(b.start == s and b.id not in run.seeded for b in _confirmed(run))
    ]
    return Check("slots_free", not taken, ", ".join(f"{s:%a %H:%M}" for s in taken))


def check_no_double_booking(run: CallRun) -> Check:
    buffer = timedelta(minutes=run.tenant.booking_buffer_min)
    live = sorted(_confirmed(run), key=lambda b: b.start)
    clashes = [
        f"{a.id}/{b.id}"
        for i, a in enumerate(live)
        for b in live[i + 1 :]
        if b.start < a.end + buffer and a.start < b.end + buffer
    ]
    return Check("no_double_booking", not clashes, ", ".join(clashes))


def check_tools(run: CallRun) -> Check:
    expect = run.scenario.expect
    if not expect.tools_called and not expect.tools_not_called:
        return Check("tools", None)
    called = {t.name for t in run.tool_calls if t.ok}
    missing = [t for t in expect.tools_called if t not in called]
    forbidden = [t for t in expect.tools_not_called if t in called]
    detail = "; ".join(
        x
        for x in (
            f"missing {missing}" if missing else "",
            f"forbidden {forbidden}" if forbidden else "",
        )
        if x
    )
    return Check("tools", not missing and not forbidden, detail)


def check_mentions(run: CallRun) -> Check:
    expect = run.scenario.expect
    if not expect.must_mention and not expect.must_not_mention:
        return Check("mentions", None)
    text = run.agent_text
    missing = [p for p in expect.must_mention if not re.search(p, text, re.IGNORECASE)]
    said = [p for p in expect.must_not_mention if re.search(p, text, re.IGNORECASE)]
    parts = [f"never said /{p}/" for p in missing] + [f"said /{p}/" for p in said]
    return Check("mentions", not parts, "; ".join(parts))


PHONE = re.compile(r"\+?\d[\d \-]{7,}\d")
NUMBER = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?![\w])|(?<![\w.,])\d+(?![\w,])")
MIN_PRICE = 100  # smaller numbers are times, durations, counts


def numbers_in(text: str) -> set[int]:
    """Numbers written in `text` (with thousands commas), phone numbers removed."""
    return {int(m.replace(",", "")) for m in NUMBER.findall(PHONE.sub(" ", text))}


def allowed_numbers(tenant: TenantConfig) -> set[int]:
    allowed = {int(s.price) for s in tenant.services if s.price is not None}
    allowed |= {s.duration_min for s in tenant.services}
    texts = [
        tenant.address or "",
        *(f.answer for f in tenant.faq),
        *(f.answer_ur or "" for f in tenant.faq),
    ]
    texts += [str(v) for v in tenant.policies.model_dump().values() if v]
    for text in texts:
        allowed |= numbers_in(text)
    return allowed | {2026, 2027}


def check_price_numbers(run: CallRun) -> Check:
    """Every number >= 100 the agent said is a real price/fact from the tenant config."""
    allowed = allowed_numbers(run.tenant)
    for t in run.tool_calls:  # numbers a tool returned (e.g. a booking code) are fine to repeat
        allowed |= numbers_in(json.dumps(t.result))
    said = numbers_in(run.agent_text)
    invented = sorted(n for n in said if n >= MIN_PRICE and n not in allowed)
    return Check(
        "price_numbers", not invented, f"numbers not in the facts: {invented}" if invented else ""
    )


JUDGE_RUBRIC = """You check a phone receptionist AI for invented facts. Be strict and literal.

You get the business facts the receptionist was given, the results of the tools it called,
and the conversation. List every statement BY THE RECEPTIONIST that asserts a fact (a price,
opening hours, a service, staff, a policy, an address, availability, or that a booking/change/
cancellation was made) which is NOT supported by the business facts or a tool result.
Saying "I'll check with the team", asking questions, or repeating what the caller said is fine.
Also flag if it claims a booking, change or cancellation was done when no tool result shows it.

Reply with JSON only, in this shape:
{"invented": [{"quote": "...", "why": "..."}], "claimed_action_without_tool": true|false}"""


async def judge(run: CallRun, llm: EvalLLM, facts: str) -> Check:
    tools = [{"tool": t.name, "args": t.arguments, "result": t.result} for t in run.tool_calls]
    conversation = "\n".join(
        f"{'RECEPTIONIST' if s == 'agent' else 'CALLER'}: {x}" for s, x in run.transcript
    )
    user = (
        f"BUSINESS FACTS:\n{facts}\n\nTOOL RESULTS:\n{json.dumps(tools, ensure_ascii=False)[:6000]}"
        f"\n\nCONVERSATION:\n{conversation[-8000:]}"
    )
    try:
        verdict = await llm.chat_json(
            [{"role": "system", "content": JUDGE_RUBRIC}, {"role": "user", "content": user}]
        )
    except EvalLLMError as exc:
        return Check("judge", None, f"judge unavailable: {exc}")
    invented: list[Any] = verdict.get("invented") or []
    claimed = bool(verdict.get("claimed_action_without_tool"))
    detail = "; ".join(str(i.get("quote", i))[:120] for i in invented if isinstance(i, dict))
    if claimed:
        detail = ("claimed an action without a tool; " + detail).strip("; ")
    return Check("judge", not invented and not claimed, detail)


def rule_checks(run: CallRun) -> list[Check]:
    return [
        check_ran(run),
        check_outcome(run),
        check_booking(run),
        check_no_new_booking(run),
        check_slots_free(run),
        check_no_double_booking(run),
        check_tools(run),
        check_mentions(run),
        check_price_numbers(run),
    ]
