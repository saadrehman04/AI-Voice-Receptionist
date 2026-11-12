"""The WhatsApp text a caller gets after a booking, a move or a cancellation.

Built from the tool's own result (what the backend actually stored), never from what the LLM
said, so the message can't disagree with the booking. Written in the language of the call.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from receptionist_shared.tenant import TenantConfig

UR_WEEKDAYS = ("پیر", "منگل", "بدھ", "جمعرات", "جمعہ", "ہفتہ", "اتوار")
UR_MONTHS = (
    "جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون",
    "جولائی", "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر",
)  # fmt: skip


def _when_en(start: datetime) -> str:
    hour = start.strftime("%I").lstrip("0")
    return f"{start:%A} {start.day} {start:%B}, {hour}:{start:%M} {start:%p}"


def _when_ur(start: datetime) -> str:
    h = start.hour
    period = "صبح" if h < 12 else "دوپہر" if h < 16 else "شام" if h < 20 else "رات"
    hour12 = h % 12 or 12
    return (
        f"{UR_WEEKDAYS[start.weekday()]} {start.day} {UR_MONTHS[start.month - 1]}، "
        f"{period} {hour12}:{start:%M} بجے"
    )


def confirmation_text(
    tenant: TenantConfig, tool: str, result: dict[str, Any], language: str | None
) -> str | None:
    """The message for a successful booking change, or None when there's nothing to confirm."""
    action = (
        "booked"
        if tool == "create_booking" and result.get("booked")
        else "moved"
        if tool == "reschedule_booking" and result.get("rescheduled")
        else "cancelled"
        if tool == "cancel_booking" and result.get("cancelled")
        else None
    )
    if action is None or not result.get("start"):
        return None
    start = datetime.fromisoformat(str(result["start"])).astimezone(tenant.tz)
    service = str(result.get("service", ""))
    code = str(result.get("booking_id", ""))

    if language == "ur":
        name = tenant.business_name_ur or tenant.business_name
        found = tenant.find_service(service)
        service = (found.name_ur if found else None) or service
        when = _when_ur(start)
        body = {
            "booked": f"آپ کی {service} کی بکنگ {when} کے لیے ہو گئی ہے۔ بکنگ کوڈ: {code}۔",
            "moved": f"آپ کی {service} کی بکنگ اب {when} کو ہے۔ بکنگ کوڈ: {code}۔",
            "cancelled": f"آپ کی {when} والی {service} کی بکنگ منسوخ ہو گئی ہے۔",
        }[action]
        tail = "" if action == "cancelled" else " تبدیلی کے لیے ہمیں دوبارہ واٹس ایپ پر کال کریں۔"
        return f"{name}: {body}{tail}"

    when = _when_en(start)
    body = {
        "booked": f"your {service} is booked for {when}. Booking code: {code}.",
        "moved": f"your {service} has been moved to {when}. Booking code: {code}.",
        "cancelled": f"your {service} on {when} has been cancelled.",
    }[action]
    tail = "" if action == "cancelled" else " To change it, just call us again on WhatsApp."
    return f"{tenant.business_name}: {body}{tail}"


__all__ = ["confirmation_text"]
