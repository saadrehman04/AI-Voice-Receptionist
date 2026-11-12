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
