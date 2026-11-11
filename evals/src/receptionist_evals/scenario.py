"""Eval scenarios: who calls, what they want, what they know, and what must happen.

One YAML file per scenario in evals/scenarios/. Each fixes the clock (`now`), so "next
Saturday" means the same day on every run, and can seed existing bookings (for reschedule,
cancel and slot-full cases). `expect` says what a correct call ends with; the scorers check it
against the backend's state and the transcript, never against what the agent claims.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from receptionist_shared.outcome import Outcome


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SeedBooking(Strict):
    service: str
    start: datetime
    caller_name: str
    phone: str
    code: str | None = None  # fixed booking code, so the caller can quote it


class ExpectedBooking(Strict):
    service: str
    start: datetime | None = None  # exact slot, or...
    day: date | None = None  # ...any slot that day (caller takes what is offered)
    caller_name: str | None = None  # None = not checked; compared case-insensitively
    phone: str | None = None  # E.164, e.g. +923001234567

    @model_validator(mode="after")
    def _when(self) -> ExpectedBooking:
        if (self.start is None) == (self.day is None):
            raise ValueError("give exactly one of start or day")
        return self


class Expect(Strict):
    outcome: Outcome | list[Outcome]  # a list = any of these is right
    booking: ExpectedBooking | None = None  # the caller's confirmed booking at the end
    no_booking_at: list[datetime] = Field(default_factory=list)  # slots that must stay free
    tools_called: list[str] = Field(default_factory=list)  # each must appear at least once
    tools_not_called: list[str] = Field(default_factory=list)
    # Regexes, case-insensitive, checked against everything the agent said.
    must_mention: list[str] = Field(default_factory=list)
    must_not_mention: list[str] = Field(default_factory=list)
    max_turns: int = 14

    @property
    def outcomes(self) -> list[Outcome]:
        return self.outcome if isinstance(self.outcome, list) else [self.outcome]


class Scenario(Strict):
    id: str
    title: str
    tenant: str
    language: Literal["en", "ur", "mixed"] = "en"
    tags: list[str] = Field(default_factory=list)
    now: datetime  # the call happens at this moment (tenant timezone in the offset)
    persona: str  # how the caller behaves
    goal: str  # what the caller wants
    facts: dict[str, str] = Field(default_factory=dict)  # what the caller knows if asked
    seed_bookings: list[SeedBooking] = Field(default_factory=list)
    expect: Expect

    @field_validator("now")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("now needs a UTC offset, e.g. 2026-11-16T10:00:00+05:00")
        return v


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(data)
    if scenario.id != path.stem:
        raise ValueError(f"{path.name}: id {scenario.id!r} must match the file name")
    return scenario


def load_scenarios(directory: Path, *, tags: set[str] | None = None) -> list[Scenario]:
    """All scenarios (sorted by id), optionally only those with any of `tags`."""
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]
    if tags:
        scenarios = [s for s in scenarios if tags & set(s.tags)]
    return scenarios
