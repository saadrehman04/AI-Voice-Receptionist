"""The receptionist's tools: JSON schemas for the LLM plus the code that runs them.

Each handler takes the LLM's arguments (a dict) and returns a JSON-able dict. Handlers never
raise for caller mistakes: bad input comes back as `{"ok": False, "error": ...}` with a hint the
LLM can act on ("ask the caller to repeat the number"). All writes go through `CallState`, which
enforces the read-back-then-confirm rule.

`register_tools` (bottom) plugs the handlers into a Pipecat LLM service.
"""

from __future__ import annotations

import re
import time as time_module
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time
from typing import Any, Literal

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    EndWorkerFrame,
    FunctionCallResultProperties,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService

from receptionist_agent.brain.backend import (
    Availability,
    Booking,
    BookingBackend,
    BookingNotFoundError,
    SlotTakenError,
    idempotency_key,
)
from receptionist_agent.brain.state import CallState, Intent, StateError
from receptionist_agent.language import caller_language
from receptionist_agent.tracing import tool_span
from receptionist_shared.availability import spoken_slot
from receptionist_shared.phone import normalize_pk_phone, spoken_digits_en, spoken_digits_ur
from receptionist_shared.tenant import Service, TenantConfig

ToolResult = dict[str, Any]
Handler = Callable[[Mapping[str, Any]], Awaitable[ToolResult]]
Clock = Callable[[], datetime]
# (name, arguments, result, ok, duration_ms): used to record tool calls on the call record.
ToolCallHook = Callable[[str, dict[str, Any], ToolResult, bool, int], None]


def _err(message: str, **extra: Any) -> ToolResult:
    return {"ok": False, "error": message, **extra}


