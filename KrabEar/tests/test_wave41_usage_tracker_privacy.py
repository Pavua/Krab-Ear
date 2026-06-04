"""wave-41 MED: get_usage_stats privacy gate + UsageTracker.clear_all() wired to purge.

C1: _handle_get_usage_stats returns ok:False when privacy_mode_enabled=True.
C2: UsageTracker.clear_all() deletes usage_stats.json and resets in-memory counters.
C3: _handle_purge_all_data calls usage_tracker.clear_all() when purge runs.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.usage_tracker import UsageTracker


# ---------------------------------------------------------------------------
# C2: UsageTracker.clear_all() unit tests
# ---------------------------------------------------------------------------

class TestUsageTrackerClearAll(unittest.TestCase):
    """Unit tests for UsageTracker.clear_all()."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clear_all_resets_in_memory_counters(self) -> None:
        """clear_all() zeros daily + all_time counters in RAM."""
        self.tracker.record_usage(60.0, 300)
        self.tracker.record_usage(30.0, 150)

        self.tracker.clear_all()

        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 0)
        self.assertEqual(stats["today"]["total_duration_sec"], 0.0)
        self.assertEqual(stats["today"]["total_words"], 0)
        self.assertEqual(stats["all_time"]["recordings"], 0)
        self.assertEqual(stats["all_time"]["total_duration_sec"], 0.0)
        self.assertEqual(stats["all_time"]["total_words"], 0)
        self.assertEqual(stats["streak_days"], 0)
        self.assertIsNone(stats["peak_day"])
        self.assertEqual(stats["daily_history"], [])

    def test_clear_all_deletes_stats_file(self) -> None:
        """clear_all() removes usage_stats.json from disk."""
        self.tracker.record_usage(10.0, 50)
        stats_file = Path(self.tmp.name) / "usage_stats.json"
        self.assertTrue(stats_file.exists(), "prerequisite: file must exist before clear")

        self.tracker.clear_all()

        self.assertFalse(stats_file.exists(), "usage_stats.json must be deleted by clear_all()")

    def test_clear_all_is_idempotent_no_file(self) -> None:
        """clear_all() on a tracker with no file on disk does not raise."""
        tracker_no_file = UsageTracker(data_dir=self.tmp.name)
        # Never wrote any data — file does not exist
        stats_file = Path(self.tmp.name) / "usage_stats.json"
        self.assertFalse(stats_file.exists())
        # Must not raise
        tracker_no_file.clear_all()
        stats = tracker_no_file.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 0)

    def test_clear_all_no_data_dir_does_not_raise(self) -> None:
        """clear_all() on an in-memory-only tracker (no data_dir) does not raise."""
        tracker_mem = UsageTracker(data_dir=None)
        tracker_mem.record_usage(5.0, 20)
        tracker_mem.clear_all()
        stats = tracker_mem.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 0)

    def test_clear_all_allows_subsequent_recording(self) -> None:
        """After clear_all(), the tracker continues to function normally."""
        self.tracker.record_usage(10.0, 50)
        self.tracker.clear_all()

        self.tracker.record_usage(20.0, 100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 20.0)
        self.assertEqual(stats["today"]["total_words"], 100)

    def test_clear_all_daily_dict_is_empty(self) -> None:
        """clear_all() empties the internal _daily dict."""
        from datetime import date, timedelta
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 5.0, "words": 20}
        self.tracker.clear_all()
        self.assertEqual(self.tracker._daily, {})

    def test_clear_all_file_gone_new_record_creates_new_file(self) -> None:
        """After clear_all() deletes file, a new record_usage() recreates it."""
        self.tracker.record_usage(5.0, 25)
        self.tracker.clear_all()

        stats_file = Path(self.tmp.name) / "usage_stats.json"
        self.assertFalse(stats_file.exists())

        self.tracker.record_usage(8.0, 40)
        self.assertTrue(stats_file.exists(), "new file must be written after clear+record")
        data = json.loads(stats_file.read_text(encoding="utf-8"))
        self.assertEqual(data["all_time"]["recordings"], 1)


# ---------------------------------------------------------------------------
# C1: _handle_get_usage_stats privacy gate via BackendService stub
# ---------------------------------------------------------------------------

class TestGetUsageStatsPrivacyGate(unittest.TestCase):
    """Test _handle_get_usage_stats() returns ok:False when privacy_mode is on."""

    def _make_service_stub(self, privacy_on: bool) -> "object":
        """Build a minimal BackendService-like object with just what the handler needs."""
        from backend.usage_tracker import UsageTracker as _UT

        tmp = tempfile.mkdtemp()
        tracker = _UT(data_dir=tmp)
        tracker.record_usage(30.0, 150)

        # Import the actual method so we test the real code path
        import importlib
        svc_mod = importlib.import_module("backend.service")
        handler = svc_mod.BackendService._handle_get_usage_stats

        stub = MagicMock()
        stub._usage_tracker = tracker
        stub._get_runtime_setting = MagicMock(
            side_effect=lambda key, default=None: privacy_on if key == "privacy_mode_enabled" else default
        )
        # Bind the unbound method to our stub
        stub._handle_get_usage_stats = lambda params: handler(stub, params)
        return stub

    def test_privacy_off_returns_stats(self) -> None:
        """With privacy_mode_enabled=False, real stats are returned."""
        stub = self._make_service_stub(privacy_on=False)
        result = stub._handle_get_usage_stats({})
        # Real stats dict must have the expected keys
        self.assertIn("today", result)
        self.assertIn("all_time", result)
        self.assertIn("streak_days", result)
        self.assertNotEqual(result.get("ok"), False)

    def test_privacy_on_returns_ok_false(self) -> None:
        """With privacy_mode_enabled=True, handler returns ok:False, reason:privacy_mode_active."""
        stub = self._make_service_stub(privacy_on=True)
        result = stub._handle_get_usage_stats({})
        self.assertFalse(result.get("ok"), "Expected ok=False in privacy mode")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_does_not_leak_stat_keys(self) -> None:
        """Privacy-mode response must NOT contain stats keys (today, daily_history, etc.)."""
        stub = self._make_service_stub(privacy_on=True)
        result = stub._handle_get_usage_stats({})
        for forbidden_key in ("today", "this_week", "this_month", "all_time",
                              "daily_history", "streak_days", "peak_day"):
            self.assertNotIn(forbidden_key, result,
                             f"Key '{forbidden_key}' must not appear in privacy-mode response")


