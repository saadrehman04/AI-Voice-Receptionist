"""Phone numbers -> E.164 (`+14155550101`, `+923001234567`).

Callers say numbers in many shapes ("0300-1234567", "+92 300 1234567", "(415) 555-0101"), and
Urdu speech-to-text may return Urdu or Arabic-Indic digits (۰۳۰۰...). We normalise all of them
so a booking is stored and matched under one canonical number.

A number without a country code is read in the business's country (`TenantConfig.country`).
Pakistan keeps its own hand-written rules (tested against real calls); every other country
uses `phonenumbers`, the Python port of Google's libphonenumber (D43).
"""

from __future__ import annotations

import re

import phonenumbers

_DIGIT_MAP = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ALLOWED = re.compile(r"^\+?[0-9]+$")


def normalize_pk_phone(raw: str) -> str | None:
    """Return `+92` followed by 9-10 national digits, or None if it can't be a PK number."""
    text = raw.translate(_DIGIT_MAP)
    text = re.sub(r"[\s\-().]", "", text)
    if not text or not _ALLOWED.match(text):
        return None
    if text.startswith("+"):
        text = text[1:]
        if not text.startswith("92"):
            return None
        national = text[2:]
    elif text.startswith("0092"):
        national = text[4:]
    elif text.startswith("92") and len(text) in (11, 12):
        national = text[2:]
    elif text.startswith("0"):
        national = text[1:]
    else:
        national = text
    national = national.lstrip("0") if national.startswith("0") else national
    if not 9 <= len(national) <= 10:
        return None
    # Mobile numbers are 3XX + 7 digits = 10 digits.
    if national.startswith("3") and len(national) != 10:
        return None
    return "+92" + national


def normalize_phone(raw: str, country: str = "PK") -> str | None:
    """E.164 for `raw`, read in `country` (ISO 3166 code) unless it carries its own country
    code; None if it can't be a phone number there (wrong length: a misheard digit, usually)."""
    text = re.sub(r"[\s\-().]", "", raw.translate(_DIGIT_MAP))
    if not text or not _ALLOWED.match(text):
        return None
    if text.startswith("+92") or (country.upper() == "PK" and not text.startswith("+")):
        return normalize_pk_phone(text)
    try:
        number = phonenumbers.parse(text, country.upper())
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def is_country(code: str) -> bool:
    """True for an ISO 3166 country code that has phone numbers ("US", "GB", "PK")."""
    return code.upper() in phonenumbers.SUPPORTED_REGIONS


def spoken_digits(e164: str) -> str:
    """'+923001234567' -> '0300 123 4567', '+14155550101' -> '415 555 0101': the number as the
    caller would say it in their country, in groups, the way a receptionist reads it back."""
    if e164.startswith("+92"):
        national = "0" + e164.removeprefix("+92")
        if len(national) == 11:
            return f"{national[:4]} {national[4:7]} {national[7:]}"
        return national
    try:
        number = phonenumbers.parse(e164)
    except phonenumbers.NumberParseException:
        return e164.lstrip("+")
    national = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL)
    return " ".join(re.findall(r"\d+", national)) or e164.lstrip("+")


ENGLISH_DIGITS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
URDU_DIGITS = ("صفر", "ایک", "دو", "تین", "چار", "پانچ", "چھ", "سات", "آٹھ", "نو")


def spoken_digits_en(e164: str) -> str:
    """'+923215550101' -> 'zero three two one, five five five, zero one zero one'. Spelled out
    for the same reason as `spoken_digits_ur`: seen live (simulated WhatsApp call), a model
    copying "0321 555 0101" into words dropped two digits."""
    groups = spoken_digits(e164).split()
    return ", ".join(" ".join(ENGLISH_DIGITS[int(d)] for d in g if d.isdigit()) for g in groups)


def spoken_digits_ur(e164: str) -> str:
    """'+923005550128' -> 'صفر تین صفر صفر، پانچ پانچ پانچ، صفر ایک دو آٹھ': one Urdu word per
    digit, grouped like `spoken_digits`. Left to the LLM, Urdu read-backs grouped digits into
    numbers and got them wrong ("three hundred, fifty, fifty..."), defeating the read-back."""
    groups = spoken_digits(e164).split()
    return "، ".join(" ".join(URDU_DIGITS[int(d)] for d in g if d.isdigit()) for g in groups)
