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

from loguru import logger
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pydantic import SecretStr

from receptionist_agent.llm_fallback import LlmTarget, gemini_target, groq_target
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


def llm_targets(settings: Settings) -> list[LlmTarget]:
    """Main Groq model first, then LLM_FALLBACKS whose keys are set (others are skipped)."""
    groq_key = _require_key(settings.groq_api_key, "GROQ_API_KEY", "Groq LLM")
    effort = settings.groq_reasoning_effort
    targets = [groq_target(settings.groq_llm_model, groq_key, reasoning_effort=effort)]
    for item in filter(None, (x.strip() for x in settings.llm_fallbacks.split(","))):
        provider, sep, model = item.partition(":")
        if not sep or not model:
            raise ProviderConfigError(f"LLM_FALLBACKS entry {item!r} is not provider:model")
        if provider == "groq":
            targets.append(groq_target(model, groq_key, reasoning_effort=effort))
        elif provider == "gemini":
            key = settings.gemini_api_key
            if key is None or not key.get_secret_value().strip():
                logger.warning("LLM fallback skipped: GEMINI_API_KEY is not set", model=model)
                continue
            targets.append(gemini_target(model, key.get_secret_value()))
        else:
            raise ProviderConfigError(f"LLM_FALLBACKS provider {provider!r} is not groq or gemini")
    return targets


def build_llm(settings: Settings, system_prompt: str) -> LLMService[Any]:
    match settings.llm_provider:
        case "groq":
            from receptionist_agent.groq_pool import PooledGroqLLMService

            targets = llm_targets(settings)
            return PooledGroqLLMService(
                targets=targets,
                api_key=targets[0].api_key,
                settings=PooledGroqLLMService.Settings(
                    model=settings.groq_llm_model,
                    system_instruction=system_prompt,
                    reasoning_effort=settings.groq_reasoning_effort,
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
    llm: str = settings.llm_provider
    if llm == "groq" and settings.llm_fallbacks.strip():
        llm += f" (fallbacks: {settings.llm_fallbacks})"
    return f"stt={settings.stt_provider} llm={llm} tts={settings.tts_provider}"
