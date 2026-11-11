import json
from pathlib import Path

import pytest

from receptionist_agent.latency_report import (
    format_table,
    load_turns,
    main,
    percentile,
    stage_stats,
)


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]  # 1..100
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


def write_turns(path: Path, totals: list[float | None]) -> None:
    with path.open("w") as fh:
        for i, total in enumerate(totals):
            record = {"turn_id": str(i), "stages": {"stt": 100.0, "total": total}}
            fh.write(json.dumps(record) + "\n")


def test_stage_stats_skip_missing_values(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    write_turns(path, [800.0, None, 1200.0, 1000.0])
    stats = {s.stage: s for s in stage_stats(load_turns(path))}
    assert stats["total"].count == 3
    assert stats["total"].p50 == 1000.0
    assert stats["total"].p95 == 1200.0
    assert stats["stt"].count == 4
    assert stats["llm"].count == 0
    assert stats["llm"].p50 is None


def test_load_turns_tolerates_partial_last_line(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    write_turns(path, [900.0])
    with path.open("a") as fh:
        fh.write('{"turn_id": "half')
    assert len(load_turns(path)) == 1
    assert load_turns(tmp_path / "missing.jsonl") == []


def test_table_lists_every_stage(tmp_path: Path) -> None:
    path = tmp_path / "turns.jsonl"
    write_turns(path, [900.0])
    table = format_table(stage_stats(load_turns(path)), 1)
    for stage in ("stt", "end_of_turn", "llm", "tts", "playout", "total"):
        assert stage in table


def test_main_prints_report_and_handles_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "turns.jsonl"
    assert main([str(path)]) == 1
    write_turns(path, [500.0, 700.0, 900.0])
    assert main([str(path), "--last", "2"]) == 0
    out = capsys.readouterr().out
    assert "turns: 2" in out
