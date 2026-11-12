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
from receptionist_agent.local_stt import LocalWhisper
from receptionist_shared import Settings

KOKORO_QUIET_TIMEOUT_SECS = 12.0


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


def _has_key(key: SecretStr | None) -> bool:
    return key is not None and bool(key.get_secret_value().strip())


def stt_provider(settings: Settings) -> str:
    """The STT actually used: Groq without its key falls back to local faster-whisper (D37)."""
    if settings.stt_provider == "groq" and not _has_key(settings.groq_api_key):
        return "faster_whisper"
    return settings.stt_provider


def build_local_whisper(settings: Settings, model: str) -> STTService:
    from pipecat.services.whisper.stt import WhisperSTTService

    return WhisperSTTService(
        device="cpu",  # no NVIDIA GPU on the dev laptop
        compute_type="int8",
        settings=WhisperSTTService.Settings(model=model, language=stt_language(settings)),
    )


def build_stt(settings: Settings) -> STTService:
    language = stt_language(settings)
    if settings.stt_provider == "faster_whisper":
        return build_local_whisper(settings, settings.faster_whisper_model)
    if stt_provider(settings) == "faster_whisper":
        logger.warning("GROQ_API_KEY is not set: speech-to-text runs locally (faster-whisper)")
        return build_local_whisper(settings, settings.stt_fallback_model)
    from receptionist_agent.groq_stt import AutoLanguageGroqSTTService

    return AutoLanguageGroqSTTService(
        api_key=_require_key(settings.groq_api_key, "GROQ_API_KEY", "Groq STT"),
        settings=AutoLanguageGroqSTTService.Settings(
            model=settings.groq_stt_model, language=language
        ),
        local_fallback=LocalWhisper(settings.stt_fallback_model),
    )


def _parse_target(settings: Settings, item: str, groq_key: str, what: str) -> LlmTarget | None:
    provider, sep, model = item.partition(":")
    if not sep or not model:
        raise ProviderConfigError(f"{what} entry {item!r} is not provider:model")
    if provider == "groq":
        if not groq_key:
            return None  # logged once by llm_targets
        return groq_target(model, groq_key, reasoning_effort=settings.groq_reasoning_effort)
    if provider == "gemini":
        key = settings.gemini_api_key
        if key is None or not key.get_secret_value().strip():
            logger.warning(f"{what} skipped: GEMINI_API_KEY is not set", model=model)
            return None
        return gemini_target(model, key.get_secret_value())
    raise ProviderConfigError(f"{what} provider {provider!r} is not groq or gemini")


def llm_targets(settings: Settings) -> list[LlmTarget]:
    """Main Groq model first, then LLM_FALLBACKS whose keys are set (others are skipped).

    Without GROQ_API_KEY the Groq entries are skipped too, so the call runs on the remaining
    fallbacks (Gemini), or on none: then every turn gets the scripted message-taker (D37).
    """
    groq_key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    effort = settings.groq_reasoning_effort
    targets = []
    if groq_key:
        targets.append(groq_target(settings.groq_llm_model, groq_key, reasoning_effort=effort))
    for item in filter(None, (x.strip() for x in settings.llm_fallbacks.split(","))):
        target = _parse_target(settings, item, groq_key, "LLM_FALLBACKS")
        if target is not None and target.name not in {t.name for t in targets}:
            targets.append(target)
    if not groq_key:
        names = ", ".join(t.name for t in targets) or "none: callers can only leave a message"
        logger.warning(f"GROQ_API_KEY is not set: the LLM uses its fallbacks ({names})")
    return targets


def simple_turn_target(settings: Settings, targets: list[LlmTarget]) -> str | None:
    """Name of the LLM_SIMPLE_TURN_MODEL target, added to `targets` if not already there."""
    item = settings.llm_simple_turn_model.strip()
    if not item or not targets:
        return None
    groq_key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    target = _parse_target(settings, item, groq_key, "LLM_SIMPLE_TURN_MODEL")
    if target is None:
        return None
    if target.name not in {t.name for t in targets}:
        targets.append(target)
    return target.name


