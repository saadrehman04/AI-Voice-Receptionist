from datetime import datetime, timedelta

import pytest

from receptionist_agent.brain.state import CallState, Intent, StateError

T = datetime(2026, 11, 18, 14, 0)


def full_state() -> CallState:
    s = CallState()
    s.record_offer([T])
    s.update(service="Haircut", start=T, caller_name="Sara", phone="+923001234567")
    return s


def test_new_state_is_empty() -> None:
    s = CallState()
    assert s.intent is Intent.UNKNOWN
    assert s.missing_for_booking() == ["service", "start", "caller_name", "phone"]
    assert not s.confirmed


def test_update_reports_only_real_changes() -> None:
    s = CallState()
    assert s.update(service="Haircut", caller_name=None) == ["service"]
    assert s.update(service="Haircut") == []


def test_confirm_requires_all_details() -> None:
    s = CallState()
    s.update(service="Haircut")
    with pytest.raises(StateError, match="start, caller_name, phone"):
        s.confirm()


def test_confirm_requires_an_offered_slot() -> None:
    s = CallState()
    s.update(service="Haircut", start=T, caller_name="Sara", phone="+923001234567")
    with pytest.raises(StateError, match="not offered"):
        s.confirm()


def test_changing_a_detail_withdraws_confirmation() -> None:
    s = full_state()
    s.confirm()
    assert s.confirmed
    s.update(caller_name="Sara Khan")
    assert not s.confirmed


def test_non_booking_detail_keeps_confirmation() -> None:
    s = full_state()
    s.confirm()
    s.update(preferred_time="afternoon")
    assert s.confirmed


def test_cannot_mark_booked_unconfirmed() -> None:
    s = full_state()
    with pytest.raises(StateError):
        s.mark_booked("ABC")
    s.confirm()
    s.mark_booked("ABC")
    assert s.booking_id == "ABC"


def test_intent_change_resets_confirmation() -> None:
    s = full_state()
    s.confirm()
    s.set_intent(Intent.CANCEL)
    assert not s.confirmed
    assert s.intent is Intent.CANCEL


def test_prepare_then_commit_must_match() -> None:
    s = CallState()
    with pytest.raises(StateError, match="read the details back first"):
        s.require_prepared("book", ("Haircut",))
    s.prepare("book", ("Haircut", "14:00"))
    with pytest.raises(StateError, match="differ"):
        s.require_prepared("book", ("Haircut", "15:00"))
    with pytest.raises(StateError, match="read the details back first"):
        s.require_prepared("cancel", ("Haircut", "14:00"))
    s.require_prepared("book", ("Haircut", "14:00"))  # no error
    s.clear_pending()
    assert s.pending is None


def test_offers_accumulate_without_duplicates() -> None:
    s = CallState()
    s.record_offer([T, T + timedelta(hours=1)])
    s.record_offer([T])
    assert len(s.offered_slots) == 2


def test_summary_is_plain_data() -> None:
    s = full_state()
    assert s.summary()["start"] == T.isoformat()
