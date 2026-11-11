"""Drafting a setup from a website: safe fetching, and nothing the page doesn't say survives."""

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from receptionist_api.website_draft import (
    MAX_BYTES,
    DraftError,
    DraftFaq,
    DraftService,
    LlmDrafter,
    LlmEndpoint,
    WebsiteDraft,
    check_public_url,
    draft_from_website,
    fetch_page,
    ground,
    page_text,
)

PAGE_HTML = """<html><head><title>Noor Beauty Salon | Lahore</title>
<style>.x{color:red}</style><script>var price = 9999;</script></head>
<body><h1>Noor Beauty Salon</h1>
<p>Visit us at 12-B Main Boulevard, Gulberg III, Lahore.</p>
<h2>Services</h2>
<ul><li>Haircut - Rs. 2,500 (45 minutes)</li><li>Blow dry - Rs. 1,500</li>
<li>Bridal makeup - price on request</li></ul>
<h2>FAQ</h2>
<p>Do you take walk-ins? Yes, walk-ins are welcome on weekdays before 5 pm.</p>
<p>Open Monday to Sunday, 11 am to 8 pm. Closed on Tuesdays.</p>
</body></html>"""

TEXT = page_text(PAGE_HTML)


async def public(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


# ---------------------------------------------------------------- page text


def test_page_text_keeps_visible_text_only() -> None:
    assert TEXT.splitlines()[0] == "Noor Beauty Salon | Lahore"
    assert "Haircut - Rs. 2,500 (45 minutes)" in TEXT
    assert "9999" not in TEXT and "color" not in TEXT  # script and style are dropped


# ---------------------------------------------------------------- URL safety


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.com", "bad_url"),
        ("file:///etc/passwd", "bad_url"),
        ("https://user:pw@example.com", "bad_url"),
        ("http://example.com:8080/", "bad_url"),
        ("not a url", "bad_url"),
    ],
)
async def test_rejects_unsafe_urls(url: str, code: str) -> None:
    with pytest.raises(DraftError) as err:
        await check_public_url(url, public)
    assert err.value.code == code


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "100.64.0.1"]
)
async def test_rejects_hosts_that_resolve_to_private_addresses(address: str) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14", address]  # one bad address is enough to refuse

    with pytest.raises(DraftError) as err:
        await check_public_url("https://looks-public.example", resolve)
    assert err.value.code == "private_address"


async def test_accepts_a_public_https_url() -> None:
    url = await check_public_url("https://example.com/about", public)
    assert url.host == "example.com"