def _str(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool(args: Mapping[str, Any], key: str) -> bool:
    value = args.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


# ---------------------------------------------------------------- JSON schemas

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "check_availability",
        "description": (
            "Find free appointment times for a service on a date. Returns at most 3 options. "
            "Only ever offer times this returns."
        ),
        "properties": {
            "service": {"type": "string", "description": "Service name as listed in the facts."},
            "date": {
                "type": "string",
                "description": "Date as YYYY-MM-DD in the business timezone.",
            },
            "preferred_time": {
                "type": "string",
                "description": "Optional preferred time as HH:MM (24 hour).",
            },
        },
        "required": ["service", "date"],
    },
    {
        "name": "create_booking",
        "description": (
            "Book an offered slot. First call with caller_confirmed=false to get the read-back; "
            "read it to the caller; call again with caller_confirmed=true only after a clear yes."
        ),
        "properties": {
            "service": {"type": "string"},
            "start": {
                "type": "string",
                "description": "Start time exactly as returned by check_availability (ISO 8601).",
            },
            "caller_name": {"type": "string"},
            "phone": {"type": "string", "description": "Caller's phone number, digits only."},
            "caller_confirmed": {"type": "boolean"},
        },
        "required": ["service", "start", "caller_name", "phone", "caller_confirmed"],
    },
    {
        "name": "reschedule_booking",
        "description": (
            "Move an existing booking to a new offered time. Look up by phone. Same two-step "
            "confirmation as create_booking."
        ),
        "properties": {
            "phone": {"type": "string"},
            "new_start": {
                "type": "string",
                "description": "ISO 8601 start from check_availability.",
            },
            "booking_id": {
                "type": "string",
                "description": "Only needed if the caller has more than one booking.",
            },
            "caller_confirmed": {"type": "boolean"},
        },
        "required": ["phone", "new_start", "caller_confirmed"],
    },
    {
        "name": "cancel_booking",
        "description": "Cancel an existing booking, looked up by phone. Two-step confirmation.",
        "properties": {
            "phone": {"type": "string"},
            "booking_id": {"type": "string"},
            "caller_confirmed": {"type": "boolean"},
        },
        "required": ["phone", "caller_confirmed"],
    },
    {
        "name": "lookup_business_info",
        "description": (
            "Look up anything about the business not already in your facts (parking, products, "
            "walk-ins, warranty...). If found=false, do not guess."
        ),
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
    {
        "name": "take_message",
        "description": "Leave a message for the team.",
        "properties": {
            "message": {"type": "string"},
            "caller_name": {"type": "string"},
            "phone": {"type": "string"},
        },
        "required": ["message"],
    },
    {
        "name": "transfer_to_human",
        "description": "Ask a team member to call the caller back.",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
    {
        "name": "end_call",
        "description": "Hang up after you have said goodbye.",
        "properties": {"reason": {"type": "string"}},
        "required": [],
    },
]

TOOL_NAMES = tuple(t["name"] for t in TOOL_DEFINITIONS)


# ---------------------------------------------------------------- handlers


class ReceptionistTools:
    def __init__(
        self,
        tenant: TenantConfig,
        state: CallState,
        backend: BookingBackend,
        *,
        call_id: str,
        clock: Clock | None = None,
        on_tool_call: ToolCallHook | None = None,
    ) -> None:
        self.tenant = tenant
        self.state = state
        self.backend = backend
        self.call_id = call_id
        self._clock = clock or tenant.now
        self._on_tool_call = on_tool_call
        self.handlers: dict[str, Handler] = {
            "check_availability": self.check_availability,
            "create_booking": self.create_booking,
            "reschedule_booking": self.reschedule_booking,
            "cancel_booking": self.cancel_booking,
            "lookup_business_info": self.lookup_business_info,
            "take_message": self.take_message,
            "transfer_to_human": self.transfer_to_human,
            "end_call": self.end_call,
        }

    # -- helpers

    def _service(self, name: str | None) -> Service | None:
        return self.tenant.find_service(name) if name else None

    def _unknown_service(self, name: str | None) -> ToolResult:
        return _err(
            f"unknown service {name!r}",
            available_services=[s.name for s in self.tenant.services],
        )

    def _parse_start(self, raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tenant.tz)
        return dt.astimezone(self.tenant.tz)

    def _phone(self, raw: str | None) -> str | None:
        return normalize_pk_phone(raw) if raw else None

    def _wrong_weekday(self, start: datetime) -> ToolResult | None:
        """Refuse a start on a weekday nobody mentioned (seen live: the caller asked for
        Thursday and the model passed Monday's date). Days the agent offered count too, so an
        alternative the caller accepted with a plain "yes" still goes through."""
        days = self.state.days_in_play
        if days is None or start.weekday() in days:
            return None
        asked = ", ".join(WEEKDAY_NAMES[d] for d in sorted(days))
        return _err(
            f"{start:%Y-%m-%d} is a {WEEKDAY_NAMES[start.weekday()]}, but the caller asked for "
            f"{asked}. Use the date of the day they asked for (check_availability again if "
            "needed), or ask them which day they mean."
        )

    def _spoken_phone(self, phone: str) -> str:
        return spoken_digits_ur(phone) if self.state.language == "ur" else spoken_digits_en(phone)

    def _say(self, step: str) -> str:
        """A read-back instruction, in the caller's language. The read-back details are
        English text; without this, models copied them out in English to Urdu callers."""
        if self.state.language == "ur":
            return (
                f"{step} The caller speaks Urdu: say it all in Urdu script, with the day, time "
                "and name in Urdu, and the phone number exactly as read_back gives it, word "
                "for word."
            )
        return f"{step} Say the phone number exactly as read_back gives it, word for word."

    async def _search(self, service: Service, day: date, preferred: time | None) -> Availability:
        return await self.backend.availability(
            self.tenant, service.name, day, preferred=preferred, call_id=self.call_id
        )

    async def _alternatives(self, service: Service, near: datetime) -> list[dict[str, str]]:
        found = await self._search(service, near.date(), near.time())
        return self._offer([o for o in found.options if o != near])

    def _offer(self, slots: list[datetime]) -> list[dict[str, str]]:
        self.state.record_offer(slots)
        return [{"start": s.isoformat(), "spoken": spoken_slot(s)} for s in slots]

    def _describe(self, booking: Booking) -> dict[str, str]:
        return {
            "booking_id": booking.id,
            "service": booking.service,
            "when": spoken_slot(booking.start.astimezone(self.tenant.tz)),
            "start": booking.start.isoformat(),
            "name": booking.caller_name,
        }

    async def _find_booking(self, phone: str, booking_id: str | None) -> Booking | ToolResult:
        bookings = await self.backend.find_bookings(self.tenant, phone)
        if booking_id:
            bookings = [b for b in bookings if b.id == booking_id.upper()]
        if not bookings:
            return _err(
                "no upcoming booking found for that number",
                hint="Ask the caller to check the number, or offer to take a message.",
            )
        if len(bookings) > 1:
            return _err(
                "more than one booking for that number",
                bookings=[self._describe(b) for b in bookings],
                hint="Ask which one, then call again with booking_id.",
            )
        return bookings[0]

    # -- tools

    async def check_availability(self, args: Mapping[str, Any]) -> ToolResult:
        service = self._service(_str(args, "service"))
        if service is None:
            return self._unknown_service(_str(args, "service"))
        try:
            day = date.fromisoformat(_str(args, "date") or "")
        except ValueError:
            return _err("date must be YYYY-MM-DD")
        preferred: time | None = None
        if pt := _str(args, "preferred_time"):
            try:
                preferred = time.fromisoformat(pt)
            except ValueError:
                return _err("preferred_time must be HH:MM")

        if self.state.intent not in (Intent.RESCHEDULE,):
            self.state.set_intent(Intent.BOOK)
        self.state.update(service=service.name, preferred_date=day.isoformat())

        found = await self._search(service, day, preferred)
        if found.options and found.requested_day_available:
            result: ToolResult = {
                "ok": True,
                "available": True,
                "date": found.day.isoformat(),
                "options": self._offer(found.options),
            }
            exact = [o for o in found.options if preferred is not None and o.time() == preferred]
            if exact:
                # Seen live: with a list, models asked "which time?" even when the caller had
                # already named one. Say plainly that the asked-for time is free.
                result["requested_time_free"] = True
                result["next_step"] = (
                    f"The time the caller asked for, {spoken_slot(exact[0])}, is free: say so "
                    "and move on to their name and number. Mention other times only if asked."
                )
            return result
        if found.options:
            # Nothing that day: the backend looked ahead so the caller gets real alternatives.
            return {
                "ok": True,
                "available": False,
                "note": f"Nothing free on {day:%A %d %B}. The next free day is shown.",
                "date": found.day.isoformat(),
                "options": self._offer(found.options),
            }
        return {
            "ok": True,
            "available": False,
            "options": [],
            "note": "Nothing free in the next week. Offer to take a message.",
        }

    async def create_booking(self, args: Mapping[str, Any]) -> ToolResult:
        service = self._service(_str(args, "service"))
        if service is None:
            return self._unknown_service(_str(args, "service"))
        start = self._parse_start(_str(args, "start"))
        if start is None:
            return _err("start must be an ISO time returned by check_availability")
        name = _str(args, "caller_name")
        if not name:
            return _err("ask the caller for their name")
        phone = self._phone(_str(args, "phone"))
        if phone is None:
            return _err("that phone number looks incomplete; ask the caller to repeat it")

        self.state.set_intent(Intent.BOOK)
        self.state.update(service=service.name, start=start, caller_name=name, phone=phone)
        if start not in self.state.offered_slots:
            return _err("that time was not offered; call check_availability and offer its times")
        if not _bool(args, "caller_confirmed") and (wrong := self._wrong_weekday(start)):
            self.state.clear_pending()
            return wrong

        details = (service.name, start.isoformat(), name, phone)
        readback = {
            "service": service.name,
            "when": spoken_slot(start),
            "name": name,
            "phone": self._spoken_phone(phone),
        }
        if not _bool(args, "caller_confirmed"):
            # Hold the slot while we read it back, so nobody takes it during the pause.
            if not await self.backend.hold(
                self.tenant, service=service.name, start=start, call_id=self.call_id
            ):
                self.state.clear_pending()
                return _err(
                    "another caller is booking that time right now",
                    options=await self._alternatives(service, start),
                    hint="Apologise and offer these instead.",
                )
            self.state.prepare("book", details)
            return {
                "ok": True,
                "booked": False,
                "read_back": readback,
                "next_step": self._say("Read these details back and ask the caller to confirm."),
            }
        try:
            self.state.require_prepared("book", details)
            self.state.confirm()
        except StateError as exc:
            self.state.prepare("book", details)
            return _err(str(exc), read_back=readback)

        try:
            booking = await self.backend.create_booking(
                self.tenant,
                service=service.name,
                start=start,
                caller_name=name,
                phone=phone,
                call_id=self.call_id,
                idempotency_key=idempotency_key(self.call_id, *details),
            )
        except SlotTakenError:
            self.state.clear_pending()
            self.state.confirmed = False
            return _err(
                "that slot was just taken by someone else",
                options=await self._alternatives(service, start),
                hint="Apologise and offer these instead.",
            )
        self.state.mark_booked(booking.id)
        self.state.clear_pending()
        logger.info("booking created", booking_id=booking.id, service=service.name)
        return {"ok": True, "booked": True, **self._describe(booking)}

    async def reschedule_booking(self, args: Mapping[str, Any]) -> ToolResult:
        phone = self._phone(_str(args, "phone"))
        if phone is None:
            return _err("that phone number looks incomplete; ask the caller to repeat it")
        new_start = self._parse_start(_str(args, "new_start"))
        if new_start is None:
            return _err("new_start must be an ISO time returned by check_availability")
        self.state.set_intent(Intent.RESCHEDULE)
        self.state.update(phone=phone)

        found = await self._find_booking(phone, _str(args, "booking_id"))
        if isinstance(found, dict):
            return found
        if new_start not in self.state.offered_slots:
            return _err(
                "that time was not offered; call check_availability for the booking's service",
                service=found.service,
            )
        if not _bool(args, "caller_confirmed") and (wrong := self._wrong_weekday(new_start)):
            return wrong
        details = (found.id, new_start.isoformat())
        readback = {
            "service": found.service,
            "from": spoken_slot(found.start.astimezone(self.tenant.tz)),
            "to": spoken_slot(new_start),
            "name": found.caller_name,
        }
        if not _bool(args, "caller_confirmed"):
            if not await self.backend.hold(
                self.tenant, service=found.service, start=new_start, call_id=self.call_id
            ):
                return _err("another caller is booking that time right now; check again")
            self.state.prepare("reschedule", details)
            return {
                "ok": True,
                "rescheduled": False,
                "read_back": readback,
                "next_step": self._say("Read the change back and ask the caller to confirm."),
            }
        try:
            self.state.require_prepared("reschedule", details)
        except StateError as exc:
            self.state.prepare("reschedule", details)
            return _err(str(exc), read_back=readback)
        try:
            moved = await self.backend.reschedule_booking(
                self.tenant,
                found.id,
                start=new_start,
                call_id=self.call_id,
                idempotency_key=idempotency_key(self.call_id, "reschedule", *details),
            )
        except SlotTakenError:
            self.state.clear_pending()
            return _err("that slot was just taken; check availability again")
        except BookingNotFoundError:
            return _err("booking no longer exists")
        self.state.clear_pending()
        return {"ok": True, "rescheduled": True, **self._describe(moved)}

    async def cancel_booking(self, args: Mapping[str, Any]) -> ToolResult:
        phone = self._phone(_str(args, "phone"))
        if phone is None:
            return _err("that phone number looks incomplete; ask the caller to repeat it")
        self.state.set_intent(Intent.CANCEL)
        self.state.update(phone=phone)
        found = await self._find_booking(phone, _str(args, "booking_id"))
        if isinstance(found, dict):
            return found
        details = (found.id,)
        readback = self._describe(found)
        if not _bool(args, "caller_confirmed"):
            self.state.prepare("cancel", details)
            return {
                "ok": True,
                "cancelled": False,
                "read_back": readback,
                "policy": self.tenant.policies.cancellation,
                "next_step": self._say("Read the booking back and ask the caller to confirm."),
            }
        try:
            self.state.require_prepared("cancel", details)
        except StateError as exc:
            self.state.prepare("cancel", details)
            return _err(str(exc), read_back=readback)
        try:
            cancelled = await self.backend.cancel_booking(
                self.tenant, found.id, call_id=self.call_id
            )
        except BookingNotFoundError:
            return _err("booking no longer exists")
        self.state.clear_pending()
        return {"ok": True, "cancelled": True, **self._describe(cancelled)}

    async def lookup_business_info(self, args: Mapping[str, Any]) -> ToolResult:
        question = _str(args, "question") or ""
        if self.state.intent == Intent.UNKNOWN:
            self.state.set_intent(Intent.QUESTION)
        found = await self.backend.lookup(
            self.tenant, question, language=self.state.language or "en"
        )
        # "ok" = the lookup ran (found or not). Without it, call records and traces marked
        # every lookup as a failed tool call.
        return {"ok": True, **found.as_tool_result()}

    async def take_message(self, args: Mapping[str, Any]) -> ToolResult:
        message = _str(args, "message")
        if not message:
            return _err("ask the caller what message to leave")
        name = _str(args, "caller_name") or self.state.caller_name
        phone = self._phone(_str(args, "phone")) or self.state.phone
        self.state.set_intent(Intent.MESSAGE)
        self.state.update(caller_name=name, phone=phone)
        message_id = await self.backend.take_message(
            self.tenant, caller_name=name, phone=phone, message=message, call_id=self.call_id
        )
        result: ToolResult = {"ok": True, "message_id": message_id}
        if phone is None:
            result["hint"] = "Ask for a phone number so the team can call back."
        return result

    async def transfer_to_human(self, args: Mapping[str, Any]) -> ToolResult:
        self.state.set_intent(Intent.HANDOFF)
        handoff_id = await self.backend.request_handoff(
            self.tenant,
            reason=_str(args, "reason") or "caller asked for a person",
            caller_name=self.state.caller_name,
            phone=self.state.phone,
            call_id=self.call_id,
        )
        result: ToolResult = {
            "ok": True,
            "handoff_id": handoff_id,
            "say": f"{self.tenant.handoff.name.split(' (')[0]} from the team will call you back.",
        }
        if self.state.phone is None:
            result["hint"] = "Ask for the caller's phone number for the call back."
        return result

    async def end_call(self, args: Mapping[str, Any]) -> ToolResult:
        self.state.ended = True
        return {"ok": True, "ended": True}

    async def run(
        self,
        name: str,
        args: Mapping[str, Any],
        *,
        caller_turns: int | None = None,
        caller_language: str | None = None,
        days_in_play: frozenset[int] | Literal["unknown"] | None = "unknown",
    ) -> ToolResult:
        """Dispatch by name, with logging. Unknown tools come back as errors, never crash.

        `caller_turns` is how many caller messages the LLM has heard (from its context); it
        gates the confirm step of every write (see `CallState.require_prepared`).
        """
        if caller_turns is not None:
            self.state.heard_caller(caller_turns)
        if caller_language is not None:
            self.state.language = caller_language
        if days_in_play != "unknown":
            self.state.days_in_play = days_in_play
        handler = self.handlers.get(name)
        if handler is None:
            return _err(f"unknown tool {name!r}")
        started = time_module.perf_counter()
        with tool_span(name, self.call_id) as span:
            try:
                result = await handler(args)
            except Exception as exc:  # a tool bug or a down api must not kill the call
                logger.exception("tool failed", tool=name)
                result = _err(
                    f"{type(exc).__name__}: the system had a problem; offer to take a message"
                )
            duration_ms = int((time_module.perf_counter() - started) * 1000)
            span.set_attribute("tool.ok", bool(result.get("ok")))
            span.set_attribute("tool.duration_ms", duration_ms)
        if self._on_tool_call is not None:
            self._on_tool_call(name, dict(args), result, bool(result.get("ok")), duration_ms)
        # Log the outcome without caller name or phone number (no PII in logs).
        logger.info(
            "tool call",
            tool=name,
            ok=result.get("ok"),
            intent=self.state.intent.value,
            confirmed=self.state.confirmed,
            booking_id=self.state.booking_id,
        )
        return result


# ---------------------------------------------------------------- Pipecat glue


def tools_schema() -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name=d["name"],
                description=d["description"],
                properties=d["properties"],
                required=d["required"],
            )
            for d in TOOL_DEFINITIONS
        ]
    )


