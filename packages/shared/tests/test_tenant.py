from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from receptionist_shared.tenant import (
    TenantConfig,
    Weekday,
    list_tenants,
    load_tenant,
    load_tenant_by_slug,
)

TENANTS = Path(__file__).resolve().parents[3] / "tenants"


def base() -> dict[str, Any]:
    data = yaml.safe_load((TENANTS / "demo-salon.yaml").read_text(encoding="utf-8"))
    return cast(dict[str, Any], data)


def test_example_tenants_load() -> None:
    tenants = {t.slug: t for t in list_tenants(TENANTS)}
    assert set(tenants) == {"demo-salon", "demo-workshop"}
    salon = tenants["demo-salon"]
    assert salon.business_name == "Noor Beauty Salon"
    assert salon.languages == ["en", "ur"]
    assert salon.find_service("haircut") is not None
    assert salon.find_service("Bridal makeup").price is None  # type: ignore[union-attr]
    assert salon.hours_on(date(2026, 11, 17)) == []  # a Tuesday: closed


def test_hours_parse_split_days() -> None:
    salon = load_tenant_by_slug("demo-salon", TENANTS)
    friday = salon.opening_hours[Weekday.FRIDAY]
    assert [r.label() for r in friday] == ["11:00-13:00", "15:00-20:00"]


def test_find_service_by_urdu_name_and_partial() -> None:
    salon = load_tenant_by_slug("demo-salon", TENANTS)
    assert salon.find_service("ہیئر کٹ").name == "Haircut"  # type: ignore[union-attr]
    assert salon.find_service("a facial please").name == "Facial"  # type: ignore[union-attr]
    assert salon.find_service("hair") is None  # ambiguous: haircut and hair colour
    assert salon.find_service("massage") is None


def test_prices_are_decimal() -> None:
    salon = load_tenant_by_slug("demo-salon", TENANTS)
    assert salon.find_service("Haircut").price == Decimal(2500)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"slug": "Bad Slug"}, "slug"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"default_language": "ur", "languages": ["en"]}, "default_language"),
        ({"opening_hours": {"monday": ["18:00-09:00"]}}, "start before"),
        ({"opening_hours": {"monday": ["9-5"]}}, "09:00-17:00"),
        ({"opening_hours": {"monday": ["09:00-13:00", "12:00-15:00"]}}, "overlap"),
        ({"greeting": {"en": "Hi"}}, "greeting.ur"),
        ({"unexpected": True}, "unexpected"),
    ],
)
def test_invalid_configs_rejected(change: dict[str, Any], message: str) -> None:
    data = base() | change
    with pytest.raises(ValidationError, match=message):
        TenantConfig.model_validate(data)


def test_duplicate_service_names_rejected() -> None:
    data = base()
    data["services"].append(dict(data["services"][0]))
    with pytest.raises(ValidationError, match="unique"):
        TenantConfig.model_validate(data)


def test_slug_must_match_file_name(tmp_path: Path) -> None:
    path = tmp_path / "other-name.yaml"
    path.write_text(yaml.safe_dump(base(), allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="file name"):
        load_tenant(path)


def test_slug_lookup_rejects_path_tricks() -> None:
    with pytest.raises(ValueError):
        load_tenant_by_slug("../secrets", TENANTS)


def test_config_is_immutable() -> None:
    salon = load_tenant_by_slug("demo-salon", TENANTS)
    with pytest.raises(ValidationError):
        salon.business_name = "Changed"  # type: ignore[misc]
