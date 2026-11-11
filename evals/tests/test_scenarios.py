"""Every scenario file is valid and refers to things that exist."""

from pathlib import Path

import pytest

from receptionist_evals.__main__ import SCENARIOS_DIR
from receptionist_evals.scenario import Scenario, load_scenarios
from receptionist_shared.tenant import load_tenant_by_slug

TENANTS = Path(__file__).resolve().parents[2] / "tenants"
SCENARIOS = load_scenarios(SCENARIOS_DIR)


def test_there_are_at_least_60_scenarios_covering_the_guide() -> None:
    assert len(SCENARIOS) >= 60
    tags = {t for s in SCENARIOS for t in s.tags}
    needed = {
        "booking", "reschedule", "cancel", "price", "unknown", "changes", "correction",
        "interruptions", "short-answers", "urdu", "mixed", "angry", "handoff", "slot-full",
        "hours", "injection",
    }  # fmt: skip
    assert needed <= tags, needed - tags


def test_ci_subset_is_small_and_mostly_bookings() -> None:
    ci = [s for s in SCENARIOS if "ci" in s.tags]
    assert 10 <= len(ci) <= 25
    assert sum("booking" in s.tags or "reschedule" in s.tags or "cancel" in s.tags for s in ci) >= 8


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario_refers_to_real_tenant_and_services(scenario: Scenario) -> None:
    tenant = load_tenant_by_slug(scenario.tenant, TENANTS)
    for seed in scenario.seed_bookings:
        assert tenant.find_service(seed.service), seed.service
    if scenario.expect.booking:
        assert tenant.find_service(scenario.expect.booking.service)
        if scenario.expect.booking.phone:
            assert scenario.expect.booking.phone.startswith("+92")


def test_load_by_tag() -> None:
    urdu = load_scenarios(SCENARIOS_DIR, tags={"urdu"})
    assert urdu and all("urdu" in s.tags for s in urdu)
