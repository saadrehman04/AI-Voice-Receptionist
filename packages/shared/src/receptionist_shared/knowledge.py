"""Answer business questions strictly from the tenant config.

`lookup` never makes anything up: it either returns text copied from the YAML (hours, prices,
policies, FAQ answers) or `found=False`, and the prompt tells the LLM what to say then.
Phase 3 adds a per-tenant markdown knowledge base with embedding search behind the same call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from receptionist_shared.tenant import FaqEntry, Service, TenantConfig, Weekday

NOT_FOUND_INSTRUCTION = (
    "This is not in the business information. Do not guess. Tell the caller you will check "
    "with the team, and offer to take a message."
)

# Whole words or phrases only ("where" must not match "somewhere"); list variants explicitly.
_HOURS_WORDS = (
    "hour",
    "hours",
    "open",
    "opening",
    "close",
    "closing",
    "timing",
    "timings",
    "what time",
    "when are you",
    "وقت",
    "کھلتے",
    "کھلا",
    "بند",
    "ٹائم",
    "ٹائمنگ",
)
_ADDRESS_WORDS = (
    "address",
    "where",
    "location",
    "located",
    "directions",
    "پتہ",
    "کہاں",
    "ایڈریس",
)
_PRICE_WORDS = (
    "price",
    "prices",
    "cost",
    "costs",
    "how much",
    "charge",
    "charges",
    "fee",
    "fees",
    "rate",
    "rates",
    "قیمت",
    "کتنے",
    "ریٹ",
    "پیسے",
)
_SERVICE_LIST_WORDS = ("services", "what do you do", "what do you offer", "menu", "سروسز")
_POLICY_WORDS = {
    "cancellation": ("cancel", "cancelling", "cancellation", "کینسل", "منسوخ"),
    "late_arrival": ("late", "delay", "delayed", "دیر", "لیٹ"),
    "payment": (
        "pay",
        "paying",
        "payment",
        "card",
        "cash",
        "jazzcash",
        "easypaisa",
        "ادائیگی",
        "کارڈ",
        "کیش",
    ),
}
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "do",
        "does",
        "you",
        "your",
        "i",
        "we",
        "can",
        "to",
        "of",
        "for",
        "in",
        "on",
        "at",
        "it",
        "there",
        "any",
        "my",
        "me",
        "what",
        "how",
    ]
)


@dataclass(frozen=True)
class KnowledgeResult:
    found: bool
    answer: str
    sources: list[str] = field(default_factory=list)

    def as_tool_result(self) -> dict[str, object]:
        if not self.found:
            return {"found": False, "instruction": NOT_FOUND_INSTRUCTION}
        return {"found": True, "answer": self.answer, "sources": self.sources}


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1}


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _mentions(text: str, words: tuple[str, ...]) -> bool:
    return any(_has_phrase(text, w) for w in words)


def describe_hours(tenant: TenantConfig) -> str:
    parts = []
    for day in Weekday:
        ranges = tenant.opening_hours.get(day, [])
        label = ", ".join(r.label() for r in ranges) if ranges else "closed"
        parts.append(f"{day.value.capitalize()}: {label}")
    return "; ".join(parts)


def describe_service(service: Service, currency: str) -> str:
    text = f"{service.name}: about {service.duration_min} minutes"
    if service.price is None:
        text += ", price on request (no price is listed; do not quote one)"
    else:
        text += f", {service.price:,.0f} {currency}"
        if service.price_note:
            text += f" ({service.price_note})"
    if service.staff:
        text += f", with {', '.join(service.staff)}"
    return text


def _services_mentioned(tenant: TenantConfig, text: str) -> list[Service]:
    hits = []
    for s in tenant.services:
        names = [s.name.lower()] + ([s.name_ur] if s.name_ur else [])
        if any(n and _has_phrase(text, n) for n in names):
            hits.append(s)
    return hits


def _best_faq(tenant: TenantConfig, text: str) -> FaqEntry | None:
    query_tokens = _tokens(text)
    best: tuple[float, FaqEntry] | None = None
    for entry in tenant.faq:
        score = 0.0
        score += 2.0 * sum(1 for k in entry.keywords if _has_phrase(text, k.lower()))
        score += len(query_tokens & _tokens(entry.question))
        if best is None or score > best[0]:
            best = (score, entry)
    if best is None or best[0] < 2.0:
        return None
    return best[1]


def lookup(tenant: TenantConfig, query: str, *, language: str = "en") -> KnowledgeResult:
    """Collect every config fact the question touches. `found=False` if none apply."""
    text = query.lower().strip()
    answers: list[str] = []
    sources: list[str] = []

    services = _services_mentioned(tenant, text)
    if services:
        answers += [describe_service(s, tenant.currency) for s in services]
        sources.append("services")
    elif _mentions(text, _PRICE_WORDS) or _mentions(text, _SERVICE_LIST_WORDS):
        answers.append(
            "Services: " + "; ".join(describe_service(s, tenant.currency) for s in tenant.services)
        )
        sources.append("services")

    if _mentions(text, _HOURS_WORDS):
        answers.append(f"Opening hours ({tenant.timezone}): {describe_hours(tenant)}")
        sources.append("opening_hours")

    if _mentions(text, _ADDRESS_WORDS) and tenant.address:
        answers.append(f"Address: {tenant.address}")
        sources.append("address")

    for policy, words in _POLICY_WORDS.items():
        value = getattr(tenant.policies, policy)
        if value and _mentions(text, words):
            answers.append(f"{policy.replace('_', ' ').capitalize()} policy: {value}")
            sources.append(f"policies.{policy}")

    faq = _best_faq(tenant, text)
    if faq is not None:
        answer = faq.answer_ur if language == "ur" and faq.answer_ur else faq.answer
        answers.append(answer)
        sources.append("faq")

    if not answers:
        return KnowledgeResult(found=False, answer="")
    return KnowledgeResult(found=True, answer=" ".join(answers), sources=sources)