def _caller_messages(context: LLMContext) -> list[str]:
    texts: list[str] = []
    for m in context.get_messages():
        if isinstance(m, dict) and m.get("role") == "user":
            content = m.get("content")
            texts.append(content if isinstance(content, str) else "")
    return texts


WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# English names, and Urdu ones that can't mean anything else ("ہفتہ" is also "week", so
# Saturday in Urdu script isn't matched; a missed match only means no check, never a refusal).
_WEEKDAY_PATTERNS = [
    re.compile(rf"\b{name}\b|{'|'.join(urdu)}" if urdu else rf"\b{name}\b", re.IGNORECASE)
    for name, urdu in zip(
        WEEKDAY_NAMES,
        (("پیر", "سوموار"), ("منگل",), ("بدھ",), ("جمعرات",), ("جمعہ", "جمعے"), (), ("اتوار",)),
        strict=True,
    )
]


def weekdays_named(text: str) -> set[int]:
    return {day for day, pattern in enumerate(_WEEKDAY_PATTERNS) if pattern.search(text)}


def days_in_play(context: LLMContext | None) -> frozenset[int] | None:
    """Weekdays in the caller's latest message that names any, plus those the agent has
    mentioned since (offers the caller can accept with a plain yes). None = no weekday named."""
    if context is None:
        return None
    messages = [
        m
        for m in context.get_messages()
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    ]
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        days = weekdays_named(str(messages[i]["content"]))
        if days:
            for later in messages[i + 1 :]:
                if later.get("role") == "assistant":
                    days |= weekdays_named(str(later["content"]))
            return frozenset(days)
    return None


