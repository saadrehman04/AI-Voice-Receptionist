"""Entry point: `make agent` / `uv run receptionist-agent`.

Phase 0 placeholder: it loads settings and reports which providers are selected,
which proves the agent can import the shared package and read .env.
"""

from __future__ import annotations

import logging

from receptionist_shared import Settings, get_settings

logger = logging.getLogger("receptionist_agent")


def describe_providers(settings: Settings) -> str:
    """One line describing the configured STT/LLM/TTS providers (never secrets)."""
    return f"stt={settings.stt_provider} llm={settings.llm_provider} tts={settings.tts_provider}"


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    logger.info("agent placeholder started: %s", describe_providers(settings))
    logger.info("voice pipeline not built yet (Phase 1)")


if __name__ == "__main__":
    main()
