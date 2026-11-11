from datetime import datetime
from pathlib import Path

import pytest

from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

TENANTS = Path(__file__).resolve().parents[3] / "tenants"


@pytest.fixture
def salon() -> TenantConfig:
    return load_tenant_by_slug("demo-salon", TENANTS)


@pytest.fixture
def workshop() -> TenantConfig:
    return load_tenant_by_slug("demo-workshop", TENANTS)


@pytest.fixture
def monday_9am(salon: TenantConfig) -> datetime:
    """Monday 16 Nov 2026, 09:00 Karachi time. Salon opens 11:00, closed Tuesday."""
    return datetime(2026, 11, 16, 9, 0, tzinfo=salon.tz)
