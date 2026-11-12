"""Entry point: `make agent` (= `python -m receptionist_agent`).

Starts Pipecat's development runner, which serves:
  - http://<AGENT_HOST>:<AGENT_PORT>/client  the prebuilt browser voice client
  - /start and /api/offer                    WebRTC signaling (peer-to-peer, no media server)
  - /whatsapp                                Meta's WhatsApp call webhook (whatsapp/routes.py)

The runner looks up `bot` on the `__main__` module, which is why this file re-exports it
and why the agent must be started with `python -m`, not through a console-script wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from receptionist_shared import get_settings
from receptionist_shared.logging import configure_logging
from receptionist_shared.tenant import load_tenant_by_slug

# Configure logging before anything imports Pipecat, which logs at import time.
configure_logging(get_settings().log_level, json_logs=get_settings().log_json)

from receptionist_agent.bot import bot  # noqa: E402
from receptionist_agent.providers import (  # noqa: E402
    ProviderConfigError,
    build_llm,
    describe_providers,
    stt_provider,
)

__all__ = ["bot", "main"]


def runner_argv(host: str, port: int, extra: list[str]) -> list[str]:
    """CLI args for Pipecat's runner: WebRTC only, host/port from our Settings."""
    return ["receptionist-agent", "-t", "webrtc", "--host", host, "--port", str(port), *extra]


def main() -> None:
    settings = get_settings()
    logger.info("agent starting", providers=describe_providers(settings))
    try:
        build_llm(settings, "startup check")  # fail fast on a missing API key
        tenant = load_tenant_by_slug(settings.default_tenant, Path(settings.tenants_dir))
    except (ProviderConfigError, ValueError, OSError) as exc:
        logger.error(str(exc))
        raise SystemExit(2) from exc
    logger.info("default tenant loaded", tenant=tenant.slug, languages=tenant.languages)
    if stt_provider(settings) == "groq":
        # The local Whisper that takes over if Groq fails: load it now, not mid-call (D37).
        from receptionist_agent.local_stt import preload

        preload(settings.stt_fallback_model)

    from pipecat.runner.run import app as runner_app
    from pipecat.runner.run import main as run_pipecat_runner

    from receptionist_agent.whatsapp.routes import install as install_whatsapp

    install_whatsapp(runner_app, settings)  # POST /whatsapp: Meta's call webhook (Phase 8)

    sys.argv = runner_argv(settings.agent_host, settings.agent_port, sys.argv[1:])
    logger.info(f"open http://{settings.agent_host}:{settings.agent_port}/client to talk")
    run_pipecat_runner()


if __name__ == "__main__":
    main()
