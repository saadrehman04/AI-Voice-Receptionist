"""One phone call = one run of `run_bot`.

Pipeline (each arrow is a stream of frames; nothing waits for a whole utterance):

    transport.input -> STT -> caller language -> user aggregator -> LLM
        -> reply language router -> TTS (Kokoro | Piper Urdu) -> transport.output
        -> assistant aggregator

- The user aggregator holds Silero VAD and Pipecat's Smart Turn v3 (CPU, ONNX) to decide
  when the caller has finished, then appends the transcript to the shared `LLMContext`.
- The LLM sees the tenant's facts in its system prompt and calls our tools (brain/tools.py).
- The reply router picks the voice per reply (Urdu script -> Piper Urdu, else Kokoro).
- The assistant aggregator records what the bot actually said back into the context.
- `TurnTimingObserver` watches frames from the side and writes per-turn latencies.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.service_switcher import ServiceSwitcher
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from receptionist_agent.brain.api_backend import ApiBackend
from receptionist_agent.brain.backend import BookingBackend, InMemoryBackend
from receptionist_agent.brain.prompt import build_system_prompt, greeting
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.tools import (
    ReceptionistTools,
    ToolCallHook,
    register_tools,
    tools_schema,
)
from receptionist_agent.language import CallerLanguageTracker, ReplyLanguageRouter
from receptionist_agent.latency import (
    FullReplyBuffer,
    LatencyFlags,
    NumberPauseTuner,
    ToolFiller,
    prewarm,
    use_clause_chunking,
)
from receptionist_agent.providers import Services, build_services, describe_providers
from receptionist_agent.records import (
    ApiCallRecorder,
    CallRecorder,
    MemoryRecorder,
    write_wav,
)
from receptionist_agent.timing import JsonlTurnWriter, TurnTimingObserver
from receptionist_agent.tracing import configure_tracing, record_turn_span
from receptionist_shared import Settings, get_settings
from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

_backend: BookingBackend | None = None


def default_backend(settings: Settings) -> BookingBackend:
    """One backend per process, shared by all calls (BOOKING_BACKEND=memory|api)."""
    global _backend
    if _backend is None:
        if settings.booking_backend == "api":
            if settings.agent_api_token is None:
                raise RuntimeError("BOOKING_BACKEND=api needs AGENT_API_TOKEN")
            _backend = ApiBackend(
                settings.api_base_url, settings.agent_api_token.get_secret_value()
            )
        else:
            _backend = InMemoryBackend()
    return _backend


@dataclass
class Call:
    """Everything one call owns, built once per connection."""

    call_id: str
    tenant: TenantConfig
    state: CallState
    tools: ReceptionistTools
    services: Services
    recorder: CallRecorder = field(default_factory=lambda: MemoryRecorder("unknown"))
    recording: AudioBufferProcessor | None = None
    recording_path: Path | None = None
    flags: LatencyFlags = field(default_factory=LatencyFlags)
    tracing: bool = False  # OpenTelemetry spans for this call (OTEL_ENABLED)


def make_recorder(call_id: str, tenant: TenantConfig, backend: BookingBackend) -> CallRecorder:
    """Records go to the api when we have one; otherwise they stay in memory."""
    if isinstance(backend, ApiBackend):

        async def send(path: str, body: dict[str, Any]) -> None:
            await backend.record(tenant, path, body)

        return ApiCallRecorder(call_id, send)
    return MemoryRecorder(call_id)


def tts_stage(services: Services) -> list[FrameProcessor]:
    """The TTS part of the pipeline: one voice, or a language router + switcher for Urdu."""
    if services.urdu_tts is None:
        return [services.tts]
    router = ReplyLanguageRouter(english_tts=services.tts, urdu_tts=services.urdu_tts)
    return [router, ServiceSwitcher(services=[services.tts, services.urdu_tts])]


def speech_stage(call: Call) -> list[FrameProcessor]:
    """Between the LLM and TTS: the latency features the flags turn on (see latency.py)."""
    flags = call.flags
    stage: list[FrameProcessor] = []
    if flags.filler_after_ms > 0:

        def language() -> str | None:
            return call.state.language

        stage.append(ToolFiller(after_ms=flags.filler_after_ms, language=language))
    if flags.vad_number_stop_secs > 0:
        stage.append(
            NumberPauseTuner(
                normal_secs=flags.vad_stop_secs, number_secs=flags.vad_number_stop_secs
            )
        )
    if flags.text_chunking == "full":
        stage.append(FullReplyBuffer())
    elif flags.text_chunking == "clause":
        for tts in (call.services.tts, call.services.urdu_tts):
            if tts is not None:
                use_clause_chunking(tts)
    return stage


def build_task(
    transport: BaseTransport,
    call: Call,
    *,
    observers: list[BaseObserver] | None = None,
    vad: SileroVADAnalyzer | None = None,
) -> tuple[PipelineWorker, LLMContext]:
    """Assemble the pipeline for one call. Pure wiring, no I/O, so it is easy to test."""
    context = LLMContext(tools=tools_schema())
    register_tools(call.services.llm, call.tools)
    vad = vad or SileroVADAnalyzer(params=VADParams(stop_secs=call.flags.vad_stop_secs))
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=vad)
    )

    def remember_language(language: str) -> None:
        call.state.language = language

    async def on_user_turn(_agg: Any, _strategy: Any, message: Any) -> None:
        call.recorder.caller_turn(getattr(message, "content", None) or "")

    async def on_assistant_turn(_agg: Any, message: Any) -> None:
        call.recorder.agent_turn(
            getattr(message, "content", "") or "", interrupted=bool(message.interrupted)
        )

    user, assistant = aggregators.user(), aggregators.assistant()
    user.add_event_handler("on_user_turn_stopped", on_user_turn)
    assistant.add_event_handler("on_assistant_turn_stopped", on_assistant_turn)

    pipeline = Pipeline(
        [
            transport.input(),
            call.services.stt,
            CallerLanguageTracker(remember_language),
            user,
            call.services.llm,
            *speech_stage(call),
            *tts_stage(call.services),
            transport.output(),
            *([call.recording] if call.recording is not None else []),
            assistant,
        ]
    )
    task = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=observers or [],
        conversation_id=call.call_id,
        enable_tracing=call.tracing,
        additional_span_attributes={"tenant.slug": call.tenant.slug},
    )
    return task, context


def prepare_call(
    settings: Settings,
    *,
    call_id: str,
    tenant_slug: str | None = None,
    backend: BookingBackend | None = None,
    now: datetime | None = None,
    on_tool_call: ToolCallHook | None = None,
) -> Call:
    """Blocking setup for one call (loads the tenant and local models). Run it in a thread.

    `now` freezes the clock (evals); `on_tool_call` also sees every tool call (evals, tests).
    """
    backend = backend or default_backend(settings)
    tenant = load_tenant(settings, tenant_slug or settings.default_tenant, backend)
    frozen = now.astimezone(tenant.tz) if now is not None else None
    prompt = build_system_prompt(tenant, frozen or tenant.now())
    services = build_services(settings, prompt, urdu="ur" in tenant.languages)
    state = CallState()
    recorder = make_recorder(call_id, tenant, backend)

    def record_tool(
        name: str, args: dict[str, Any], result: dict[str, Any], ok: bool, duration_ms: int
    ) -> None:
        recorder.tool_call(name, args, result, ok=ok, duration_ms=duration_ms)
        if on_tool_call is not None:
            on_tool_call(name, args, result, ok, duration_ms)

    clock: Callable[[], datetime] | None = None
    if frozen is not None:
        fixed = frozen

        def clock() -> datetime:
            return fixed

    tools = ReceptionistTools(
        tenant, state, backend, call_id=call_id, on_tool_call=record_tool, clock=clock
    )
    call = Call(
        call_id=call_id,
        tenant=tenant,
        state=state,
        tools=tools,
        services=services,
        recorder=recorder,
        flags=LatencyFlags.from_settings(settings),
        tracing=configure_tracing(settings),
    )
    if settings.record_calls:
        # Stereo: caller on the left channel, agent on the right. Written once at hang-up.
        call.recording = AudioBufferProcessor(num_channels=2, auto_start_recording=True)
        call.recording_path = Path(settings.storage_dir) / tenant.slug / f"{call_id}.wav"
    return call


def load_tenant(settings: Settings, slug: str, backend: BookingBackend) -> TenantConfig:
    """From the api when we have one (tenants can be created there); else from YAML."""
    if isinstance(backend, ApiBackend):
        return asyncio.run(_fetch_tenant(settings, slug))
    return load_tenant_by_slug(slug, Path(settings.tenants_dir))


async def _fetch_tenant(settings: Settings, slug: str) -> TenantConfig:
    # A short-lived client: this runs in prepare_call's worker thread, with its own event loop.
    if settings.agent_api_token is None:
        raise RuntimeError("BOOKING_BACKEND=api needs AGENT_API_TOKEN")
    api = ApiBackend(settings.api_base_url, settings.agent_api_token.get_secret_value())
    try:
        return await api.tenant_config(slug)
    finally:
        await api.aclose()


async def run_bot(
    transport: BaseTransport,
    settings: Settings,
    *,
    call_id: str,
    tenant_slug: str | None = None,
    handle_sigint: bool = False,
) -> None:
    """Run one call until the caller hangs up."""
    with logger.contextualize(call_id=call_id):
        logger.info("call starting", providers=describe_providers(settings), tenant=tenant_slug)
        # Local models load inside constructors (seconds of CPU): keep that off the loop.
        call = await asyncio.to_thread(
            prepare_call, settings, call_id=call_id, tenant_slug=tenant_slug
        )
        writer = JsonlTurnWriter(Path(settings.turns_log_path))

        async def on_turn_timing(record: dict[str, Any]) -> None:
            await writer.write(record)
            call.recorder.timing(record)
            if call.tracing:
                record_turn_span(record)

        timing = TurnTimingObserver(call_id=call_id, on_turn=on_turn_timing)
        task, _context = build_task(transport, call, observers=[timing])
        saved_recording: list[Path] = []
        if call.recording is not None and call.recording_path is not None:
            path = call.recording_path

            async def on_audio(_buf: Any, audio: bytes, sample_rate: int, channels: int) -> None:
                await asyncio.to_thread(write_wav, path, audio, sample_rate, channels)
                saved_recording.append(path)

            call.recording.add_event_handler("on_audio_data", on_audio)

        await call.recorder.start()
        # Held for the whole call; a failed warm-up is logged and costs nothing else.
        warmup = (
            asyncio.create_task(prewarm(call.services.stt, call.services.llm))
            if call.flags.prewarm
            else None
        )

        async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller connected", tenant=call.tenant.slug)
            hello = greeting(call.tenant, recording=call.recording is not None)
            await task.queue_frames([TTSSpeakFrame(hello)])

        async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller disconnected")
            await task.cancel()

        transport.add_event_handler("on_client_connected", on_client_connected)
        transport.add_event_handler("on_client_disconnected", on_client_disconnected)

        runner = WorkerRunner(handle_sigint=handle_sigint)
        await runner.add_workers(task)
        try:
            await runner.run()
        finally:
            if warmup is not None and not warmup.done():
                warmup.cancel()
            await call.recorder.end(
                language=call.state.language,
                recording_path=str(saved_recording[0]) if saved_recording else None,
            )
            logger.info("call ended", intent=call.state.intent.value, booked=call.state.booking_id)


def webrtc_transport(runner_args: SmallWebRTCRunnerArguments) -> SmallWebRTCTransport:
    return SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


def tenant_from_body(body: Any) -> str | None:
    """The browser client may pick a tenant: POST /start {"body": {"tenant": "demo-workshop"}}."""
    if isinstance(body, dict):
        value = body.get("tenant")
        if isinstance(value, str) and value:
            return value
    return None


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point the Pipecat runner calls for each new browser connection."""
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise TypeError(f"unsupported transport: {type(runner_args).__name__}")
    call_id = runner_args.session_id or uuid.uuid4().hex
    await run_bot(
        webrtc_transport(runner_args),
        get_settings(),
        call_id=call_id,
        tenant_slug=tenant_from_body(runner_args.body),
        handle_sigint=runner_args.handle_sigint,
    )
