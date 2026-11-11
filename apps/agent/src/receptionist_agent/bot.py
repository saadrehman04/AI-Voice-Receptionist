"""One phone call = one run of `run_bot`.

Pipeline (each arrow is a stream of frames, nothing waits for a whole utterance):

    transport.input -> STT -> user aggregator -> LLM -> TTS -> transport.output
                              (VAD + end-of-turn)                        -> assistant aggregator

- The user aggregator holds Silero VAD and Pipecat's Smart Turn v3 (CPU, ONNX) to decide
  when the caller has finished, then appends the transcript to the shared `LLMContext`.
- The assistant aggregator records what the bot actually said back into the same context.
- `TurnTimingObserver` watches frames from the side and writes per-turn latencies.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.observers.base_observer import BaseObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

from receptionist_agent.prompts import PHASE1_BUSINESS_NAME, PHASE1_SYSTEM_PROMPT
from receptionist_agent.providers import Services, build_services, describe_providers
from receptionist_agent.timing import JsonlTurnWriter, TurnTimingObserver
from receptionist_shared import Settings, get_settings

GREETING = f"Hello, thank you for calling {PHASE1_BUSINESS_NAME}. How can I help you today?"


def build_task(
    transport: BaseTransport,
    services: Services,
    *,
    call_id: str,
    observers: list[BaseObserver] | None = None,
    vad: SileroVADAnalyzer | None = None,
) -> tuple[PipelineWorker, LLMContext]:
    """Assemble the pipeline for one call. Pure wiring, no I/O, so it is easy to test."""
    context = LLMContext()
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=vad or SileroVADAnalyzer()),
    )
    pipeline = Pipeline(
        [
            transport.input(),
            services.stt,
            aggregators.user(),
            services.llm,
            services.tts,
            transport.output(),
            aggregators.assistant(),
        ]
    )
    task = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=observers or [],
        conversation_id=call_id,
    )
    return task, context


async def run_bot(
    transport: BaseTransport,
    settings: Settings,
    *,
    call_id: str,
    handle_sigint: bool = False,
) -> None:
    """Run one call until the caller hangs up."""
    with logger.contextualize(call_id=call_id):
        logger.info("call starting", providers=describe_providers(settings))
        # Local models load inside constructors (seconds of CPU): keep that off the loop.
        services = await asyncio.to_thread(build_services, settings, PHASE1_SYSTEM_PROMPT)
        writer = JsonlTurnWriter(Path(settings.turns_log_path))
        timing = TurnTimingObserver(call_id=call_id, on_turn=writer.write)
        task, _context = build_task(transport, services, call_id=call_id, observers=[timing])

        async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller connected")
            await task.queue_frames([TTSSpeakFrame(GREETING)])

        async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
            logger.info("caller disconnected")
            await task.cancel()

        transport.add_event_handler("on_client_connected", on_client_connected)
        transport.add_event_handler("on_client_disconnected", on_client_disconnected)

        runner = PipelineRunner(handle_sigint=handle_sigint)
        await runner.run(task)
        logger.info("call ended")


def webrtc_transport(runner_args: SmallWebRTCRunnerArguments) -> SmallWebRTCTransport:
    return SmallWebRTCTransport(
        webrtc_connection=runner_args.webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point the Pipecat runner calls for each new browser connection."""
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise TypeError(f"unsupported transport: {type(runner_args).__name__}")
    call_id = runner_args.session_id or uuid.uuid4().hex
    await run_bot(
        webrtc_transport(runner_args),
        get_settings(),
        call_id=call_id,
        handle_sigint=runner_args.handle_sigint,
    )
