"""Entry point: `make evals`. Runs simulated-caller scenarios against the real agent brain.

    make evals                                  # all scenarios
    make evals ARGS="--tags ci"                 # the CI subset
    make evals ARGS="--ids salon-book-haircut-en --no-judge"
    make evals ARGS="--tags ci --baseline evals/baseline.json"   # exit 1 if booking accuracy
                                                                 # drops more than 2 points

Writes evals/results/<timestamp>/scorecard.{md,json} and prints the markdown.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from receptionist_agent.brain.prompt import business_facts
from receptionist_evals.llm import CALLER_MODELS, JUDGE_MODELS, EvalLLM, EvalLLMError, eval_targets
from receptionist_evals.report import ScenarioResult, metrics, write_run
from receptionist_evals.runner import run_scenario, run_scenario_audio
from receptionist_evals.scenario import Scenario, load_scenarios
from receptionist_evals.scorers import judge, rule_checks
from receptionist_shared import Settings, get_settings
from receptionist_shared.logging import configure_logging
from receptionist_shared.tenant import TenantConfig

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS_DIR = ROOT / "evals" / "scenarios"
RESULTS_DIR = ROOT / "evals" / "results"


def find_scenarios(directory: Path = SCENARIOS_DIR) -> list[Path]:
    """All scenario YAML files, sorted, so runs are reproducible."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.yaml"))


def judge_facts(tenant: TenantConfig) -> str:
    faq = "\n".join(f"- Q: {f.question} A: {f.answer}" for f in tenant.faq)
    return f"{business_facts(tenant)}\nFAQ:\n{faq}"


def is_booking_case(s: Scenario) -> bool:
    wants_state = {"booked", "rescheduled", "cancelled"} & set(s.expect.outcomes)
    return bool(wants_state or s.expect.booking or s.expect.no_booking_at)


async def evaluate(
    scenario: Scenario,
    settings: Settings,
    llm: EvalLLM,
    *,
    use_judge: bool,
    synth: Any = None,
    judge_llm: EvalLLM | None = None,
) -> ScenarioResult:
    tenants_dir = ROOT / settings.tenants_dir
    if synth is not None:
        run = await run_scenario_audio(
            scenario, settings, llm, tenants_dir=tenants_dir, synth=synth
        )
    else:
        run = await run_scenario(scenario, settings, llm, tenants_dir=tenants_dir)
    checks = rule_checks(run)
    if use_judge and run.error is None and run.inconclusive is None:
        checks.append(await judge(run, judge_llm or llm, judge_facts(run.tenant)))
    result = ScenarioResult(
        id=scenario.id,
        title=scenario.title,
        tags=list(scenario.tags),
        language=scenario.language,
        checks=checks,
        caller_turns=run.caller_turns,
        seconds=run.seconds,
        ended_by=run.ended_by,
        booking_case=is_booking_case(scenario),
        inconclusive=run.inconclusive,
        llm_targets=run.llm_targets,
        tokens_estimate=run.agent_tokens_estimate,
        transcript=run.transcript,
    )
    status = "pass" if result.passed else "FAIL " + ", ".join(c.name for c in result.failed)
    if result.inconclusive:
        status = f"inconclusive ({result.inconclusive})"
    logger.info("scenario", id=scenario.id, result=status, turns=run.caller_turns, secs=run.seconds)
    return result


async def run_suite(
    scenarios: list[Scenario],
    settings: Settings,
    *,
    parallel: int,
    use_judge: bool,
    retries: int,
    audio: bool = False,
) -> list[ScenarioResult]:
    llm = EvalLLM(eval_targets(settings, CALLER_MODELS))
    judge_llm = EvalLLM(eval_targets(settings, JUDGE_MODELS))
    gate = asyncio.Semaphore(1 if audio else parallel)  # audio runs in real time, one at a time
    synth = None
    if audio:
        from receptionist_agent.bench.synth import Synth

        synth = Synth()

    async def one(s: Scenario) -> ScenarioResult:
        async with gate:
            kw: dict[str, Any] = {"use_judge": use_judge, "synth": synth, "judge_llm": judge_llm}
            result = await evaluate(s, settings, llm, **kw)
            for _ in range(retries):
                if result.passed and not result.inconclusive:
                    break
                logger.info("retrying scenario", id=s.id)
                result = await evaluate(s, settings, llm, **kw)
                result.tags = [*result.tags, "retried"]
            return result

    try:
        return list(await asyncio.gather(*(one(s) for s in scenarios)))
    finally:
        await llm.aclose()
        await judge_llm.aclose()