# ---------------------------------------------------------------------------
# C3: _handle_purge_all_data calls usage_tracker.clear_all()
# ---------------------------------------------------------------------------

class TestPurgeAllDataCallsUsageTrackerClearAll(unittest.TestCase):
    """Test that handle_purge_all_data triggers usage_tracker.clear_all()."""

    def test_purge_calls_clear_all_when_purge_succeeds(self) -> None:
        """When the purge confirms (ok not False), clear_all() is called."""
        import importlib
        svc_mod = importlib.import_module("backend.service")
        handler = svc_mod.BackendService._handle_purge_all_data

        mock_usage_tracker = MagicMock(spec=UsageTracker)

        stub = MagicMock()
        stub._usage_tracker = mock_usage_tracker
        # auto_backup is called in the handler wrapper
        stub._auto_backup = MagicMock()
        stub._auto_backup.set_purged = MagicMock()
        stub._auto_backup.clear_purged = MagicMock()
        # _history.handle_purge_all_data returns success
        stub._history = MagicMock()
        stub._history.handle_purge_all_data = MagicMock(return_value={"ok": True, "deleted": 5})
        # other collaborators cleared in the same block
        stub._hotword_detector = MagicMock()
        stub._hotword_detector.clear = MagicMock()
        stub._transcription_queue = MagicMock()
        stub._transcription_queue.clear = MagicMock()

        result = handler(stub, {"confirm": True})

        mock_usage_tracker.clear_all.assert_called_once()
        self.assertEqual(result.get("ok"), True)

    def test_purge_does_not_call_clear_all_when_purge_refused(self) -> None:
        """When purge returns ok=False (confirm missing), clear_all() is NOT called."""
        import importlib
        svc_mod = importlib.import_module("backend.service")
        handler = svc_mod.BackendService._handle_purge_all_data

        mock_usage_tracker = MagicMock(spec=UsageTracker)

        stub = MagicMock()
        stub._usage_tracker = mock_usage_tracker
        stub._auto_backup = MagicMock()
        stub._auto_backup.set_purged = MagicMock()
        stub._auto_backup.clear_purged = MagicMock()
        # _history returns ok=False (confirm not provided)
        stub._history = MagicMock()
        stub._history.handle_purge_all_data = MagicMock(
            return_value={"ok": False, "reason": "confirm_required"}
        )
        stub._hotword_detector = MagicMock()
        stub._transcription_queue = MagicMock()

        result = handler(stub, {})

        mock_usage_tracker.clear_all.assert_not_called()
        self.assertFalse(result.get("ok"))

    def test_purge_clear_all_exception_does_not_abort_purge(self) -> None:
        """Even if clear_all() raises, purge result is still returned (no exception propagation)."""
        import importlib
        svc_mod = importlib.import_module("backend.service")
        handler = svc_mod.BackendService._handle_purge_all_data

        mock_usage_tracker = MagicMock(spec=UsageTracker)
        mock_usage_tracker.clear_all.side_effect = RuntimeError("disk full")

        stub = MagicMock()
        stub._usage_tracker = mock_usage_tracker
        stub._auto_backup = MagicMock()
        stub._auto_backup.set_purged = MagicMock()
        stub._auto_backup.clear_purged = MagicMock()
        stub._history = MagicMock()
        stub._history.handle_purge_all_data = MagicMock(return_value={"ok": True})
        stub._hotword_detector = MagicMock()
        stub._transcription_queue = MagicMock()

        # Must not raise
        result = handler(stub, {"confirm": True})
        self.assertEqual(result.get("ok"), True)


# ---------------------------------------------------------------------------
# Integration: clear_all + file verified gone
# ---------------------------------------------------------------------------

class TestUsageTrackerClearAllIntegration(unittest.TestCase):
    """Integration: write stats file → clear → verify file gone, counters zero."""

    def test_clear_all_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            from datetime import date, timedelta
            today = date.today()
            for i in range(7):
                d = (today - timedelta(days=i)).isoformat()
                tracker._daily[d] = {"recordings": 3, "duration_sec": 30.0, "words": 100}
            tracker._all_recordings = 21
            tracker._all_duration = 210.0
            tracker._all_words = 700
            tracker._persist()

            stats_file = Path(tmp) / "usage_stats.json"
            self.assertTrue(stats_file.exists())

            tracker.clear_all()

            self.assertFalse(stats_file.exists(), "file must be gone after clear_all()")
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 0)
            self.assertEqual(stats["streak_days"], 0)
            self.assertIsNone(stats["peak_day"])
            self.assertEqual(stats["daily_history"], [])

    def test_clear_all_reload_starts_fresh(self) -> None:
        """A new UsageTracker after clear_all on the same dir starts with zero counters."""
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(10.0, 50)
            t1.record_usage(20.0, 100)
            t1.clear_all()

            t2 = UsageTracker(data_dir=tmp)
            stats = t2.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 0)
            self.assertEqual(stats["all_time"]["total_duration_sec"], 0.0)
            self.assertEqual(stats["all_time"]["total_words"], 0)


if __name__ == "__main__":
    unittest.main()
