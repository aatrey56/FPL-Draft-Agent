"""Tests for the autopilot notification logic (scripts/notify_state.py)."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from notify_state import compute_notifications  # noqa: E402

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
EVENTS = [{
    "id": 2,
    "trades_time": "2026-08-26T17:30:00Z",     # past
    "waivers_time": "2026-08-27T17:30:00Z",    # in 5.5h
    "deadline_time": "2026-08-28T17:30:00Z",   # in 29.5h
}]


def test_threshold_notifications_fire_once():
    msgs, state = compute_notifications(EVENTS, {}, {}, NOW)
    # waivers within 24h fires; lineup lock 29.5h away -> nothing.
    assert any("waivers due" in m and "within 24h" in m for m in msgs)
    assert not any("lineup lock" in m for m in msgs)
    # Re-run with same state: nothing repeats.
    msgs2, _ = compute_notifications(EVENTS, {}, state, NOW)
    assert msgs2 == []
    # Inside 3h of the waiver deadline there is no extra reminder any more
    # (the deadline agent owns that), and the lineup lock's 24h notice fires once.
    msgs3, state3 = compute_notifications(EVENTS, {}, state, NOW + timedelta(hours=7))
    assert [m for m in msgs3 if "lineup lock" in m and "within 24h" in m]
    assert not any("under" in m for m in msgs3)
    msgs4, _ = compute_notifications(EVENTS, {}, state3, NOW + timedelta(hours=9))
    assert msgs4 == []
    # Two hours before the lineup lock: still no tight-window reminder.
    msgs5, _ = compute_notifications(EVENTS, {}, state3, NOW + timedelta(hours=27))
    assert msgs5 == []


def test_transition_notifications():
    game = {"current_event": 1, "current_event_finished": True,
            "next_event": 2, "waivers_processed": True}
    msgs, state = compute_notifications([], game, {}, NOW)
    assert any("GW1 is final" in m for m in msgs)
    assert any("free agency open" in m for m in msgs)
    msgs2, _ = compute_notifications([], game, state, NOW + timedelta(hours=1))
    assert msgs2 == []


def test_past_and_malformed_times_are_ignored():
    events = [{"id": 1, "trades_time": "2026-08-01T17:30:00Z",
               "waivers_time": "nonsense", "deadline_time": None}]
    msgs, _ = compute_notifications(events, {}, {}, NOW)
    assert msgs == []
