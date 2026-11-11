"""Draft a business's services and FAQ from its website, for the owner to approve (Phase 7).

Three steps, each one a guard against a different failure:

1. **Fetch safely** (`fetch_page`). The URL comes from a stranger filling in a sign-up form, so
   the server must not become a way into our own network (SSRF): only http(s) on the default
   ports, every address the host resolves to must be a public one, redirects are followed by
   hand and re-checked at every hop, and the body is capped at 1 MB.
2. **Extract** (`LlmDrafter`). An LLM reads the page text and returns JSON. It is told to copy
   an `evidence` quote from the page for everything it reports, and null for anything not
   stated.
3. **Ground** (`ground`). The LLM's word isn't trusted: a service, price, duration or FAQ answer
   survives only if its evidence and every number in it really are on the page. Whatever is
   dropped is listed in `dropped`, so the owner sees what to fill in by hand. This is the
   "never invent business facts" rule applied at onboarding: a made-up price never reaches the
   tenant config, because the owner only approves what the page backs up.

Known limit: the address check and the request are two DNS lookups, so a host that changes its
answer between them (DNS rebinding) could slip through. A production deployment would send
these fetches through an egress proxy that blocks private ranges.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Protocol

import httpx
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

MAX_BYTES = 1_000_000
MAX_TEXT_CHARS = 15_000
MAX_REDIRECTS = 3
MIN_TEXT_CHARS = 40
EVIDENCE_OVERLAP = 0.8  # share of an evidence quote's words that must be on the page

Resolver = Callable[[str, int], Awaitable[list[str]]]


class DraftError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------- fetch


async def resolve_host(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


async def check_public_url(url: str, resolve: Resolver | None = None) -> httpx.URL:
    """Raise `DraftError` unless `url` is http(s) on a default port and resolves only to
    public addresses."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise DraftError("bad_url", "that doesn't look like a web address") from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise DraftError("bad_url", "use an http:// or https:// address")
    if parsed.userinfo:
        raise DraftError("bad_url", "addresses with a user name or password aren't allowed")
    if parsed.port not in (None, 80, 443):
        raise DraftError("bad_url", "only the standard web ports are allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await (resolve or resolve_host)(parsed.host, port)
    except OSError as exc:
        raise DraftError("unreachable", f"couldn't find {parsed.host}") from exc
    if not addresses:
        raise DraftError("unreachable", f"couldn't find {parsed.host}")
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise DraftError("private_address", "that address isn't on the public internet")
    return parsed


async def fetch_page(
    url: str, client: httpx.AsyncClient, *, resolve: Resolver | None = None
) -> str:
    """The page's HTML (or text). Redirects are followed by hand so each hop is re-checked."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        target = await check_public_url(current, resolve)
        try:
            async with client.stream(
                "GET", target, follow_redirects=False, headers={"Accept": "text/html"}
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise DraftError("unreachable", "the site sent an empty redirect")
                    current = str(target.join(location))
                    continue
                if response.status_code >= 400:
                    raise DraftError("unreachable", f"the site answered {response.status_code}")
                kind = response.headers.get("content-type", "")
                if "html" not in kind and "text/plain" not in kind:
                    raise DraftError("not_html", "that address isn't a web page")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                return bytes(body[:MAX_BYTES]).decode(response.encoding or "utf-8", "replace")
        except httpx.HTTPError as exc:
            raise DraftError("unreachable", "couldn't load that page") from exc
    raise DraftError("unreachable", "too many redirects")


# ---------------------------------------------------------------- text

_SKIP = {"script", "style", "noscript", "svg", "template", "head"}
_BLOCK = {
    "p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "td", "th", "dt", "dd", "title",
}  # fmt: skip


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP and self._skip:
            self._skip -= 1
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.parts.append(data)


def page_text(html: str) -> str:
    """Visible text, one line per block, title first, capped for the LLM."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    title = " ".join("".join(parser.title).split())
    text = "\n".join([title, *(ln for ln in lines if ln)] if title else [ln for ln in lines if ln])
    return text[:MAX_TEXT_CHARS]


# ---------------------------------------------------------------- draft models


class DraftService(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price: Decimal | None = Field(default=None, ge=0)
    price_note: str | None = Field(default=None, max_length=80)
    duration_min: int | None = Field(default=None, gt=0, le=600)
    evidence: str = Field(default="", max_length=400)


class DraftFaq(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=1, max_length=800)
    evidence: str = Field(default="", max_length=600)


class WebsiteDraft(BaseModel):
    business_name: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=300)
    opening_hours_text: str | None = Field(default=None, max_length=400)
    services: list[DraftService] = Field(default_factory=list, max_length=40)
    faq: list[DraftFaq] = Field(default_factory=list, max_length=30)
    dropped: list[str] = Field(default_factory=list)  # what grounding removed, and why


# ---------------------------------------------------------------- grounding

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


def _numbers(text: str) -> set[str]:
    """Numbers with thousands separators removed: "Rs. 2,500" -> {"2500"}; "1.5" stays."""
    out = set()
    for raw in _NUMBER.findall(text):
        plain = raw.replace(",", "")
        out.add(plain[:-2] if plain.endswith(".00") else plain)
    return out


@dataclass(frozen=True)
class _Page:
    text: str
    words: frozenset[str]
    flat: str  # lowercased words joined by single spaces
    numbers: frozenset[str]

    @classmethod
    def of(cls, text: str) -> _Page:
        words = _words(text)
        return cls(text, frozenset(words), " ".join(words), frozenset(_numbers(text)))

    def has_phrase(self, phrase: str) -> bool:
        words = _words(phrase)
        return bool(words) and f" {' '.join(words)} " in f" {self.flat} "

    def backs(self, evidence: str) -> bool:
        """The quote is on the page: verbatim, or nearly (LLMs drop a word here and there).

        "Nearly" never covers numbers: every number in the quote must be on the page, so a
        quote with one price changed doesn't pass on the strength of its other words.
        """
        if self.has_phrase(evidence):
            return True
        if not self.has_numbers_of(evidence):
            return False
        words = _words(evidence)
        if len(words) < 3:
            return False
        return sum(w in self.words for w in words) / len(words) >= EVIDENCE_OVERLAP

    def has_numbers_of(self, text: str) -> bool:
        return _numbers(text) <= self.numbers


def _price_text(price: Decimal) -> str:
    return str(int(price)) if price == price.to_integral_value() else str(price)


def ground(draft: WebsiteDraft, text: str) -> WebsiteDraft:
    """Keep only what the page says. Everything removed is explained in `dropped`."""
    page = _Page.of(text)
    dropped: list[str] = []

    name = draft.business_name
    if name and not page.has_phrase(name):
        dropped.append(f"business name {name!r}: not on the page")
        name = None
    address = draft.address
    if address and not page.backs(address):
        dropped.append("address: not found on the page")
        address = None
    hours = draft.opening_hours_text
    if hours and not page.backs(hours):
        dropped.append("opening hours: not found on the page")
        hours = None

    services: list[DraftService] = []
    seen: set[str] = set()
    for s in draft.services:
        key = " ".join(_words(s.name))
        if not page.has_phrase(s.name) or key in seen:
            dropped.append(f"service {s.name!r}: not on the page")
            continue
        seen.add(key)
        evidence_ok = bool(s.evidence) and page.backs(s.evidence)
        price, note = s.price, s.price_note
        if price is not None:
            in_evidence = evidence_ok and _price_text(price) in _numbers(s.evidence)
            if not in_evidence:
                dropped.append(f"price for {s.name!r}: no matching price on the page")
                price, note = None, None
        duration = s.duration_min
        if duration is not None and not (evidence_ok and str(duration) in _numbers(s.evidence)):
            dropped.append(f"duration for {s.name!r}: not stated on the page")
            duration = None
        services.append(
            DraftService(
                name=s.name.strip(),
                price=price,
                price_note=note,
                duration_min=duration,
                evidence=s.evidence if evidence_ok else "",
            )
        )

    faq: list[DraftFaq] = []
    for f in draft.faq:
        if not (f.evidence and page.backs(f.evidence) and page.has_numbers_of(f.answer)):
            dropped.append(f"FAQ {f.question!r}: answer not found on the page")
            continue
        faq.append(f)

    return WebsiteDraft(
        business_name=name,
        address=address,
        opening_hours_text=hours,
        services=services,
        faq=faq,
        dropped=[*draft.dropped, *dropped],
    )


# ---------------------------------------------------------------- LLM extraction

SYSTEM = """You read the text of a small business's website and extract facts for its phone
receptionist. Return JSON with exactly these keys:
- "business_name": string or null
- "address": string or null
- "opening_hours_text": the opening hours copied from the page, or null
- "services": list of {"name", "price", "price_note", "duration_min", "evidence"}
- "faq": list of {"question", "answer", "evidence"}: questions a caller might ask that the
  page answers (parking, payment, walk-ins, policies...)
Rules:
- Only facts the page states. Never guess. Use null for a price or duration that isn't written.
- "price" is a plain number in the page's currency (2500, not "Rs. 2,500"); "price_note" is a
  word like "from" when the page says so.
- "evidence" is a short quote copied exactly from the page that backs the item.
- At most 25 services and 15 FAQ entries."""


class Drafter(Protocol):
    async def extract(self, page: str) -> WebsiteDraft: ...


@dataclass(frozen=True)
class LlmEndpoint:
    name: str
    url: str
    api_key: str
    model: str
    extra: dict[str, Any]


def groq_endpoint(api_key: str, model: str, reasoning_effort: str | None) -> LlmEndpoint:
    extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    return LlmEndpoint(
        "groq", "https://api.groq.com/openai/v1/chat/completions", api_key, model, extra
    )


def gemini_endpoint(api_key: str, model: str) -> LlmEndpoint:
    return LlmEndpoint(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        api_key,
        model,
        {"reasoning_effort": "low"},
    )


class LlmDrafter:
    """Tries each endpoint in order (Groq, then Gemini) until one returns valid JSON."""

    def __init__(self, endpoints: Sequence[LlmEndpoint], client: httpx.AsyncClient) -> None:
        self._endpoints = list(endpoints)
        self._client = client

    async def extract(self, page: str) -> WebsiteDraft:
        for endpoint in self._endpoints:
            body: dict[str, Any] = {
                "model": endpoint.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Website text:\n\n{page}"},
                ],
                **endpoint.extra,
            }
            try:
                response = await self._client.post(
                    endpoint.url,
                    json=body,
                    headers={"Authorization": f"Bearer {endpoint.api_key}"},
                    timeout=40.0,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return WebsiteDraft.model_validate(json.loads(content) | {"dropped": []})
            except (httpx.HTTPError, KeyError, IndexError, ValueError, ValidationError) as exc:
                logger.warning(
                    "website draft LLM failed", llm=endpoint.name, error=type(exc).__name__
                )
        raise DraftError("llm_unavailable", "couldn't read the page right now; try again later")


async def draft_from_website(
    url: str,
    *,
    client: httpx.AsyncClient,
    drafter: Drafter,
    resolve: Resolver | None = None,
) -> WebsiteDraft:
    text = page_text(await fetch_page(url, client, resolve=resolve))
    if len(text) < MIN_TEXT_CHARS:
        raise DraftError("empty_page", "the page has almost no text to read")
    return ground(await drafter.extract(text), text)


__all__ = [
    "DraftError",
    "DraftFaq",
    "DraftService",
    "Drafter",
    "LlmDrafter",
    "LlmEndpoint",
    "WebsiteDraft",
    "check_public_url",
    "draft_from_website",
    "fetch_page",
    "gemini_endpoint",
    "groq_endpoint",
    "ground",
    "page_text",
]
