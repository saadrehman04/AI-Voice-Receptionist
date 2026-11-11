from pathlib import Path

import pytest

from receptionist_shared.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test in an empty dir so a developer's real .env never leaks in."""
    monkeypatch.chdir(tmp_path)
    for name in ("GROQ_API_KEY", "LLM_PROVIDER", "API_PORT", "RECORD_CALLS"):
        monkeypatch.delenv(name, raising=False)


def test_defaults_work_without_any_env() -> None:
    s = Settings()
    assert s.environment == "dev"
    assert s.llm_provider == "groq"
    assert s.groq_api_key is None
    assert s.api_port == 8000


def test_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("API_PORT", "9001")
    monkeypatch.setenv("RECORD_CALLS", "true")
    s = Settings()
    assert s.llm_provider == "gemini"
    assert s.api_port == 9001
    assert s.record_calls is True


def test_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "some-paid-llm")
    with pytest.raises(ValueError, match="llm_provider"):
        Settings()


def test_secrets_are_masked_when_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "gsk_super_secret")
    s = Settings()
    assert "gsk_super_secret" not in repr(s)
    assert "gsk_super_secret" not in str(s.model_dump())
    assert s.groq_api_key is not None
    assert s.groq_api_key.get_secret_value() == "gsk_super_secret"


def test_env_example_parses(tmp_path: Path) -> None:
    """The checked-in .env.example must load cleanly (inline comments included)."""
    env_file = tmp_path / ".env"
    env_file.write_text((REPO_ROOT / ".env.example").read_text())
    s = Settings(_env_file=env_file)
    assert s.stt_provider == "groq"
    assert s.llm_provider == "groq"
    assert s.tts_provider == "kokoro"
    # empty values in the example mean "unset", so they fall back to None
    assert s.groq_api_key is None
    assert s.groq_llm_model == "llama-3.3-70b-versatile"
    assert s.stt_language is None
    assert s.record_calls is False