def count_caller_turns(context: LLMContext | None) -> int | None:
    """Caller messages in the LLM context: the source of truth for "has the caller replied"."""
    return None if context is None else len(_caller_messages(context))


def last_caller_language(context: LLMContext | None) -> str | None:
    """Language of the caller's latest message ("en"/"ur"), or None if it can't be told."""
    if context is None:
        return None
    for text in reversed(_caller_messages(context)):
        language = caller_language(text)
        if language is not None:
            return language
    return None


class SpokeObserver(BaseObserver):
    """Keeps `CallState.agent_spoke`: has the agent said anything since the caller last spoke?

    An observer sees each frame when it is pushed. The LLM pushes its text before it runs any
    tool, so by the time `end_call` runs this is up to date. (The LLM context isn't: it gets
    the reply's text only once the reply has finished.)
    """

    def __init__(self, state: CallState) -> None:
        super().__init__()
        self._state = state

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        if isinstance(frame, UserStoppedSpeakingFrame | LLMMessagesAppendFrame):
            self._state.agent_spoke = False  # a new caller turn (voice | text mode)
        elif isinstance(frame, LLMTextFrame | TTSSpeakFrame) and frame.text.strip():
            self._state.agent_spoke = True


def goodbye_line(tenant: TenantConfig, language: str | None) -> str:
    if language == "ur":
        name = tenant.business_name_ur or tenant.business_name
        return f"{name} میں فون کرنے کا شکریہ۔ خدا حافظ!"
    return f"Thank you for calling {tenant.business_name}. Goodbye!"


