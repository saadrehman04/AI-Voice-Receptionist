"""Print P50/P95 per pipeline stage from logs/turns.jsonl.

make latency                     # whole file
make latency ARGS="--last 50"    # only the 50 most recent turns
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STAGES = ("stt", "end_of_turn", "llm", "tts", "playout", "total")


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100). Simple and exact for small samples."""
    if not values:
        raise ValueError("percentile of an empty list")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass(frozen=True)
class StageStats:
    stage: str
    count: int
    p50: float | None
    p95: float | None
    mean: float | None


def load_turns(path: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    if not path.exists():
        return turns
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            turns.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a half-written last line must not break the report
    return turns


def stage_stats(turns: Iterable[dict[str, Any]]) -> list[StageStats]:
    by_stage: dict[str, list[float]] = {s: [] for s in STAGES}
    for turn in turns:
        stages = turn.get("stages") or {}
        for stage in STAGES:
            value = stages.get(stage)
            if isinstance(value, int | float):
                by_stage[stage].append(float(value))
    out = []
    for stage, values in by_stage.items():
        if values:
            out.append(
                StageStats(
                    stage,
                    len(values),
                    percentile(values, 50),
                    percentile(values, 95),
                    round(sum(values) / len(values), 1),
                )
            )
        else:
            out.append(StageStats(stage, 0, None, None, None))
    return out


def format_table(stats: Sequence[StageStats], n_turns: int) -> str:
    def cell(v: float | None) -> str:
        return "-" if v is None else f"{v:.0f}"

    lines = [
        f"turns: {n_turns}",
        f"{'stage':<12}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'mean ms':>10}",
    ]
    for s in stats:
        lines.append(
            f"{s.stage:<12}{s.count:>6}{cell(s.p50):>10}{cell(s.p95):>10}{cell(s.mean):>10}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("path", nargs="?", default="logs/turns.jsonl", type=Path)
    parser.add_argument("--last", type=int, default=None, help="only the N most recent turns")
    args = parser.parse_args(argv)

    turns = load_turns(args.path)
    if args.last:
        turns = turns[-args.last :]
    if not turns:
        print(f"no turns in {args.path} yet - make a call first (make agent)")
        return 1
    print(format_table(stage_stats(turns), len(turns)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
