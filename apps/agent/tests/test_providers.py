from typing import Any

import pytest

from receptionist_agent import providers
from receptionist_agent.providers import (
    ProviderConfigError,
    build_llm,
    build_stt,
    build_tts,
    describe_providers,
    stt_language,
)
from receptionist_shared import Settings

PROMPT = "You are a test receptionist."


def make(**kwargs: Any) -> Settings:
    return Settings(_env_file=None, **kwargs)


class Recorder:
    """Stands in for a heavy local service class and records how it was built."""

    calls: list[dict[str, Any]]

    def __init__(self, **kwargs: Any) -> None:
        type(self).calls.append(kwargs)

    class Settings:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs


@pytest.fixture
def recorder() -> type[Recorder]:
    class R(Recorder):
        calls: list[dict[str, Any]] = []  # noqa: RUF012

    return R


def test_groq_llm_uses_model_and_system_prompt() -> None:
    llm = build_llm(make(groq_api_key="gsk_test"), PROMPT)
    assert type(llm).__name__ == "GroqLLMService"
    assert llm._settings.model == "llama-3.3-70b-versatile"
    assert llm._settings.system_instruction == PROMPT


def test_gemini_llm_selected_by_config() -> None:
    s = make(llm_provider="gemini", gemini_api_key="g_test")
    llm = build_llm(s, PROMPT)
    assert type(llm).__name__ == "GoogleLLMService"
    assert llm._settings.model == "gemini-3.5-flash-lite"


def test_ollama_llm_needs_no_key() -> None:
    llm = build_llm(make(llm_provider="ollama"), PROMPT)
    assert type(llm).__name__ == "OLLamaLLMService"
    assert llm._settings.model == "llama3.2:3b"


@pytest.mark.parametrize(
    ("overrides", "env_name"),
    [
        ({"llm_provider": "groq"}, "GROQ_API_KEY"),
        ({"llm_provider": "gemini"}, "GEMINI_API_KEY"),
    ],
)
def test_missing_key_fails_with_clear_message(overrides: dict[str, str], env_name: str) -> None:
    with pytest.raises(ProviderConfigError, match=env_name):
        build_llm(make(**overrides), PROMPT)


def test_blank_key_counts_as_missing() -> None:
    with pytest.raises(ProviderConfigError):
        build_stt(make(groq_api_key="   "))


def test_groq_stt_autodetects_language_by_default() -> None:
    stt = build_stt(make(groq_api_key="gsk_test"))
    assert type(stt).__name__ == "GroqSTTService"
    assert stt._settings.model == "whisper-large-v3-turbo"
    assert stt._settings.language is None


def test_stt_language_mapping() -> None:
    assert stt_language(make()) is None
    assert stt_language(make(stt_language="ur")) is not None
    with pytest.raises(ProviderConfigError, match="STT_LANGUAGE"):
        stt_language(make(stt_language="klingon"))


def test_faster_whisper_runs_on_cpu(
    monkeypatch: pytest.MonkeyPatch, recorder: type[Recorder]
) -> None:
    import pipecat.services.whisper.stt as whisper_mod

    monkeypatch.setattr(whisper_mod, "WhisperSTTService", recorder)
    build_stt(make(stt_provider="faster_whisper", faster_whisper_model="base"))
    (kwargs,) = recorder.calls
    assert kwargs["device"] == "cpu"
    assert kwargs["settings"].kwargs["model"] == "base"


@pytest.mark.parametrize(
    ("provider", "module", "cls", "voice"),
    [
        ("kokoro", "pipecat.services.kokoro.tts", "KokoroTTSService", "af_heart"),
        ("piper", "pipecat.services.piper.tts", "PiperTTSService", "en_US-lessac-medium"),
    ],
)
def test_tts_selected_by_config(
    monkeypatch: pytest.MonkeyPatch,
    recorder: type[Recorder],
    provider: str,
    module: str,
    cls: str,
    voice: str,
) -> None:
    import importlib

    monkeypatch.setattr(importlib.import_module(module), cls, recorder)
    build_tts(make(tts_provider=provider))
    (kwargs,) = recorder.calls
    assert kwargs["settings"].kwargs["voice"] == voice


def test_build_services_checks_llm_key_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_: Settings) -> None:
        raise AssertionError("TTS must not load before the LLM key is validated")

    monkeypatch.setattr(providers, "build_tts", boom)
    with pytest.raises(ProviderConfigError):
        providers.build_services(make(), PROMPT)


def test_describe_providers_never_includes_secrets() -> None:
    s = make(groq_api_key="gsk_super_secret")
    assert describe_providers(s) == "stt=groq llm=groq tts=kokoro"
