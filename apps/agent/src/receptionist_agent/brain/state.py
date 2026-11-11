"""What we know about the caller so far, and the rules for changing it.

The LLM drives the conversation, but bookings only happen through tools, and tools go through
this state. That gives us hard guarantees the prompt alone can't:

- A booking needs every detail (service, start time, name, phone).
- A booking needs `confirmed`, which is only set after the caller agreed to a read-back.
- Changing any detail after confirmation clears `confirmed`, so the read-back happens again.
- A slot can only be booked if `check_availability` offered it in this call.
- Every write (book, reschedule, cancel) is two calls: `prepare` returns the read-back, and
  `require_prepared` only lets the commit through with exactly the same details.
- ...and only after the caller has spoken since the read-back. The LLM can't read back and
  confirm in one breath (a real model did exactly that, inventing the caller's "yes"); the
  commit needs a new caller turn, counted from the LLM context (`heard_caller`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import ClassVar


class Intent(StrEnum):
    UNKNOWN = "unknown"
    BOOK = "book"
    RESCHEDULE = "reschedule"
    CANCEL = "cancel"
    QUESTION = "question"
    MESSAGE = "message"
    HANDOFF = "handoff"


class StateError(ValueError):
    """An action is not allowed in the current state; the message is safe to show the LLM."""


@dataclass
class CallState:
    caller_name: str | None = None
    phone: str | None = None  # E.164
    intent: Intent = Intent.UNKNOWN
    service: str | None = None
    start: datetime | None = None  # chosen slot, tenant timezone
    preferred_date: str | None = None  # what the caller asked for, as said ("tomorrow")
    preferred_time: str | None = None
    confirmed: bool = False
    offered_slots: list[datetime] = field(default_factory=list)
    booking_id: str | None = None
    ended: bool = False
    language: str | None = None  # last language the caller used ("en" / "ur")
    pending: tuple[str, tuple[object, ...]] | None = None  # (action, details) awaiting a yes
    caller_turns: int = 0  # caller messages heard so far in this call
    pending_turn: int | None = None  # caller_turns when the read-back was given
    agent_spoke: bool = False  # anything said since the caller last spoke (SpokeObserver)

    REQUIRED_FOR_BOOKING: ClassVar[tuple[str, ...]] = ("service", "start", "caller_name", "phone")

    def set_intent(self, intent: Intent) -> None:
        if intent != self.intent:
            self.intent = intent
            self.confirmed = False

    def update(
        self,
        *,
        service: str | None = None,
        start: datetime | None = None,
        caller_name: str | None = None,
        phone: str | None = None,
        preferred_date: str | None = None,
        preferred_time: str | None = None,
    ) -> list[str]:
        """Apply new details. Returns the names of fields that actually changed.

        Any change to a booking detail withdraws an earlier confirmation.
        """
        changed: list[str] = []
        for name, value in (
            ("service", service),
            ("start", start),
            ("caller_name", caller_name),
            ("phone", phone),
            ("preferred_date", preferred_date),
            ("preferred_time", preferred_time),
        ):
            if value is not None and getattr(self, name) != value:
                setattr(self, name, value)
                changed.append(name)
        if self.confirmed and any(c in self.REQUIRED_FOR_BOOKING for c in changed):
            self.confirmed = False
        return changed

    def record_offer(self, slots: list[datetime]) -> None:
        for slot in slots:
            if slot not in self.offered_slots:
                self.offered_slots.append(slot)

    def missing_for_booking(self) -> list[str]:
        return [name for name in self.REQUIRED_FOR_BOOKING if getattr(self, name) is None]

    def confirm(self) -> None:
        """Mark the read-back as agreed. Only valid once every detail is known and offered."""
        missing = self.missing_for_booking()
        if missing:
            raise StateError(f"still missing: {', '.join(missing)}")
        if self.start not in self.offered_slots:
            raise StateError("that time was not offered; call check_availability first")
        self.confirmed = True

    def mark_booked(self, booking_id: str) -> None:
        if not self.confirmed:
            raise StateError("cannot book before the caller confirms the details")
        self.booking_id = booking_id

    def heard_caller(self, turns: int) -> None:
        """Update how many caller turns there have been (never goes backwards)."""
        self.caller_turns = max(self.caller_turns, turns)

    def prepare(self, action: str, details: tuple[object, ...]) -> None:
        """Remember what was read back to the caller; a later commit must match it exactly."""
        self.pending = (action, details)
        self.pending_turn = self.caller_turns
        if action == "book":
            self.confirmed = False

    def require_prepared(self, action: str, details: tuple[object, ...]) -> None:
        if self.pending is None or self.pending[0] != action:
            raise StateError("read the details back first: call again with caller_confirmed=false")
        if self.pending[1] != details:
            raise StateError("these details differ from what was read back; read them back again")
        if self.pending_turn is not None and self.caller_turns <= self.pending_turn:
            raise StateError(
                "the caller has not answered yet: read the details back, then stop and wait "
                "for their reply before calling with caller_confirmed=true"
            )

    def clear_pending(self) -> None:
        self.pending = None
        self.pending_turn = None

    def summary(self) -> dict[str, object]:
        """Compact view for logs and tool results (no secrets in here)."""
        return {
            "intent": self.intent.value,
            "service": self.service,
            "start": self.start.isoformat() if self.start else None,
            "caller_name": self.caller_name,
            "phone": self.phone,
            "confirmed": self.confirmed,
            "booking_id": self.booking_id,
        }
