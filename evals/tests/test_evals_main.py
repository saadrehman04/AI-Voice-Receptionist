from pathlib import Path

from receptionist_evals.__main__ import find_scenarios


def test_find_scenarios_sorted(tmp_path: Path) -> None:
    (tmp_path / "b.yaml").write_text("")
    (tmp_path / "a.yaml").write_text("")
    (tmp_path / "notes.md").write_text("")
    assert [p.name for p in find_scenarios(tmp_path)] == ["a.yaml", "b.yaml"]


def test_find_scenarios_missing_dir(tmp_path: Path) -> None:
    assert find_scenarios(tmp_path / "missing") == []
