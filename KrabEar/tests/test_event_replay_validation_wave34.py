"""Tests for event_replay.py — wave-34 G1 LOW fix.

Covers:
- handle_replay_events rejects from_ts > to_ts
- handle_replay_events rejects time window > 7 days
- replay_events truncates to _MAX_REPLAY_EVENTS (10 000) when buffer has more
- handle_replay_events sets truncated=True when limit reached
- Valid requests within bounds succeed normally
"""

from __future__ import annotations

from backend.event_replay import EventReplayManager, _MAX_REPLAY_EVENTS, _MAX_REPLAY_WINDOW_SEC
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRABEAR_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PROJECT_ROOT), str(KRABEAR_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TestHandleReplayEventsValidation(unittest.TestCase):
    """IPC handler input validation: from_ts/to_ts ordering and window size."""

    def setUp(self):
        self.mgr = EventReplayManager(max_buffer=100)

    def tearDown(self):
        self.mgr.close()

    # ------------------------------------------------------------------
    # from_ts > to_ts must be rejected
    # ------------------------------------------------------------------

    def test_from_ts_greater_than_to_ts_is_rejected(self):
        """from_ts=999, to_ts=1 -> error response."""
        result = self.mgr.handle_replay_events({"from_ts": 999, "to_ts": 1})
        self.assertFalse(result.get("ok", True))
        self.assertIn("from_ts", result.get("reason", ""))

    def test_from_ts_equal_to_ts_is_accepted(self):
        """from_ts == to_ts is a valid degenerate range (empty result)."""
        now = time.time()
        result = self.mgr.handle_replay_events({"from_ts": now, "to_ts": now})
        # Should not be an error — just return empty events
        self.assertNotIn("ok", result)  # success responses don't include 'ok'
        self.assertIn("events", result)
        self.assertEqual(result["count"], 0)

    # ------------------------------------------------------------------
    # Window size > 7 days must be rejected
    # ------------------------------------------------------------------

    def test_window_larger_than_7_days_is_rejected(self):
        """Window > 7 days (604800 s) -> error."""
        to_ts = time.time()
        from_ts = to_ts - (_MAX_REPLAY_WINDOW_SEC + 1)
        result = self.mgr.handle_replay_events({"from_ts": from_ts, "to_ts": to_ts})
        self.assertFalse(result.get("ok", True))
        self.assertIn("7 days", result.get("reason", ""))

    def test_window_exactly_7_days_is_accepted(self):
        """Window == 7 days exactly should pass."""
        to_ts = time.time()
        from_ts = to_ts - _MAX_REPLAY_WINDOW_SEC
        result = self.mgr.handle_replay_events({"from_ts": from_ts, "to_ts": to_ts})
        self.assertNotIn("ok", result)
        self.assertIn("events", result)

    def test_window_one_day_is_accepted(self):
        """Normal 1-day window returns events list."""
        to_ts = time.time()
        from_ts = to_ts - 86400
        result = self.mgr.handle_replay_events({"from_ts": from_ts, "to_ts": to_ts})
        self.assertIn("events", result)

    # ------------------------------------------------------------------
    # Invalid parameter types
    # ------------------------------------------------------------------

    def test_non_numeric_from_ts_is_rejected(self):
        result = self.mgr.handle_replay_events({"from_ts": "not-a-number", "to_ts": time.time()})
        self.assertFalse(result.get("ok", True))
        self.assertIn("numeric", result.get("reason", ""))

    def test_non_numeric_to_ts_is_rejected(self):
        result = self.mgr.handle_replay_events({"from_ts": time.time() - 10, "to_ts": "bad"})
        self.assertFalse(result.get("ok", True))

    # ------------------------------------------------------------------
    # Default values: missing params -> window too large rejected
    # ------------------------------------------------------------------

    def test_missing_both_params_defaults_to_large_window_rejected(self):
        """from_ts defaults to 0 (epoch), to_ts to now → ~56y window → rejected."""
        result = self.mgr.handle_replay_events({})
        self.assertFalse(result.get("ok", True))
        self.assertIn("7 days", result.get("reason", ""))

    # ------------------------------------------------------------------
    # Successful call returns expected structure
    # ------------------------------------------------------------------

    def test_successful_call_returns_events_count_truncated(self):
        """Valid params return {events, count, truncated}."""
        now = time.time()
        # Record one event
        self.mgr.record_event("ping", {"x": 1})
        result = self.mgr.handle_replay_events({"from_ts": now - 3600, "to_ts": now + 3600})
        self.assertIn("events", result)
        self.assertIn("count", result)
        self.assertIn("truncated", result)
        self.assertFalse(result["truncated"])  # only 1 event, well below 10 000


class TestReplayEventsTruncation(unittest.TestCase):
    """replay_events() truncates at _MAX_REPLAY_EVENTS when buffer has more."""

    def test_replay_truncates_at_max_events(self):
        """If buffer holds more than _MAX_REPLAY_EVENTS matching events,
        replay_events returns exactly _MAX_REPLAY_EVENTS."""
        # Use a buffer that can hold 10 001 events
        mgr = EventReplayManager(max_buffer=_MAX_REPLAY_EVENTS + 1)
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with mgr._lock:
                for i in range(_MAX_REPLAY_EVENTS + 1):
                    mgr._buffer.append({"type": "flood", "ts": ts, "data": {}, "seq": i})

            from_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
            to_ts = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
            events = mgr.replay_events(from_ts, to_ts)
            self.assertEqual(len(events), _MAX_REPLAY_EVENTS)
        finally:
            mgr.close()

    def test_replay_does_not_truncate_below_max(self):
        """If buffer holds fewer than _MAX_REPLAY_EVENTS matching events,
        all are returned (no spurious truncation)."""
        mgr = EventReplayManager(max_buffer=500)
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with mgr._lock:
                for i in range(50):
                    mgr._buffer.append({"type": "ok", "ts": ts, "data": {}, "seq": i})

            from_ts = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat(timespec="seconds")
            to_ts = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
            events = mgr.replay_events(from_ts, to_ts)
            self.assertEqual(len(events), 50)
        finally:
            mgr.close()

    def test_handle_replay_events_sets_truncated_flag_when_limit_hit(self):
        """handle_replay_events sets truncated=True when replay_events returns exactly _MAX_REPLAY_EVENTS."""
        mgr = EventReplayManager(max_buffer=_MAX_REPLAY_EVENTS + 1)
        try:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with mgr._lock:
                for i in range(_MAX_REPLAY_EVENTS + 1):
                    mgr._buffer.append({"type": "flood", "ts": ts, "data": {}, "seq": i})

            now = time.time()
            result = mgr.handle_replay_events({"from_ts": now - 3600, "to_ts": now + 3600})
            self.assertIn("truncated", result)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["count"], _MAX_REPLAY_EVENTS)
        finally:
            mgr.close()


class TestReplayEventsWindowConstants(unittest.TestCase):
    """Verify module-level constants have expected values."""

    def test_max_replay_events_is_10000(self):
        self.assertEqual(_MAX_REPLAY_EVENTS, 10_000)

    def test_max_replay_window_is_7_days(self):
        self.assertEqual(_MAX_REPLAY_WINDOW_SEC, 86_400 * 7)


if __name__ == "__main__":
    unittest.main()
