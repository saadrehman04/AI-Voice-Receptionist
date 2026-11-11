from receptionist_agent.brain.knowledge import NOT_FOUND_INSTRUCTION, describe_hours, lookup
from receptionist_shared.tenant import TenantConfig


def test_faq_by_keyword(salon: TenantConfig) -> None:
    r = lookup(salon, "is there somewhere to park my car?")
    assert r.found
    assert "free parking" in r.answer
    assert r.sources == ["faq"]


def test_faq_answer_in_urdu_when_caller_speaks_urdu(salon: TenantConfig) -> None:
    r = lookup(salon, "کیا پارکنگ ہے؟", language="ur")
    assert r.found
    assert "پارکنگ" in r.answer


def test_service_price_comes_from_config(salon: TenantConfig) -> None:
    r = lookup(salon, "how much is a haircut")
    assert "2,500 PKR" in r.answer
    assert "services" in r.sources


def test_unknown_price_says_not_listed(salon: TenantConfig) -> None:
    r = lookup(salon, "what does bridal makeup cost")
    assert "price on request" in r.answer
    assert "do not quote" in r.answer


def test_hours_and_policy(salon: TenantConfig) -> None:
    r = lookup(salon, "what time do you open, and can I cancel?")
    assert "Tuesday: closed" in r.answer
    assert "4 hours" in r.answer
    assert set(r.sources) == {"opening_hours", "policies.cancellation"}


def test_unknown_question_is_not_found(salon: TenantConfig) -> None:
    r = lookup(salon, "do you sell gift vouchers")
    assert not r.found
    assert r.as_tool_result() == {"found": False, "instruction": NOT_FOUND_INSTRUCTION}


def test_workshop_faq(workshop: TenantConfig) -> None:
    r = lookup(workshop, "do you do pick and drop?")
    assert "1000 rupees" in r.answer


def test_describe_hours_lists_every_day(workshop: TenantConfig) -> None:
    text = describe_hours(workshop)
    assert text.startswith("Monday: 09:00-18:00")
    assert "Friday: 09:00-12:30, 14:30-18:00" in text
    assert text.endswith("Sunday: closed")
