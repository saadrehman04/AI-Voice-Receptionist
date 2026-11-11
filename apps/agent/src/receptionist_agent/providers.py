"""Provider factories: the one place that maps config to concrete STT / LLM / TTS services.

The interfaces are Pipecat's own base classes (`STTService`, `LLMService`, `TTSService`), so
the rest of the pipeline never knows which vendor it talks to. Swapping a provider is a
change to `.env` (`STT_PROVIDER`, `LLM_PROVIDER`, `TTS_PROVIDER`), never to code.

Local providers (faster-whisper, Kokoro, Piper) load their models inside the constructor,
which blocks for seconds. Call `build_services` through `asyncio.to_thread` from async code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pydantic import SecretStr

from receptionist_shared import Settings


class ProviderConfigError(RuntimeError):
    """A provider is selected but something it needs (e.g. an API key) is missing."""


@dataclass(frozen=True)
class Services:
    stt: STTService
    llm: LLMService[Any]
    tts: TTSService
    urdu_tts: TTSService | None = None  # set when the tenant speaks Urdu


def _require_key(key: SecretStr | None, env_name: str, provider: str) -> str:
    if key is None or not key.get_secret_value().strip():
        raise ProviderConfigError(
            f"{provider} is selected but {env_name} is not set. Add it to .env "
            f"or choose another provider."
        )
    return key.get_secret_value()


def stt_language(settings: Settings) -> Language | None:
    """Map STT_LANGUAGE to Pipecat's Language enum; None means auto-detect."""
    if settings.stt_language is None:
        return None
    try:
        return Language(settings.stt_language)
    except ValueError as exc:
        raise ProviderConfigError(
            f"STT_LANGUAGE={settings.stt_language!r} is not a language Pipecat knows"
        ) from exc


def build_stt(settings: Settings) -> STTService:
    language = stt_language(settings)
    match settings.stt_provider:
        case "groq":
            from pipecat.services.groq.stt import GroqSTTService

            return GroqSTTService(
                api_key=_require_key(settings.groq_api_key, "GROQ_API_KEY", "Groq STT"),
                settings=GroqSTTService.Settings(model=settings.groq_stt_model, language=language),
            )
        case "faster_whisper":
            from pipecat.services.whisper.stt import WhisperSTTService

            return WhisperSTTService(
                device="cpu",  # no NVIDIA GPU on the dev laptop
                compute_type="int8",
                settings=WhisperSTTService.Settings(
                    model=settings.faster_whisper_model, language=language
                ),
            )


def build_llm(settings: Settings, system_prompt: str) -> LLMService[Any]:
    match settings.llm_provider:
        case "groq":
            from pipecat.services.groq.llm import GroqLLMService

            return GroqLLMService(
                api_key=_require_key(settings.groq_api_key, "GROQ_API_KEY", "Groq LLM"),
                settings=GroqLLMService.Settings(
                    model=settings.groq_llm_model, system_instruction=system_prompt
                ),
            )
        case "gemini":
            from pipecat.services.google.llm import GoogleLLMService

            return GoogleLLMService(
                api_key=_require_key(settings.gemini_api_key, "GEMINI_API_KEY", "Gemini LLM"),
                settings=GoogleLLMService.Settings(
                    model=settings.gemini_model, system_instruction=system_prompt
                ),
            )
        case "ollama":
            from pipecat.services.ollama.llm import OLLamaLLMService

            return OLLamaLLMService(
                base_url=settings.ollama_base_url,
                settings=OLLamaLLMService.Settings(
                    model=settings.ollama_model, system_instruction=system_prompt
                ),
            )


def build_tts(settings: Settings) -> TTSService:
    match settings.tts_provider:
        case "kokoro":
            from pipecat.services.kokoro.tts import KokoroTTSService

            return KokoroTTSService(settings=KokoroTTSService.Settings(voice=settings.kokoro_voice))
        case "piper":
            from pipecat.services.piper.tts import PiperTTSService

            return PiperTTSService(settings=PiperTTSService.Settings(voice=settings.piper_voice))


def build_urdu_tts(settings: Settings) -> TTSService:
    """Piper's Urdu voice: the only free Urdu TTS we found (DECISIONS D10)."""
    from pipecat.services.piper.tts import PiperTTSService

    return PiperTTSService(settings=PiperTTSService.Settings(voice=settings.piper_urdu_voice))


def build_services(settings: Settings, system_prompt: str, *, urdu: bool = False) -> Services:
    """Build all services. Cloud ones are validated first so a missing key fails fast.

    `urdu=True` adds a second, Urdu TTS for tenants that speak Urdu.
    """
    llm = build_llm(settings, system_prompt)
    stt = build_stt(settings)
    tts = build_tts(settings)
    urdu_tts = build_urdu_tts(settings) if urdu else None
    return Services(stt=stt, llm=llm, tts=tts, urdu_tts=urdu_tts)


def describe_providers(settings: Settings) -> str:
    """One line describing the configured STT/LLM/TTS providers (never secrets)."""
    return f"stt={settings.stt_provider} llm={settings.llm_provider} tts={settings.tts_provider}"