def register_tools(llm: LLMService[Any], tools: ReceptionistTools) -> None:
    """Route every tool call from `llm` to `tools`. `end_call` also ends the pipeline."""

    def make_handler(name: str) -> Callable[[FunctionCallParams], Awaitable[None]]:
        async def handler(params: FunctionCallParams) -> None:
            result = await tools.run(
                name,
                params.arguments,
                caller_turns=count_caller_turns(params.context),
                caller_language=last_caller_language(params.context),
                days_in_play=days_in_play(params.context),
            )
            if name == "end_call" and result.get("ended"):
                # Don't ask the LLM for more, just hang up once queued audio has played
                # (EndWorkerFrame flushes frames ahead of it). Never hang up in silence: seen
                # live, a model ended the call on "Great, thank you" without a word.
                await params.result_callback(
                    result, properties=FunctionCallResultProperties(run_llm=False)
                )
                if not tools.state.agent_spoke:
                    goodbye = goodbye_line(tools.tenant, tools.state.language)
                    await params.llm.push_frame(TTSSpeakFrame(goodbye))
                await params.llm.push_frame(EndWorkerFrame(reason="end_call"))
                return
            await params.result_callback(result)

        return handler

    for name in TOOL_NAMES:
        llm.register_function(name, make_handler(name))
