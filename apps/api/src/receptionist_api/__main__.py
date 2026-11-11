"""Entry point: `make api` / `uv run receptionist-api`.

Host, port and auto-reload come from Settings, so .env is the single source of config.
"""

from __future__ import annotations

import uvicorn

from receptionist_shared import get_settings
from receptionist_shared.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    uvicorn.run(
        "receptionist_api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.environment == "dev",
        log_level=settings.log_level.lower(),
        log_config=None,  # keep our loguru/JSON setup instead of uvicorn's own
    )


if __name__ == "__main__":
    main()
