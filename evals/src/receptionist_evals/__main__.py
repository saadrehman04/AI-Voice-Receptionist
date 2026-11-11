"""Entry point: `make evals`. Phase 0 placeholder so the command already exists."""

from __future__ import annotations

from pathlib import Path

SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "scenarios"


def find_scenarios(directory: Path = SCENARIOS_DIR) -> list[Path]:
    """All scenario YAML files, sorted, so runs are reproducible."""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.yaml"))


def main() -> None:
    scenarios = find_scenarios()
    print(f"found {len(scenarios)} scenario(s) in {SCENARIOS_DIR}")
    print("eval runner not built yet (Phase 6)")


if __name__ == "__main__":
    main()
