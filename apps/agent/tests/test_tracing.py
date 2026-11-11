"""Tracing helpers: spans for tools and turn latency, checked with an in-memory exporter."""

from collections.abc import Iterator
from datetime import datetime

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from receptionist_agent import tracing
from receptionist_agent.brain.backend import InMemoryBackend
from receptionist_agent.brain.state import CallState
from receptionist_agent.brain.tools import ReceptionistTools
from receptionist_shared import Settings
from receptionist_shared.tenant import TenantConfig


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("test"))
    yield exporter


def test_tracing_is_off_unless_enabled() -> None:
    assert tracing.configure_tracing(Settings(_env_file=None)) is False


async def test_each_tool_call_is_a_span(
    spans: InMemorySpanExporter, salon: TenantConfig, monday_9am: datetime
) -> None:
    tools = ReceptionistTools(
        salon, CallState(), InMemoryBackend(clock=lambda: monday_9am), call_id="t1"
    )
    await tools.run("lookup_business_info", {"question": "Is there parking?"})
    await tools.run("lookup_business_info", {"question": "Do you sell gift vouchers?"})
    await tools.run("check_availability", {"service": "massage", "date": "2026-11-18"})
    found, not_found, bad_service = spans.get_finished_spans()
    assert found.name == "tool lookup_business_info"
    assert found.attributes is not None and found.attributes["call.id"] == "t1"
    # A lookup that ran is ok, whether or not it found an answer; a bad service is not.
    assert found.attributes["tool.ok"] is True
    assert not_found.attributes is not None and not_found.attributes["tool.ok"] is True
    assert bad_service.attributes is not None and bad_service.attributes["tool.ok"] is False


def test_turn_latency_span_has_an_event_per_stage(spans: InMemorySpanExporter) -> None:
    record = {
        "call_id": "c1",
        "turn_id": "t1",
        "turn_index": 2,
        "stt_final": 410.0,
        "turn_released": 415.0,
        "llm_first_token": 900.0,
        "tts_first_audio": 1100.0,
        "bot_started_speaking": 1120.0,
        "stages": {"stt": 410.0, "llm": 485.0, "tts": 200.0, "total": 1120.0},
    }
    tracing.record_turn_span(record)
    (span,) = spans.get_finished_spans()
    assert span.name == "turn latency"
    assert span.end_time is not None and span.start_time is not None
    assert (span.end_time - span.start_time) / 1e6 == pytest.approx(1120.0)
    assert [e.name for e in span.events] == [
        "caller stopped speaking",
        "STT final",
        "end of turn",
        "LLM first token",
        "TTS first audio",
        "bot started speaking",
    ]
    assert span.attributes is not None and span.attributes["stage.llm_ms"] == 485.0


def test_turn_without_audio_makes_no_span(spans: InMemorySpanExporter) -> None:
    tracing.record_turn_span({"call_id": "c1", "stages": {}})
    assert spans.get_finished_spans() == ()
