"""Request/response models for the agent -> api tool endpoints.

Both sides import these, so the wire format is defined once and validated on both ends.
All datetimes are timezone-aware ISO 8601.
"""

from __future__ import annotations

from datetime import date, time
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookingStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class AvailabilityRequest(_Model):
    service: str
    day: date
    preferred_time: time | None = None
    call_id: str  # holds by this call don't hide slots from itself


class AvailabilityResponse(_Model):
    day: date
    options: list[AwareDatetime]
    requested_day_available: bool


class HoldRequest(_Model):
    service: str
    start: AwareDatetime
    call_id: str


class HoldResponse(_Model):
    held: bool
    expires_in_secs: int = 0


class BookingCreate(_Model):
    service: str
    start: AwareDatetime
    caller_name: str = Field(min_length=1, max_length=120)
    phone: str = Field(pattern=r"^\+92\d{9,10}$")
    call_id: str
    idempotency_key: str = Field(min_length=8, max_length=64)


class RescheduleRequest(_Model):
    start: AwareDatetime
    call_id: str
    idempotency_key: str = Field(min_length=8, max_length=64)


class CancelRequest(_Model):
    call_id: str


class BookingOut(_Model):
    id: str  # short code callers can read out, e.g. "K7Q2M9XD"
    tenant_slug: str
    service: str
    start: AwareDatetime
    end: AwareDatetime
    caller_name: str
    phone: str
    status: BookingStatus
    call_id: str | None = None
    calendar_event_id: str | None = None


class MessageKind(StrEnum):
    MESSAGE = "message"
    HANDOFF = "handoff"


class MessageCreate(_Model):
    kind: MessageKind
    body: str = Field(min_length=1, max_length=2000)
    caller_name: str | None = None
    phone: str | None = None
    call_id: str


class MessageOut(_Model):
    id: str


class LookupRequest(_Model):
    question: str = Field(min_length=1, max_length=500)
    language: str = "en"


class LookupResponse(_Model):
    found: bool
    answer: str = ""
    sources: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- call records (Phase 4)


class Speaker(StrEnum):
    CALLER = "caller"
    AGENT = "agent"


class CallStart(_Model):
    call_id: str = Field(min_length=1, max_length=64)
    channel: str = "webrtc"
    caller_phone: str | None = None
    started_at: AwareDatetime


class TurnIn(_Model):
    idx: int = Field(ge=0)
    speaker: Speaker
    text: str
    interrupted: bool = False
    timings: dict[str, float | None] | None = None  # stage -> ms (see TurnTimingObserver)
    spoken_at: AwareDatetime


class ToolCallIn(_Model):
    name: str = Field(max_length=64)
    arguments: dict[str, object]
    result: dict[str, object]
    ok: bool
    duration_ms: int = Field(ge=0)
    called_at: AwareDatetime


class CallEnd(_Model):
    ended_at: AwareDatetime
    language: str | None = None
    recording_path: str | None = None


class Admission(_Model):
    """May a business take one more call now? (Phase 9 limits; reason when it may not)."""

    allowed: bool
    reason: Literal["calls_per_hour", "monthly_minutes"] | None = None


class ApiError(_Model):
    """Body of every 4xx the tool endpoints return; `code` is stable, `detail` is for humans."""

    code: str
    detail: str


__all__ = [
    "Admission",
    "ApiError",
    "AvailabilityRequest",
    "AvailabilityResponse",
    "BookingCreate",
    "BookingOut",
    "BookingStatus",
    "CallEnd",
    "CallStart",
    "CancelRequest",
    "HoldRequest",
    "HoldResponse",
    "LookupRequest",
    "LookupResponse",
    "MessageCreate",
    "MessageKind",
    "MessageOut",
    "RescheduleRequest",
    "Speaker",
    "ToolCallIn",
    "TurnIn",
]
