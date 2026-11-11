from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from receptionist_shared.availability import (
    Interval,
    free_slots,
    pick_options,
    spoken_slot,
)
from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

TENANTS = Path(__file__).resolve().parents[3] / "tenants"
WED = date(2026, 11, 18)  # salon open 11:00-20:00


@pytest.fixture(scope="module")
def salon() -> TenantConfig:
    return load_tenant_by_slug("demo-salon", TENANTS)


def at(tenant: TenantConfig, day: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=tenant.tz)


def test_slots_fit_inside_opening_hours(salon: TenantConfig) -> None:
    haircut = salon.find_service("Haircut")
    assert haircut is not None
    now = at(salon, WED - timedelta(days=1), "09:00")
    slots = free_slots(salon, haircut, WED, [], now=now)
    assert slots[0] == at(salon, WED, "11:00")
    assert slots[-1] == at(salon, WED, "19:15")  # 45 min must end by 20:00
    assert all(s.minute % 15 == 0 for s in slots)


def test_closed_day_has_no_slots(salon: TenantConfig) -> None:
    haircut = salon.find_service("Haircut")
    assert haircut is not None
    tue = date(2026, 11, 17)
    assert free_slots(salon, haircut, tue, [], now=at(salon, tue, "08:00")) == []


def test_busy_interval_and_buffer_block_slots(salon: TenantConfig) -> None:
    haircut = salon.find_service("Haircut")
    assert haircut is not None
    busy = [Interval(at(salon, WED, "13:00"), at(salon, WED, "14:00"))]
    slots = free_slots(salon, haircut, WED, busy, now=at(salon, WED - timedelta(days=1), "09:00"))
    # 15 min buffer: nothing may overlap 12:45-14:15
    assert at(salon, WED, "12:00") in slots  # 12:00-12:45 ends exactly at buffer start
    assert at(salon, WED, "12:15") not in slots
    assert at(salon, WED, "14:00") not in slots
    assert at(salon, WED, "14:15") in slots


def test_min_notice_and_past(salon: TenantConfig) -> None:
    haircut = salon.find_service("Haircut")
    assert haircut is not None
    slots = free_slots(salon, haircut, WED, [], now=at(salon, WED, "15:05"))
    assert slots[0] == at(salon, WED, "16:15")  # 60 min notice, next 15 min step
    assert (
        free_slots(salon, haircut, WED - timedelta(days=1), [], now=at(salon, WED, "09:00")) == []
    )


def test_too_far_ahead(salon: TenantConfig) -> None:
    haircut = salon.find_service("Haircut")
    assert haircut is not None
    now = at(salon, WED, "09:00")
    far = WED + timedelta(days=salon.max_days_ahead + 7)
    assert free_slots(salon, haircut, far, [], now=now) == []


def test_split_friday_hours_respected(salon: TenantConfig) -> None:
    facial = salon.find_service("Facial")
    assert facial is not None
    fri = date(2026, 11, 20)
    slots = free_slots(salon, facial, fri, [], now=at(salon, WED, "09:00"))
    assert at(salon, fri, "12:00") in slots  # 12:00-13:00 fits the morning range
    assert at(salon, fri, "12:15") not in slots  # would run into the prayer break
    assert at(salon, fri, "15:00") in slots


def test_pick_options_prefers_requested_time(salon: TenantConfig) -> None:
    slots = [at(salon, WED, f"{h:02d}:00") for h in range(11, 20)]
    picked = pick_options(slots, preferred=time(15, 10))
    assert picked == [at(salon, WED, "14:00"), at(salon, WED, "15:00"), at(salon, WED, "16:00")]


def test_pick_options_spreads_over_day(salon: TenantConfig) -> None:
    slots = [at(salon, WED, f"{h:02d}:00") for h in range(11, 20)]
    picked = pick_options(slots)
    assert picked == [at(salon, WED, "11:00"), at(salon, WED, "15:00"), at(salon, WED, "19:00")]
    assert pick_options([]) == []
    assert pick_options(slots[:2]) == slots[:2]


def test_spoken_slot(salon: TenantConfig) -> None:
    assert spoken_slot(at(salon, WED, "14:30")) == "Wednesday 18 November, 2:30 PM"
