"""Post-call analysis: summary, intent, outcome, sentiment.

- **Outcome** comes from the tool calls, deterministically: a successful `create_booking` means
  "booked", whatever the transcript sounds like. Facts don't need an LLM.
- **Summary, intent, sentiment** come from an LLM (Groq, JSON mode, validated with pydantic).
  With no key, or if the LLM fails, a simple heuristic fills them in so the dashboard never
  shows an empty row.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

Outcome = Literal["booked", "rescheduled", "cancelled", "message", "handoff", "no_action"]
Sentiment = Literal["positive", "neutral", "negative"]

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(frozen=True)
class TranscriptLine:
    speaker: str  # "caller" | "agent"
    text: str


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    ok: bool
    result: dict[str, Any]


class Analysis(BaseModel):
    summary: str = Field(max_length=600)
    intent: str = Field(max_length=32)
    sentiment: Sentiment
    outcome: Outcome = "no_action"


def outcome_from_tools(tools: Sequence[ToolOutcome]) -> Outcome:
    """What actually happened, from successful tool results (last decisive one wins)."""
    outcome: Outcome = "no_action"
    for t in tools:
        if not t.ok:
            continue
        if t.name == "create_booking" and t.result.get("booked") is True:
            outcome = "booked"
        elif t.name == "reschedule_booking" and t.result.get("rescheduled") is True:
            outcome = "rescheduled"
        elif t.name == "cancel_booking" and t.result.get("cancelled") is True:
            outcome = "cancelled"
        elif t.name == "transfer_to_human":
            outcome = "handoff"
        elif t.name == "take_message" and outcome == "no_action":
            outcome = "message"
    return outcome


_INTENT_BY_OUTCOME = {
    "booked": "book",
    "rescheduled": "reschedule",
    "cancelled": "cancel",
    "message": "message",
    "handoff": "handoff",
    "no_action": "question",
}


class Summarizer(Protocol):
    async def analyze(
        self, business: str, lines: Sequence[TranscriptLine], tools: Sequence[ToolOutcome]
    ) -> Analysis: ...


class HeuristicSummarizer:
    """No LLM: first thing the caller said + the outcome. Always works."""

    async def analyze(
        self, business: str, lines: Sequence[TranscriptLine], tools: Sequence[ToolOutcome]
    ) -> Analysis:
        outcome = outcome_from_tools(tools)
        first = next((ln.text for ln in lines if ln.speaker == "caller" and ln.text.strip()), "")
        summary = f"Caller said: {first[:200]}" if first else "No speech from the caller."
        return Analysis(
            summary=f"{summary} Outcome: {outcome.replace('_', ' ')}.",
            intent=_INTENT_BY_OUTCOME[outcome],
            sentiment="neutral",
            outcome=outcome,
        )


SYSTEM = """You analyse phone calls to a small business's AI receptionist.
Return JSON with exactly these keys:
- "summary": 1-2 plain English sentences for the business owner (what the caller wanted, what
  happened, anything the owner must do). English even if the call was in Urdu.
- "intent": one of book, reschedule, cancel, question, message, handoff, complaint, other.
- "sentiment": the caller's mood: positive, neutral or negative.
Do not invent details that are not in the transcript."""


class GroqSummarizer:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        client: httpx.AsyncClient | None = None,
        fallback: Summarizer | None = None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._fallback = fallback or HeuristicSummarizer()

    async def analyze(
        self, business: str, lines: Sequence[TranscriptLine], tools: Sequence[ToolOutcome]
    ) -> Analysis:
        outcome = outcome_from_tools(tools)
        transcript = "\n".join(f"{ln.speaker}: {ln.text}" for ln in lines)[-12000:]
        body = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": f"Business: {business}\nOutcome recorded by the "
                    f"system: {outcome}\n\nTranscript:\n{transcript}",
                },
            ],
        }
        try:
            response = await self._client.post(
                GROQ_URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            analysis = Analysis.model_validate(json.loads(content) | {"outcome": outcome})
        except (httpx.HTTPError, KeyError, ValueError, ValidationError) as exc:
            logger.warning("LLM summary failed, using heuristic", error=type(exc).__name__)
            return await self._fallback.analyze(business, lines, tools)
        return analysis
