"""What a call achieved, decided from its successful tool calls (DECISIONS D21).

Shared by the api's post-call analysis and the eval suite, so "booked" means the same thing in
the dashboard and in the scorecard: a `create_booking` that returned `booked: true`, whatever the
conversation sounded like.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

Outcome = Literal["booked", "rescheduled", "cancelled", "message", "handoff", "no_action"]


@dataclass(frozen=True)
class ToolOutcome:
    name: str
    ok: bool
    result: dict[str, Any]


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
