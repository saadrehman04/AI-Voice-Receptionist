from typing import Any

import pytest
from pipecat.services.groq.llm import GroqLLMSettings

from receptionist_agent import providers
from receptionist_agent.groq_stt import AutoLanguageGroqSTTService
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
    groq = llm._settings  # (the service is PooledGroqLLMService; see test_llm_fallback)
    assert isinstance(groq, GroqLLMSettings)
    assert groq.model == "openai/gpt-oss-120b"
    assert groq.system_instruction == PROMPT
    assert groq.reasoning_effort == "low"


def test_groq_reasoning_effort_can_be_turned_off_for_non_reasoning_models() -> None:
    llm = build_llm(make(groq_api_key="gsk_test", groq_reasoning_effort=None), PROMPT)
    groq = llm._settings
    assert isinstance(groq, GroqLLMSettings)
    assert groq.reasoning_effort is None


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
    assert isinstance(stt, AutoLanguageGroqSTTService)
    settings: Any = stt._settings
    assert settings.model == "whisper-large-v3-turbo"
    assert settings.language is None


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
    s = make(groq_api_key="gsk_super_secret", gemini_api_key="AIza_super_secret")
    described = describe_providers(s)
    assert described.startswith("stt=groq llm=groq (fallbacks: groq:openai/gpt-oss-20b,")
    assert "secret" not in described
    assert (
        describe_providers(make(llm_fallbacks="")) == "stt=groq llm=groq tts=kokoro (urdu: piper)"
    )


class FakeTranscriptions:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return "ok"


class FakeAudio:
    def __init__(self) -> None:
        self.transcriptions = FakeTranscriptions()


class FakeClient:
    def __init__(self) -> None:
        self.audio = FakeAudio()


async def test_groq_stt_lets_whisper_detect_the_language_when_unset() -> None:
    """Pipecat's GroqSTTService asserts a language is set; a bilingual line must not send one."""
    stt = providers.build_stt(make(groq_api_key="gsk_test"))
    assert isinstance(stt, AutoLanguageGroqSTTService)
    fake = FakeClient()
    stt._client = fake  # type: ignore[assignment]
    assert await stt._transcribe(b"RIFF") == "ok"
    assert "language" not in fake.audio.transcriptions.kwargs
    assert fake.audio.transcriptions.kwargs["model"] == "whisper-large-v3-turbo"


async def test_groq_stt_sends_a_configured_language() -> None:
    stt = providers.build_stt(make(groq_api_key="gsk_test", stt_language="ur"))
    fake = FakeClient()
    stt._client = fake  # type: ignore[attr-defined]
    await stt._transcribe(b"RIFF")  # type: ignore[attr-defined]
    assert fake.audio.transcriptions.kwargs["language"] == "ur"


def test_kokoro_gets_a_quiet_timeout_longer_than_its_synthesis_time(
    monkeypatch: pytest.MonkeyPatch, recorder: type[Recorder]
) -> None:
    """Pipecat drops a TTS request after 3 s without audio; Kokoro needs ~1x real time."""
    import pipecat.services.kokoro.tts as kokoro_mod

    monkeypatch.setattr(kokoro_mod, "KokoroTTSService", recorder)
    build_tts(make(tts_provider="kokoro"))
    (kwargs,) = recorder.calls
    assert kwargs["stop_frame_timeout_s"] == providers.KOKORO_QUIET_TIMEOUT_SECS > 3.0


# ---------------------------------------------------------------- Azure voices (D30)


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch, recorder: type[Recorder]) -> type[Recorder]:
    import receptionist_agent.azure_tts as azure_mod

    monkeypatch.setattr(azure_mod, "QuickStopAzureTTSService", recorder)
    return recorder


def az(**over: Any) -> Settings:
    return make(azure_speech_key="az-test-key", azure_speech_region="eastus", **over)


def test_azure_english_voice(azure: type[Recorder]) -> None:
    build_tts(az(tts_provider="azure"))
    (kwargs,) = azure.calls
    assert kwargs["region"] == "eastus"
    assert kwargs["settings"].kwargs == {"voice": "en-US-JennyNeural", "language": "en-US"}


def test_urdu_follows_azure_automatically(azure: type[Recorder]) -> None:
    providers.build_urdu_tts(az(tts_provider="azure"))
    (kwargs,) = azure.calls
    assert kwargs["settings"].kwargs == {"voice": "ur-PK-UzmaNeural", "language": "ur-PK"}


def test_urdu_can_use_azure_with_a_local_english_voice(azure: type[Recorder]) -> None:
    providers.build_urdu_tts(az(tts_provider="piper", urdu_tts_provider="azure"))
    assert azure.calls[0]["settings"].kwargs["voice"] == "ur-PK-UzmaNeural"


def test_urdu_stays_on_piper_by_default(
    monkeypatch: pytest.MonkeyPatch, recorder: type[Recorder]
) -> None:
    import pipecat.services.piper.tts as piper_mod

    monkeypatch.setattr(piper_mod, "PiperTTSService", recorder)
    providers.build_urdu_tts(make(tts_provider="kokoro"))
    assert recorder.calls[0]["settings"].kwargs["voice"] == "ur_PK-fasih-medium"


def test_azure_needs_key_and_region() -> None:
    with pytest.raises(ProviderConfigError, match="AZURE_SPEECH_KEY"):
        build_tts(make(tts_provider="azure", azure_speech_region="eastus"))
    with pytest.raises(ProviderConfigError, match="AZURE_SPEECH_REGION"):
        build_tts(make(tts_provider="azure", azure_speech_key="az-test-key"))


def test_describe_shows_the_urdu_voice_provider_without_the_key() -> None:
    described = describe_providers(az(tts_provider="piper", urdu_tts_provider="azure"))
    assert "tts=piper (urdu: azure)" in described
    assert "az-test-key" not in described


def test_real_azure_service_builds_offline() -> None:
    """The real class constructs without a network call (it connects when the call starts)."""
    tts = build_tts(az(tts_provider="azure"))
    assert type(tts).__name__ == "QuickStopAzureTTSService"
