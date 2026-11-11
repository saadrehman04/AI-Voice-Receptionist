"""Tenant (business) configuration: the only source of business facts the agent may state.

One YAML file per business in `tenants/<slug>.yaml`, validated by `TenantConfig`. If a fact
(price, hours, policy) is not in here or in the tenant's knowledge base, the agent must say it
will check rather than guess.
"""

from __future__ import annotations

import itertools
import re
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LanguageCode = Literal["en", "ur"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
HOURS_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


class Weekday(StrEnum):
    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @classmethod
    def of(cls, day: date) -> Weekday:
        return list(cls)[day.weekday()]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(_Strict):
    start: time
    end: time

    @model_validator(mode="after")
    def _start_before_end(self) -> TimeRange:
        if self.start >= self.end:
            raise ValueError(f"opening range {self.start}-{self.end} must start before it ends")
        return self

    @classmethod
    def parse(cls, text: str) -> TimeRange:
        m = HOURS_RE.match(text.strip())
        if not m:
            raise ValueError(f"opening hours must look like '09:00-17:00', got {text!r}")
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        return cls(start=time(h1, m1), end=time(h2, m2))

    def label(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


class Service(_Strict):
    name: str = Field(min_length=1)
    name_ur: str | None = None
    duration_min: Annotated[int, Field(gt=0, le=600)]
    price: Annotated[Decimal, Field(ge=0)] | None = None  # None = price on request
    price_note: str | None = None  # e.g. "from", "depends on length"
    staff: list[str] = Field(default_factory=list)


class Policies(_Strict):
    cancellation: str | None = None
    late_arrival: str | None = None
    payment: str | None = None
    other: list[str] = Field(default_factory=list)


class Greeting(_Strict):
    en: str
    ur: str | None = None


class HandoffContact(_Strict):
    name: str
    phone: str


class FaqEntry(_Strict):
    question: str = Field(min_length=3)
    answer: str = Field(min_length=1)
    answer_ur: str | None = None
    keywords: list[str] = Field(default_factory=list)


class TenantConfig(_Strict):
    slug: str
    business_name: str = Field(min_length=1)
    business_name_ur: str | None = None
    business_type: str
    languages: list[LanguageCode] = Field(min_length=1)
    default_language: LanguageCode = "en"
    timezone: str = "Asia/Karachi"
    currency: str = "PKR"
    address: str | None = None
    opening_hours: dict[Weekday, list[TimeRange]]
    services: list[Service] = Field(min_length=1)
    policies: Policies = Field(default_factory=Policies)
    greeting: Greeting
    handoff: HandoffContact
    faq: list[FaqEntry] = Field(default_factory=list)
    booking_buffer_min: Annotated[int, Field(ge=0, le=120)] = 0
    min_notice_min: Annotated[int, Field(ge=0)] = 60
    max_days_ahead: Annotated[int, Field(gt=0, le=365)] = 30
    calendar_id: str | None = None  # Google Calendar id (Phase 3)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("slug must be lowercase letters, digits and dashes")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @field_validator("opening_hours", mode="before")
    @classmethod
    def _parse_hours(cls, v: object) -> object:
        """Accept `monday: ["09:00-13:00", "14:00-18:00"]` or `sunday: closed`."""
        if not isinstance(v, dict):
            return v
        out: dict[str, list[object]] = {}
        for day, ranges in v.items():
            if ranges in (None, "closed", []):
                out[str(day).lower()] = []
                continue
            items = ranges if isinstance(ranges, list) else [ranges]
            out[str(day).lower()] = [TimeRange.parse(r) if isinstance(r, str) else r for r in items]
        return out

    @model_validator(mode="after")
    def _consistency(self) -> TenantConfig:
        if self.default_language not in self.languages:
            raise ValueError("default_language must be one of languages")
        if "ur" in self.languages and self.greeting.ur is None:
            raise ValueError("an Urdu-speaking tenant needs greeting.ur")
        names = [s.name.lower() for s in self.services]
        if len(names) != len(set(names)):
            raise ValueError("service names must be unique")
        for day, ranges in self.opening_hours.items():
            ordered = sorted(ranges, key=lambda r: r.start)
            for a, b in itertools.pairwise(ordered):
                if b.start < a.end:
                    raise ValueError(f"opening hours overlap on {day}")
        return self

    @cached_property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def hours_on(self, day: date) -> list[TimeRange]:
        return sorted(self.opening_hours.get(Weekday.of(day), []), key=lambda r: r.start)

    def find_service(self, name: str) -> Service | None:
        """Case-insensitive match on English or Urdu name, then on a unique prefix/substring."""
        needle = name.strip().lower()
        if not needle:
            return None
        for s in self.services:
            if needle in (s.name.lower(), (s.name_ur or "").lower()):
                return s
        hits = [s for s in self.services if needle in s.name.lower() or s.name.lower() in needle]
        return hits[0] if len(hits) == 1 else None


def load_tenant(path: Path) -> TenantConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    tenant = TenantConfig.model_validate(data)
    if tenant.slug != path.stem:
        raise ValueError(f"{path}: slug {tenant.slug!r} must match the file name")
    return tenant


def load_tenant_by_slug(slug: str, tenants_dir: Path) -> TenantConfig:
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid tenant slug {slug!r}")
    return load_tenant(tenants_dir / f"{slug}.yaml")


def list_tenants(tenants_dir: Path) -> list[TenantConfig]:
    return [load_tenant(p) for p in sorted(tenants_dir.glob("*.yaml"))]
