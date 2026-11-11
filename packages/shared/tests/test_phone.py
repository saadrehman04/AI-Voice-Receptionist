import pytest

from receptionist_shared.phone import normalize_pk_phone, spoken_digits


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
