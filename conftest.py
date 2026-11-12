"""Fixtures shared by every package's tests (pytest loads this file for the whole repo)."""

import logging
import os
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

pytest_plugins = ["receptionist_api.testing"]


def _import_pipecat_runner_without_dotenv() -> None:
    """Pipecat's runner runs `load_dotenv(override=True)` when imported, which put the developer's
    whole .env (keys included) into every test's environment while test files were collected,
    before any fixture could undo it. Import it once here and put the environment back."""
    environ = dict(os.environ)
    import pipecat.runner.run  # noqa: F401  (cached: later imports don't reload .env)

    os.environ.clear()
    os.environ.update(environ)


_import_pipecat_runner_without_dotenv()

TENANTS = Path(__file__).resolve().parent / "tenants"


@pytest.fixture(autouse=True)
def _isolated_process_state() -> Iterator[None]:
    """Undo what a test leaves in the process (environment variables, the root logging
    handlers that logging setup replaces). Without this, results depended on test order."""
    environ = dict(os.environ)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    os.environ.clear()
    os.environ.update(environ)
    root.handlers[:] = handlers
    root.setLevel(level)


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
