"""FastAPI app. Run with `make api` (uvicorn with auto-reload).

Phase 0: only a health endpoint. Tenants, bookings and call records come in Phases 3-4.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from receptionist_shared import get_settings


class Health(BaseModel):
    status: Literal["ok"]
    environment: str
    version: str


def create_app() -> FastAPI:
    """Build the app. A factory (instead of a module-level app only) keeps tests isolated."""
    app = FastAPI(title="Voice Receptionist API", version="0.1.0")

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        """Liveness check: the process is up. (DB/Redis readiness comes in Phase 4.)"""
        return Health(status="ok", environment=get_settings().environment, version=app.version)

    return app


app = create_app()
