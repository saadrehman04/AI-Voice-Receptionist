"""OpenTelemetry tracing for calls (Phase 5). Off unless `OTEL_ENABLED=true`.

What a trace holds, per call:

- Pipecat's own spans (`enable_tracing` on the worker): `conversation` > `turn` (with
  `turn.user_bot_latency_seconds`) > `stt` / `llm` / `tts` service spans with time to first byte.
- `tool <name>`: one span per tool call, with its result (ok or not) and duration.
- `turn latency`: our stage timings from `TurnTimingObserver`, as a span from "caller stopped"
  to "bot started speaking" with an event at each stage (VAD end, STT final, end of turn, LLM
  first token, TTS first audio).

Export goes to an OTLP/HTTP endpoint; `make traces` starts a local Jaeger to look at them
(http://localhost:16686). With tracing off, the OpenTelemetry API is a no-op, so the tool and
turn spans cost nothing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger
from opentelemetry import trace

from receptionist_shared import Settings

SERVICE_NAME = "receptionist-agent"
NS_PER_MS = 1_000_000
STAGE_EVENTS = (
    ("stt_final", "STT final"),
    ("turn_released", "end of turn"),
    ("llm_first_token", "LLM first token"),
    ("tts_first_audio", "TTS first audio"),
    ("bot_started_speaking", "bot started speaking"),
)

_configured = False
tracer = trace.get_tracer("receptionist")


def configure_tracing(settings: Settings) -> bool:
    """Set up the tracer provider once per process. Returns whether calls should be traced."""
    global _configured
    if not settings.otel_enabled:
        return False
    if _configured:
        return True
    from pipecat.utils.tracing.setup import setup_tracing

    if settings.otel_exporter == "console":
        ok = setup_tracing(SERVICE_NAME, console_export=True)
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        ok = setup_tracing(SERVICE_NAME, exporter=OTLPSpanExporter(endpoint=settings.otel_endpoint))
    _configured = ok
    if ok:
        logger.info("tracing on", exporter=settings.otel_exporter, endpoint=settings.otel_endpoint)
    else:
        logger.warning("tracing requested but could not be set up")
    return ok


@contextmanager
def tool_span(name: str, call_id: str) -> Iterator[trace.Span]:
    with tracer.start_as_current_span(
        f"tool {name}", attributes={"tool.name": name, "call.id": call_id}
    ) as span:
        yield span


def record_turn_span(record: dict[str, Any]) -> None:
    """Turn a `TurnTiming` record into a span ending now, with one event per stage."""
    end_offset = record.get("bot_started_speaking") or record.get("tts_first_audio")
    if end_offset is None:
        return
    end_ns = time.time_ns()
    start_ns = end_ns - int(end_offset * NS_PER_MS)
    span = tracer.start_span(
        "turn latency",
        start_time=start_ns,
        attributes={
            "call.id": str(record.get("call_id")),
            "turn.id": str(record.get("turn_id")),
            "turn.index": int(record.get("turn_index") or 0),
            **{
                f"stage.{k}_ms": float(v)
                for k, v in (record.get("stages") or {}).items()
                if isinstance(v, int | float)
            },
        },
    )
    span.add_event("caller stopped speaking", timestamp=start_ns)
    for key, label in STAGE_EVENTS:
        offset = record.get(key)
        if isinstance(offset, int | float):
            span.add_event(label, timestamp=start_ns + int(offset * NS_PER_MS))
    span.end(end_time=end_ns)
