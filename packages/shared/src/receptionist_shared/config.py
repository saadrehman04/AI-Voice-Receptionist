"""App-wide settings, loaded from environment variables and the .env file.

Every service (agent, api, evals) reads its config through `get_settings()`, so there is
exactly one place that knows the variable names, their types and their defaults.

Secrets are typed as `SecretStr`: printing or logging a Settings object shows `**********`
instead of the key. Call `.get_secret_value()` only at the point where the key is sent.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # Defaults were checked against each provider's live model list (with a key) on 2026-11-11.
    groq_api_key: SecretStr | None = None
    groq_llm_model: str = "openai/gpt-oss-120b"
    # For reasoning models (gpt-oss, qwen): "low" keeps the first token fast (DECISIONS D23).
    # Unset for non-reasoning models, which reject the parameter.
    groq_reasoning_effort: Literal["none", "default", "low", "medium", "high"] | None = "low"
    groq_stt_model: str = "whisper-large-v3-turbo"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    # When LLM_PROVIDER=groq: models tried in order when the main one is rate-limited or down,
    # as "provider:model" (provider groq or gemini). Empty = no fallback. See DECISIONS D23.
    llm_fallbacks: str = "groq:openai/gpt-oss-20b,gemini:gemini-3.5-flash-lite"
    # Short yes/no/thanks turns go to this target first ("" = off). See DECISIONS D26.
    llm_simple_turn_model: str = "groq:openai/gpt-oss-20b"

    # --- latency (Phase 5; each is a flag so `make bench` can compare before/after) ---
    tts_text_chunking: Literal["full", "sentence", "clause"] = "clause"
    filler_after_ms: int = Field(default=400, ge=0)  # 0 = no "one moment" fillers
    prewarm_connections: bool = True
    vad_stop_secs: float = Field(default=0.2, gt=0, le=2)
    availability_cache_secs: int = Field(default=30, ge=0)  # api side; 0 = no cache
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.2:3b"
    faster_whisper_model: str = "small"
    kokoro_voice: str = "af_heart"
    piper_voice: str = "en_US-lessac-medium"
    # Urdu replies are spoken by Piper (Kokoro has no Urdu); see DECISIONS D10.
    piper_urdu_voice: str = "ur_PK-fasih-medium"
    # ISO code like "en" or "ur"; unset = let Whisper auto-detect per utterance.
    stt_language: str | None = None

    # --- tenants ---
    tenants_dir: str = "tenants"
    default_tenant: str = "demo-salon"

    # --- observability ---
    log_json: bool = True
    turns_log_path: str = "logs/turns.jsonl"

    # --- local services ---
    database_url: str = "postgresql+asyncpg://receptionist:receptionist@localhost:5432/receptionist"
    redis_url: str = "redis://localhost:6379/0"

    # --- our own services ---
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=1, le=65535)
    api_base_url: str = "http://127.0.0.1:8000"
    agent_host: str = "127.0.0.1"
    agent_port: int = Field(default=7860, ge=1, le=65535)

    # --- auth ---
    jwt_secret: SecretStr | None = None  # owner sessions (Phase 4)
    # Shared secret the agent sends to the api (X-Agent-Token). Required when the api runs.
    agent_api_token: SecretStr | None = None

    # --- tools backend (Phase 3) ---
    # "memory": in-process stub (tests, make chat). "api": real bookings via apps/api.
    booking_backend: Literal["memory", "api"] = "memory"
    # "fake": in-memory calendar (dev/tests). "google": Google Calendar via service account.
    calendar_provider: Literal["fake", "google"] = "fake"
    hold_secs: int = Field(default=120, ge=10, le=900)

    # --- knowledge base (Phase 3) ---
    kb_dir: str = "tenants/kb"
    embeddings_enabled: bool = True
    embeddings_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # --- call recording (phase 4) ---
    record_calls: bool = False  # callers hear a recording + AI disclosure when on
    storage_dir: str = "storage"  # recordings: <storage_dir>/<tenant>/<call_id>.wav

    # --- Google Calendar (phase 3) ---
    google_service_account_file: str | None = None

    # --- WhatsApp Business Platform (phase 8) ---
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_verify_token: SecretStr | None = None

    @field_validator("jwt_secret", "agent_api_token")
    @classmethod
    def _long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("must be at least 32 characters (openssl rand -hex 32)")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide Settings, built once on first use."""
    return Settings()
