from typing import Any

import pytest
import uvicorn

from receptionist_api import __main__ as api_main
from receptionist_shared import Settings


def test_main_runs_uvicorn_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    settings = Settings(api_host="0.0.0.0", api_port=9100, environment="prod")  # noqa: S104
    monkeypatch.setattr(api_main, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append((a, kw)))

    api_main.main()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ("receptionist_api.main:app",)
    assert kwargs["host"] == "0.0.0.0"  # noqa: S104
    assert kwargs["port"] == 9100
    assert kwargs["reload"] is False  # never auto-reload outside dev
