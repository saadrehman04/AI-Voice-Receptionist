import pytest

from receptionist_shared.phone import (
    is_country,
    normalize_phone,
    normalize_pk_phone,
    spoken_digits,
    spoken_digits_en,
)


@pytest.mark.parametrize(
    "raw",
    [
        "0300-1234567",
        "0300 123 4567",
        "+92 300 1234567",
        "923001234567",
        "00923001234567",
        "(0300) 123-4567",
        "۰۳۰۰۱۲۳۴۵۶۷",  # Urdu digits from speech-to-text
        "3001234567",
    ],
)
def test_mobile_formats_normalise(raw: str) -> None:
    assert normalize_pk_phone(raw) == "+923001234567"


def test_landline() -> None:
    assert normalize_pk_phone("042-35761234") == "+924235761234"


@pytest.mark.parametrize(
    "raw", ["", "12345", "0300-12345", "+1 415 555 0100", "call me", "03001234567890"]
)
def test_invalid_numbers(raw: str) -> None:
    assert normalize_pk_phone(raw) is None


def test_spoken_digits() -> None:
    assert spoken_digits("+923001234567") == "0300 123 4567"


def test_urdu_read_back_is_one_word_per_digit() -> None:
    from receptionist_shared.phone import spoken_digits_ur

    # the number the live eval read back as "three hundred, fifty, fifty..."
    assert spoken_digits_ur("+923005550128") == ("صفر تین صفر صفر، پانچ پانچ پانچ، صفر ایک دو آٹھ")


def test_english_read_back_is_one_word_per_digit() -> None:
    from receptionist_shared.phone import spoken_digits_en

    # the number a live run read back with two digits missing
    assert (
        spoken_digits_en("+923215550101") == "zero three two one, five five five, zero one zero one"
    )


@pytest.mark.parametrize(
    ("raw", "country", "e164"),
    [
        ("(512) 555-0147", "US", "+15125550147"),
        ("512 555 0147", "us", "+15125550147"),
        ("020 7946 0018", "GB", "+442079460018"),
        ("+44 20 7946 0018", "PK", "+442079460018"),  # a caller from abroad, Pakistani business
        ("0300-1234567", "PK", "+923001234567"),  # Pakistan keeps its own tested rules
        ("+92 300 1234567", "US", "+923001234567"),
    ],
)
def test_numbers_are_read_in_the_business_country(raw: str, country: str, e164: str) -> None:
    assert normalize_phone(raw, country) == e164


@pytest.mark.parametrize(
    ("raw", "country"),
    [("555 01", "US"), ("5125550147123", "US"), ("", "GB"), ("0300 1234567", "US")],
)
def test_impossible_numbers_are_refused(raw: str, country: str) -> None:
    """The last: a Pakistani number without +92 can't be a US number; the agent asks again."""
    assert normalize_phone(raw, country) is None


def test_read_back_follows_the_numbers_country() -> None:
    assert spoken_digits("+15125550147") == "512 555 0147"
    assert spoken_digits("+442079460018") == "020 7946 0018"
    assert spoken_digits_en("+15125550147") == "five one two, five five five, zero one four seven"


def test_country_codes() -> None:
    assert is_country("US") and is_country("pk") and not is_country("XX")