def mock_site(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_redirects_are_rechecked_at_every_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.example/admin"})
        return httpx.Response(200, html="<p>secret admin page</p>")

    async def resolve(host: str, port: int) -> list[str]:
        return ["10.0.0.2"] if host == "internal.example" else ["93.184.215.14"]

    async with mock_site(handler) as client:
        with pytest.raises(DraftError) as err:
            await fetch_page("https://example.com", client, resolve=resolve)
    assert err.value.code == "private_address"


async def test_follows_a_safe_relative_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            return httpx.Response(301, headers={"location": "/home"})
        return httpx.Response(200, html="<p>home</p>")

    async with mock_site(handler) as client:
        assert "home" in await fetch_page("https://example.com/", client, resolve=public)


async def test_refuses_non_html_and_caps_the_body() -> None:
    async with mock_site(lambda r: httpx.Response(200, content=b"%PDF")) as client:
        with pytest.raises(DraftError) as err:
            await fetch_page("https://example.com/menu.pdf", client, resolve=public)
    assert err.value.code == "not_html"

    huge = b"<p>" + b"a" * (MAX_BYTES * 2)
    site = mock_site(
        lambda r: httpx.Response(200, content=huge, headers={"content-type": "text/html"})
    )
    async with site as client:
        body = await fetch_page("https://example.com", client, resolve=public)
    assert len(body) == MAX_BYTES


# ---------------------------------------------------------------- grounding


def test_grounding_keeps_what_the_page_says() -> None:
    draft = WebsiteDraft(
        business_name="Noor Beauty Salon",
        address="12-B Main Boulevard, Gulberg III, Lahore",
        opening_hours_text="Monday to Sunday, 11 am to 8 pm. Closed on Tuesdays.",
        services=[
            DraftService(
                name="Haircut",
                price=Decimal(2500),
                duration_min=45,
                evidence="Haircut - Rs. 2,500 (45 minutes)",
            ),
            DraftService(name="Bridal makeup", evidence="Bridal makeup - price on request"),
        ],
        faq=[
            DraftFaq(
                question="Do you take walk-ins?",
                answer="Yes, on weekdays before 5 pm.",
                evidence="Yes, walk-ins are welcome on weekdays before 5 pm.",
            )
        ],
    )
    out = ground(draft, TEXT)
    assert out.dropped == []
    assert out.services[0].price == 2500 and out.services[0].duration_min == 45
    assert out.services[1].price is None
    assert len(out.faq) == 1 and out.address and out.opening_hours_text


def test_grounding_drops_invented_facts() -> None:
    draft = WebsiteDraft(
        business_name="Noor Luxury Spa",  # not the page's name
        address="45 Mall Road, Lahore",
        services=[
            # a real service with an invented price and duration
            DraftService(
                name="Blow dry",
                price=Decimal(1800),
                duration_min=30,
                evidence="Blow dry - Rs. 1,800",
            ),
            # the price is on the page, but for another service
            DraftService(name="Haircut", price=Decimal(1500), evidence="Haircut - Rs. 2,500"),
            DraftService(name="Keratin treatment", price=Decimal(9000), evidence="Keratin"),
        ],
        faq=[
            DraftFaq(
                question="Is there parking?",
                answer="Yes, free parking.",
                evidence="Free parking for all customers",
            ),
            # evidence is real, but the answer adds a number the page doesn't have
            DraftFaq(
                question="Walk-ins?",
                answer="Yes, before 7 pm.",
                evidence="Yes, walk-ins are welcome on weekdays before 5 pm.",
            ),
        ],
    )
    out = ground(draft, TEXT)
    assert out.business_name is None and out.address is None
    assert [s.name for s in out.services] == ["Blow dry", "Haircut"]
    assert all(s.price is None for s in out.services)  # the owner fills these in
    assert out.services[0].duration_min is None
    assert out.faq == []
    assert len(out.dropped) == 8, out.dropped


def test_grounding_accepts_near_verbatim_quotes() -> None:
    faq = DraftFaq(
        question="Walk-ins?",
        answer="Yes, on weekdays before 5 pm.",
        evidence="walk-ins are welcome on weekdays before 5pm",  # "5pm": tokenised differently
    )
    assert ground(WebsiteDraft(faq=[faq]), TEXT).faq == [faq]


# ---------------------------------------------------------------- LLM


def llm_reply(content: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})


async def test_drafter_falls_back_to_the_next_llm_and_grounds_the_result() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "site.example":
            return httpx.Response(200, html=PAGE_HTML)
        body = json.loads(request.content)
        seen.append(body["model"])
        assert "Haircut - Rs. 2,500" in body["messages"][1]["content"]
        if body["model"] == "first":
            return httpx.Response(429, json={"error": "rate limited"})
        return llm_reply(
            {
                "business_name": "Noor Beauty Salon",
                "services": [
                    {"name": "Haircut", "price": 2500, "evidence": "Haircut - Rs. 2,500"},
                    {"name": "Blow dry", "price": 999, "evidence": "Blow dry"},
                ],
                "faq": [],
            }
        )

    async with mock_site(handler) as client:
        drafter = LlmDrafter(
            [
                LlmEndpoint("a", "https://llm-a.example/v1", "k", "first", {}),
                LlmEndpoint("b", "https://llm-b.example/v1", "k", "second", {}),
            ],
            client,
        )
        out = await draft_from_website(
            "https://site.example", client=client, drafter=drafter, resolve=public
        )
    assert seen == ["first", "second"]
    assert [(s.name, s.price) for s in out.services] == [("Haircut", 2500), ("Blow dry", None)]


async def test_drafter_reports_when_no_llm_answers() -> None:
    async with mock_site(lambda r: httpx.Response(200, text="not json")) as client:
        drafter = LlmDrafter([LlmEndpoint("a", "https://llm.example", "k", "m", {})], client)
        with pytest.raises(DraftError) as err:
            await drafter.extract("some page text")
    assert err.value.code == "llm_unavailable"
