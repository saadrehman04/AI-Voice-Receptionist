"""Pakistani phone numbers -> E.164 (`+92...`).

Callers say numbers in many shapes ("0300-1234567", "+92 300 1234567", "923001234567"), and
Urdu speech-to-text may return Urdu or Arabic-Indic digits (۰۳۰۰...). We normalise all of them
so a booking is stored and matched under one canonical number.
"""

from __future__ import annotations

import re

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


def spoken_digits(e164: str) -> str:
    """'+923001234567' -> '0300 123 4567', the way a receptionist reads a number back."""
    national = "0" + e164.removeprefix("+92")
    if len(national) == 11:
        return f"{national[:4]} {national[4:7]} {national[7:]}"
    return national
