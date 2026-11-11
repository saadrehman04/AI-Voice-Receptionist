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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
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
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.workers.runner import WorkerRunner

from receptionist_agent.brain.backend import BookingBackend, InMemoryBackend
from receptionist_agent.brain.prompt import build_system_prompt, greeting
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.tools import ReceptionistTools, register_tools, tools_schema
from receptionist_agent.language import CallerLanguageTracker, ReplyLanguageRouter
from receptionist_agent.providers import Services, build_services, describe_providers
from receptionist_agent.timing import JsonlTurnWriter, TurnTimingObserver
from receptionist_shared import Settings, get_settings
from receptionist_shared.tenant import TenantConfig, load_tenant_by_slug

# Phase 2 stub: one shared in-memory backend per process. Phase 3 swaps in the api.
_DEFAULT_BACKEND = InMemoryBackend()


@dataclass
class Call:
    """Everything one call owns, built once per connection."""

    call_id: str
    tenant: TenantConfig
    state: CallState
    tools: ReceptionistTools
    services: Services


def tts_stage(services: Services) -> list[FrameProcessor]:
    """The TTS part of the pipeline: one voice, or a language router + switcher for Urdu."""
    if services.urdu_tts is None:
        return [services.tts]
    router = ReplyLanguageRouter(english_tts=services.tts, urdu_tts=services.urdu_tts)
    return [router, ServiceSwitcher(services=[services.tts, services.urdu_tts])]


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
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=vad or SileroVADAnalyzer()),
    )

    def remember_language(language: str) -> None:
        call.state.language = language

    pipeline = Pipeline(
        [
            transport.input(),
            call.services.stt,
            CallerLanguageTracker(remember_language),
            aggregators.user(),
            call.services.llm,
            *tts_stage(call.services),
            transport.output(),
            aggregators.assistant(),
        ]
    )
    task = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=observers or [],
        conversation_id=call.call_id,
    )
    return task, context


def prepare_call(
    settings: Settings,
    *,
    call_id: str,
    tenant_slug: str | None = None,
    backend: BookingBackend | None = None,
) -> Call:
    """Blocking setup for one call (loads YAML and local models). Run it in a thread."""
    tenant = load_tenant_by_slug(tenant_slug or settings.default_tenant, Path(settings.tenants_dir))
    prompt = build_system_prompt(tenant, tenant.now())
    services = build_services(settings, prompt, urdu="ur" in tenant.languages)
    state = CallState()
    tools = ReceptionistTools(tenant, state, backend or _DEFAULT_BACKEND, call_id=call_id)
    return Call(call_id=call_id, tenant=tenant, state=state, tools=tools, services=services)


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
        timing = TurnTimingObserver(call_id=call_id, on_turn=writer.write)
        task, _context = build_task(transport, call, observers=[timing])

        async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller connected", tenant=call.tenant.slug)
            await task.queue_frames([TTSSpeakFrame(greeting(call.tenant))])

        async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller disconnected")
            await task.cancel()

        transport.add_event_handler("on_client_connected", on_client_connected)
        transport.add_event_handler("on_client_disconnected", on_client_disconnected)

        runner = WorkerRunner(handle_sigint=handle_sigint)
        await runner.add_workers(task)
        await runner.run()
        logger.info("call ended", outcome=call.state.intent.value, booked=call.state.booking_id)


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
