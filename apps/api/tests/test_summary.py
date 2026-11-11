import json

import httpx
import respx

from receptionist_api.summary import (
    GROQ_URL,
    GroqSummarizer,
    HeuristicSummarizer,
    ToolOutcome,
    TranscriptLine,
    outcome_from_tools,
)

LINES = [
    TranscriptLine("agent", "Hello, Noor Beauty Salon."),
    TranscriptLine("caller", "I want a haircut tomorrow at two."),
    TranscriptLine("agent", "Booked, see you then."),
]


def test_outcome_comes_from_successful_tools() -> None:
    assert outcome_from_tools([]) == "no_action"
    booked = ToolOutcome("create_booking", True, {"booked": True})
    readback = ToolOutcome("create_booking", True, {"booked": False})
    assert outcome_from_tools([readback]) == "no_action"
    assert outcome_from_tools([readback, booked]) == "booked"
    assert outcome_from_tools([ToolOutcome("take_message", True, {})]) == "message"
    assert outcome_from_tools([ToolOutcome("transfer_to_human", True, {})]) == "handoff"
    failed = ToolOutcome("create_booking", False, {"booked": True})
    assert outcome_from_tools([failed]) == "no_action"
    assert outcome_from_tools([booked, ToolOutcome("take_message", True, {})]) == "booked"


async def test_heuristic_summary() -> None:
    a = await HeuristicSummarizer().analyze("Salon", LINES, [])
    assert "I want a haircut tomorrow at two." in a.summary
    assert a.outcome == "no_action" and a.sentiment == "neutral"


@respx.mock
async def test_groq_summary_uses_llm_json_and_tool_outcome() -> None:
    content = {"summary": "Sara booked a haircut.", "intent": "book", "sentiment": "positive"}
    route = respx.post(GROQ_URL).respond(
        200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
    )
    s = GroqSummarizer("gsk_test", "openai/gpt-oss-120b", client=httpx.AsyncClient())
    a = await s.analyze("Salon", LINES, [ToolOutcome("create_booking", True, {"booked": True})])
    assert a.summary == "Sara booked a haircut."
    assert a.outcome == "booked"  # from tools, not from the LLM
    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"] == {"type": "json_object"}
    assert "caller: I want a haircut" in sent["messages"][1]["content"]
    assert sent["reasoning_effort"] == "low"


@respx.mock
async def test_groq_failure_falls_back_to_heuristic() -> None:
    respx.post(GROQ_URL).respond(200, json={"choices": [{"message": {"content": "not json"}}]})
    s = GroqSummarizer("gsk_test", "m", client=httpx.AsyncClient())
    a = await s.analyze("Salon", LINES, [])
    assert a.summary.startswith("Caller said:")
    respx.post(GROQ_URL).respond(503)
    assert (await s.analyze("Salon", LINES, [])).summary.startswith("Caller said:")