def build_llm(settings: Settings, system_prompt: str) -> LLMService[Any]:
    match settings.llm_provider:
        case "groq":
            from receptionist_agent.groq_pool import PooledGroqLLMService

            targets = llm_targets(settings)
            simple = simple_turn_target(settings, targets)
            return PooledGroqLLMService(
                targets=targets,
                simple_target=simple,
                # The transport sets each target's own key; the client's is never sent.
                api_key=targets[0].api_key if targets else "unused",
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

            # Kokoro sends nothing until a whole sentence is synthesised, at about real time on
            # this CPU, so a 4 s sentence goes 4 s without audio. Pipecat's default 3 s quiet
            # timeout then dropped it as "no audio"; give it room (see DECISIONS D25).
            return KokoroTTSService(
                settings=KokoroTTSService.Settings(voice=settings.kokoro_voice),
                stop_frame_timeout_s=KOKORO_QUIET_TIMEOUT_SECS,
            )
        case "piper":
            return build_piper(settings.piper_voice)
        case "azure":
            if not _azure_ready(settings):
                logger.warning("Azure TTS has no key/region: speaking with local Piper instead")
                return build_piper(settings.piper_voice)
            return build_azure_tts(
                settings,
                voice=settings.azure_voice_en,
                language="en-US",
                fallback=settings.piper_voice,
            )


def build_piper(voice: str) -> TTSService:
    from pipecat.services.piper.tts import PiperTTSService

    return PiperTTSService(settings=PiperTTSService.Settings(voice=voice))


def _azure_ready(settings: Settings) -> bool:
    return _has_key(settings.azure_speech_key) and bool(settings.azure_speech_region)


def build_azure_tts(
    settings: Settings, *, voice: str, language: str, fallback: str | None = None
) -> TTSService:
    """Azure AI Speech, streaming over its SDK, with word timestamps (DECISIONS D30).

    `fallback`: a Piper voice that speaks a sentence Azure failed on (D37).
    """
    key = _require_key(settings.azure_speech_key, "AZURE_SPEECH_KEY", "Azure TTS")
    if not settings.azure_speech_region:
        raise ProviderConfigError(
            "Azure TTS is selected but AZURE_SPEECH_REGION is not set (e.g. eastus). "
            "It is shown next to the key in the Azure portal."
        )
    from receptionist_agent.azure_tts import QuickStopAzureTTSService
    from receptionist_agent.local_tts import PiperVoiceFallback

    return QuickStopAzureTTSService(
        local_fallback=PiperVoiceFallback(fallback) if fallback else None,
        api_key=key,
        region=settings.azure_speech_region,
        settings=QuickStopAzureTTSService.Settings(voice=voice, language=language),
    )


def urdu_tts_provider(settings: Settings) -> str:
    """Who speaks Urdu replies; Azure without its key/region falls back to Piper (D37)."""
    choice = settings.urdu_tts_provider
    if choice == "auto":
        choice = "azure" if settings.tts_provider == "azure" else "piper"
    if choice == "azure" and not _azure_ready(settings):
        return "piper"
    return choice


def build_urdu_tts(settings: Settings) -> TTSService:
    """The voice for Urdu replies: Azure's ur-PK voices, or Piper's (DECISIONS D10, D30)."""
    if urdu_tts_provider(settings) == "azure":
        return build_azure_tts(
            settings,
            voice=settings.azure_voice_ur,
            language="ur-PK",
            fallback=settings.piper_urdu_voice,
        )
    return build_piper(settings.piper_urdu_voice)


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
    tts: str = settings.tts_provider
    if tts == "azure" and not _azure_ready(settings):
        tts = "piper"
    if urdu_tts_provider(settings) != tts:
        tts += f" (urdu: {urdu_tts_provider(settings)})"
    stt = stt_provider(settings)
    if stt == "groq":
        stt += " (fallback: local whisper)"
    return f"stt={stt} llm={llm} tts={tts}"