def gate_on_baseline(current: Mapping[str, Any], baseline_path: Path, max_drop: float) -> bool:
    """True if booking accuracy is within `max_drop` points of the baseline."""
    baseline = json.loads(baseline_path.read_text())
    unsure, judged = current.get("inconclusive") or 0, current.get("scenarios") or 0
    if judged == 0 or unsure > judged:  # more than half couldn't run: nothing fair to compare
        print(f"::warning::evals inconclusive: {unsure} scenario(s) hit free-tier limits")
        return True
    before, now = baseline.get("booking_accuracy"), current.get("booking_accuracy")
    if before is None or now is None:
        print("baseline gate: no booking accuracy to compare")
        return True
    ok = bool(now >= before - max_drop)
    print(
        f"baseline gate: booking accuracy {now:.1f}% vs baseline {before:.1f}% "
        f"(allowed drop {max_drop} points): {'ok' if ok else 'FAILED'}"
    )
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Simulated-caller eval suite")
    parser.add_argument("--tags", nargs="*", help="only scenarios with any of these tags")
    parser.add_argument("--ids", nargs="*", help="only these scenario ids")
    parser.add_argument(
        "--parallel", type=int, default=1, help="scenarios at once (free tier: 1-2)"
    )
    parser.add_argument(
        "--retries", type=int, default=0, help="re-run a failed scenario this often"
    )
    parser.add_argument(
        "--no-judge", action="store_true", help="skip the LLM judge (rule checks only)"
    )
    parser.add_argument("--baseline", type=Path, help="fail if booking accuracy drops vs this file")
    parser.add_argument("--max-drop", type=float, default=2.0)
    parser.add_argument("--update-baseline", type=Path, help="write this run's metrics here")
    parser.add_argument("--list", action="store_true", help="list matching scenarios and exit")
    parser.add_argument(
        "--audio",
        action="store_true",
        help="speech mode (TTS -> full voice pipeline); defaults to the scenarios tagged audio",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    configure_logging("WARNING" if args.list else "INFO", json_logs=False)
    tags = set(args.tags) if args.tags else ({"audio"} if args.audio else None)
    scenarios = load_scenarios(SCENARIOS_DIR, tags=tags)
    if args.ids:
        wanted = set(args.ids)
        scenarios = [s for s in scenarios if s.id in wanted]
    print(f"found {len(scenarios)} scenario(s) in {SCENARIOS_DIR}")
    if args.list or not scenarios:
        for s in scenarios:
            print(f"  {s.id}  [{', '.join(s.tags)}]")
        return 0
    try:
        results = asyncio.run(
            run_suite(
                scenarios,
                settings,
                parallel=args.parallel,
                use_judge=not args.no_judge,
                retries=args.retries,
                audio=args.audio,
            )
        )
    except EvalLLMError as exc:
        print(f"cannot run evals: {exc}", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mode = "audio" if args.audio else "text"
    label = f"{stamp} ({mode}; {', '.join(sorted(tags)) if tags else 'all'})"
    data = write_run(results, RESULTS_DIR / stamp, title=label)
    print((RESULTS_DIR / stamp / "scorecard.md").read_text())
    if args.update_baseline:
        args.update_baseline.write_text(json.dumps(metrics(results), indent=2) + "\n")
        print(f"baseline written to {args.update_baseline}")
    if args.baseline and not gate_on_baseline(data["metrics"], args.baseline, args.max_drop):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
