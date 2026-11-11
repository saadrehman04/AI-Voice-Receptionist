"""App-wide settings, loaded from environment variables and the .env file.

Every service (agent, api, evals) reads its config through `get_settings()`, so there is
exactly one place that knows the variable names, their types and their defaults.

Secrets are typed as `SecretStr`: printing or logging a Settings object shows `**********`
instead of the key. Call `.get_secret_value()` only at the point where the key is sent.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "test", "prod"]
SttProvider = Literal["groq", "faster_whisper"]
LlmProvider = Literal["groq", "gemini", "ollama"]
TtsProvider = Literal["kokoro", "piper"]


class Settings(BaseSettings):
    """Typed view of the environment. Field names map to UPPER_CASE env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env also holds vars for docker-compose (POSTGRES_*)
        env_ignore_empty=True,  # `GROQ_API_KEY=` means "not set", not an empty key
    )

    # --- general ---
    environment: Environment = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- provider selection (each sits behind an interface, swapped by config) ---
    stt_provider: SttProvider = "groq"
    llm_provider: LlmProvider = "groq"
    tts_provider: TtsProvider = "kokoro"

    # --- provider credentials and models ---
    groq_api_key: SecretStr | None = None
    groq_llm_model: str | None = None
    groq_stt_model: str | None = None
    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str | None = None
    faster_whisper_model: str = "small"
    kokoro_voice: str | None = None
    piper_voice: str | None = None

    # --- local services ---
    database_url: str = "postgresql+asyncpg://receptionist:receptionist@localhost:5432/receptionist"
    redis_url: str = "redis://localhost:6379/0"

    # --- our own services ---
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_base_url: str = "http://127.0.0.1:8000"
    agent_host: str = "127.0.0.1"
    agent_port: int = Field(default=7860, ge=1, le=65535)

    # --- auth (phase 4) ---
    jwt_secret: SecretStr | None = None

    # --- call recording (phase 4) ---
    record_calls: bool = False

    # --- Google Calendar (phase 3) ---
    google_service_account_file: str | None = None

    # --- WhatsApp Business Platform (phase 8) ---
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_verify_token: SecretStr | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings, built once on first use."""
    return Settings()
