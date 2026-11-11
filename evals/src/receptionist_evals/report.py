"""Scorecards: per-scenario results, headline metrics, and the diff against the last run."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from receptionist_evals.scorers import Check

BOOKING_CHECKS = ("outcome", "booking", "slots_free", "no_new_booking", "no_double_booking")
FACT_CHECKS = ("judge", "price_numbers", "mentions")


@dataclass
class ScenarioResult:
    id: str
    title: str
    tags: list[str]
    language: str
    checks: list[Check]
    caller_turns: int
    seconds: float
    ended_by: str
    booking_case: bool = False  # a booking must (or must not) end in a precise state
    inconclusive: str | None = None  # rate limits got in the way: left out of the metrics
    llm_targets: list[str] = field(default_factory=list)
    tokens_estimate: int = 0
    transcript: list[tuple[str, str]] = field(default_factory=list)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.passed is False]

    @property
    def passed(self) -> bool:
        return not self.failed

    def check(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def all_pass(self, names: tuple[str, ...]) -> bool:
        return all((c := self.check(n)) is None or c.passed is not False for n in names)


def _rate(results: list[ScenarioResult], ok: Any) -> float | None:
    return round(100 * sum(1 for r in results if ok(r)) / len(results), 1) if results else None


def metrics(all_results: list[ScenarioResult]) -> dict[str, Any]:
    results = [r for r in all_results if r.inconclusive is None]
    booking = [r for r in results if r.booking_case]
    handoff = [r for r in results if "handoff" in r.tags]
    safety = [r for r in results if "injection" in r.tags or "safety" in r.tags]
    judged = [r for r in results if (c := r.check("judge")) is not None and c.passed is not None]
    targets = Counter(t for r in results for t in r.llm_targets)
    return {
        "scenarios": len(results),
        "inconclusive": len(all_results) - len(results),
        "pass_rate": _rate(results, lambda r: r.passed),
        "booking_accuracy": _rate(booking, lambda r: r.all_pass(BOOKING_CHECKS)),
        "booking_cases": len(booking),
        "outcome_accuracy": _rate(results, lambda r: r.all_pass(("outcome",))),
        "no_invented_facts": _rate(results, lambda r: r.all_pass(("judge", "price_numbers"))),
        "judged": len(judged),
        "handoff_accuracy": _rate(handoff, lambda r: r.all_pass(("outcome", "tools"))),
        "safety": _rate(safety, lambda r: r.passed),
        "completed": _rate(results, lambda r: r.all_pass(("ran",))),
        "avg_caller_turns": round(sum(r.caller_turns for r in results) / len(results), 1)
        if results
        else None,
        "avg_seconds": round(sum(r.seconds for r in results) / len(results), 1)
        if results
        else None,
        "agent_tokens_estimate": sum(r.tokens_estimate for r in results),
        "llm_requests_by_target": dict(targets.most_common()),
    }


def by_tag(results: list[ScenarioResult]) -> dict[str, tuple[int, int]]:
    tags: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        for tag in r.tags:
            tags[tag].append(r.passed)
    return {t: (sum(v), len(v)) for t, v in sorted(tags.items())}


def to_json(results: list[ScenarioResult]) -> dict[str, Any]:
    return {
        "metrics": metrics(results),
        "scenarios": [
            {
                **{k: v for k, v in asdict(r).items() if k not in ("checks", "transcript")},
                "passed": r.passed,
                "checks": [asdict(c) for c in r.checks],
                "transcript": [{"speaker": s, "text": t} for s, t in r.transcript],
            }
            for r in results
        ],
    }


HEADLINE = [
    ("pass_rate", "All checks pass"),
    ("booking_accuracy", "Booking accuracy"),
    ("outcome_accuracy", "Right outcome"),
    ("no_invented_facts", "No invented facts"),
    ("handoff_accuracy", "Handoff when expected"),
    ("safety", "Safety (injection, refusals)"),
    ("completed", "Finished within the turn limit"),
]


def _delta(now: Any, before: Any) -> str:
    if not isinstance(now, int | float) or not isinstance(before, int | float):
        return ""
    d = now - before
    return " (=)" if abs(d) < 0.05 else f" ({d:+.1f})"


def to_markdown(
    results: list[ScenarioResult], *, previous: dict[str, Any] | None, title: str
) -> str:
    m = metrics(results)
    prev = (previous or {}).get("metrics", {})
    lines = [
        f"# Eval scorecard: {title}",
        "",
        "| metric | value | vs previous run |",
        "|---|---:|---:|",
    ]
    for key, label in HEADLINE:
        value = m[key]
        shown = "-" if value is None else f"{value:.1f}%"
        lines.append(f"| {label} | {shown} | {_delta(value, prev.get(key)).strip(' ()') or '-'} |")
    lines += [
        "",
        f"{m['scenarios']} scenarios ({m['booking_cases']} with a booking to check, "
        f"{m['judged']} judged). Average {m['avg_caller_turns']} caller turns, "
        f"{m['avg_seconds']} s per scenario. Agent prompt tokens (estimate): "
        f"{m['agent_tokens_estimate']:,}; on the free tiers this costs $0.",
        "",
        "LLM requests by target: "
        + (", ".join(f"{k} {v}" for k, v in m["llm_requests_by_target"].items()) or "-"),
        "",
        "## By tag",
        "",
        "| tag | passed |",
        "|---|---:|",
    ]
    lines += [f"| {tag} | {ok}/{n} |" for tag, (ok, n) in by_tag(results).items()]
    unsure = [r for r in results if r.inconclusive]
    if unsure:
        lines += ["", f"## Inconclusive: free-tier limits ({len(unsure)}, not counted above)", ""]
        lines += [f"- `{r.id}`: {r.inconclusive}" for r in unsure]
    failed = [r for r in results if not r.passed and not r.inconclusive]
    lines += ["", f"## Failures ({len(failed)})", ""]
    if not failed:
        lines.append("None.")
    for r in failed:
        reasons = "; ".join(f"**{c.name}**: {c.detail or 'failed'}" for c in r.failed)
        lines.append(f"- `{r.id}` ({r.title}): {reasons}")
    prev_by_id = {s["id"]: s["passed"] for s in (previous or {}).get("scenarios", [])}
    flipped = [
        (r.id, prev_by_id[r.id], r.passed)
        for r in results
        if r.id in prev_by_id and prev_by_id[r.id] != r.passed
    ]
    if flipped:
        lines += ["", "## Changed since the previous run", ""]
        lines += [
            f"- `{i}`: {'pass' if a else 'fail'} -> {'pass' if b else 'fail'}"
            for i, a, b in flipped
        ]
    return "\n".join(lines) + "\n"


def latest_run(results_dir: Path, *, exclude: Path | None = None) -> dict[str, Any] | None:
    runs = sorted(p for p in results_dir.glob("*/scorecard.json") if p.parent != exclude)
    return json.loads(runs[-1].read_text()) if runs else None


def write_run(results: list[ScenarioResult], run_dir: Path, *, title: str) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    previous = latest_run(run_dir.parent, exclude=run_dir)
    data = to_json(results)
    (run_dir / "scorecard.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str)
    )
    (run_dir / "scorecard.md").write_text(to_markdown(results, previous=previous, title=title))
    return data
