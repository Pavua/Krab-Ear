"""wave-34 — F1 PlaybackTracker Infinity guard + F2 UsageTracker dict validation.

F1 (playback_tracker.py):
  - duration_sec=+Inf / -Inf / NaN → rejected, total_listened_sec unchanged.
  - duration_sec > 86400 (24 h) → rejected (cap single play).
  - Normal accumulation still works after rejection.

F2 (usage_tracker.py):
  - usage_stats.json with a non-dict daily entry (str, int, list, None) → no
    crash on load or get_usage_stats(); bad entries silently discarded.
  - Valid entries in the same file are loaded correctly.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.playback_tracker import PlaybackTracker  # noqa: E402
from backend.usage_tracker import UsageTracker  # noqa: E402


# ---------------------------------------------------------------------------
# F1 — PlaybackTracker Infinity guard
# ---------------------------------------------------------------------------

class TestPlaybackInfinityGuard(unittest.TestCase):
    """F1: +Inf/-Inf/NaN durations must not poison total_listened_sec."""

    def _tracker(self) -> PlaybackTracker:
        return PlaybackTracker(data_dir=None)

    # -- non-finite inputs are rejected --------------------------------------

    def test_inf_duration_rejected(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=float("inf"))
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_neg_inf_duration_rejected(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=float("-inf"))
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_nan_duration_rejected(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=float("nan"))
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    # -- total not poisoned after a bad call ---------------------------------

    def test_total_unchanged_after_inf_attempt(self):
        t = self._tracker()
        t.record_playback("item1", duration_listened_sec=10.0)
        t.record_playback("item1", duration_listened_sec=float("inf"))
        stats = t.get_playback_stats("item1")
        # total must stay at 10.0 — Inf must not have been added
        self.assertAlmostEqual(stats["total_listened_sec"], 10.0)
        self.assertTrue(math.isfinite(stats["total_listened_sec"]))

    def test_total_unchanged_after_nan_attempt(self):
        t = self._tracker()
        t.record_playback("item1", duration_listened_sec=5.0)
        t.record_playback("item1", duration_listened_sec=float("nan"))
        stats = t.get_playback_stats("item1")
        self.assertAlmostEqual(stats["total_listened_sec"], 5.0)
        self.assertTrue(math.isfinite(stats["total_listened_sec"]))

    # -- play_count is NOT incremented on a bad call -------------------------

    def test_play_count_not_incremented_on_inf(self):
        t = self._tracker()
        t.record_playback("item1", duration_listened_sec=1.0)
        t.record_playback("item1", duration_listened_sec=float("inf"))
        stats = t.get_playback_stats("item1")
        self.assertEqual(stats["play_count"], 1)

    # -- 24h single-play cap -----------------------------------------------

    def test_duration_over_24h_rejected(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=86401.0)
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_duration_exactly_24h_accepted(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=86400.0)
        # 86400 == 24 h exactly → within bound → accepted
        self.assertIn("play_count", result)
        self.assertEqual(result["play_count"], 1)
        self.assertAlmostEqual(result["total_listened_sec"], 86400.0)

    def test_duration_just_under_24h_accepted(self):
        t = self._tracker()
        result = t.record_playback("item1", duration_listened_sec=86399.9)
        self.assertIn("play_count", result)

    # -- normal accumulation after rejection still works --------------------

    def test_normal_accumulation_after_inf_rejection(self):
        t = self._tracker()
        t.record_playback("item1", duration_listened_sec=10.0)
        t.record_playback("item1", duration_listened_sec=float("inf"))
        t.record_playback("item1", duration_listened_sec=5.0)
        stats = t.get_playback_stats("item1")
        self.assertEqual(stats["play_count"], 2)
        self.assertAlmostEqual(stats["total_listened_sec"], 15.0)

    # -- persistence: Infinity guard survives disk round-trip ---------------

    def test_inf_guard_with_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = PlaybackTracker(data_dir=tmpdir)
            t1.record_playback("item1", duration_listened_sec=20.0)
            t1.record_playback("item1", duration_listened_sec=float("inf"))

            t2 = PlaybackTracker(data_dir=tmpdir)
            stats = t2.get_playback_stats("item1")
            self.assertAlmostEqual(stats["total_listened_sec"], 20.0)
            self.assertTrue(math.isfinite(stats["total_listened_sec"]))

    # -- IPC handler also rejects non-finite --------------------------------

    def test_handle_record_playback_inf_rejected(self):
        t = self._tracker()
        result = t.handle_record_playback(
            {"item_id": "ipc_item", "duration_listened_sec": float("inf")}
        )
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")

    def test_handle_record_playback_nan_rejected(self):
        t = self._tracker()
        result = t.handle_record_playback(
            {"item_id": "ipc_item", "duration_listened_sec": float("nan")}
        )
        self.assertEqual(result.get("ok"), False)
        self.assertEqual(result.get("reason"), "invalid_duration")


# ---------------------------------------------------------------------------
# F2 — UsageTracker dict validation on _load
# ---------------------------------------------------------------------------

class TestUsageTrackerDictValidation(unittest.TestCase):
    """F2: non-dict entries in daily stats must not crash load or get_usage_stats."""

    def _write_and_load(self, daily_payload: dict) -> UsageTracker:
        """Write a custom daily payload to disk and return a freshly loaded tracker."""
        self._tmp = tempfile.TemporaryDirectory()
        stats_path = Path(self._tmp.name) / "usage_stats.json"
        data = {
            "daily": daily_payload,
            "all_time": {"recordings": 0, "duration_sec": 0.0, "words": 0},
        }
        stats_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return UsageTracker(data_dir=self._tmp.name)

    def tearDown(self):
        if hasattr(self, "_tmp"):
            self._tmp.cleanup()

    # -- non-dict values must be silently discarded -------------------------

    def test_string_value_discarded(self):
        t = self._write_and_load({"2025-01-01": "bad_string"})
        stats = t.get_usage_stats()
        self.assertIsInstance(stats, dict)

    def test_integer_value_discarded(self):
        t = self._write_and_load({"2025-01-01": 42})
        stats = t.get_usage_stats()
        self.assertIsInstance(stats, dict)

    def test_list_value_discarded(self):
        t = self._write_and_load({"2025-01-01": [1, 2, 3]})
        stats = t.get_usage_stats()
        self.assertIsInstance(stats, dict)

    def test_none_value_discarded(self):
        t = self._write_and_load({"2025-01-01": None})
        stats = t.get_usage_stats()
        self.assertIsInstance(stats, dict)

    def test_bool_value_discarded(self):
        # bool is a subclass of int in Python — still not a valid entry
        t = self._write_and_load({"2025-01-01": True})
        stats = t.get_usage_stats()
        self.assertIsInstance(stats, dict)

    # -- get_usage_stats returns zero all-time when all entries discarded ---

    def test_all_time_zeros_when_all_entries_discarded(self):
        t = self._write_and_load({"2025-01-01": "garbage", "2025-01-02": 99})
        stats = t.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 0)

    # -- valid dict entries alongside bad ones are preserved ----------------

    def test_valid_entry_preserved_alongside_bad(self):
        from datetime import date
        today = date.today().isoformat()
        t = self._write_and_load({
            today: {"recordings": 3, "duration_sec": 30.0, "words": 150},
            "2000-01-01": "bad_string",
        })
        stats = t.get_usage_stats()
        # today's recordings must be visible in this_week / today
        self.assertEqual(stats["today"]["recordings"], 3)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 30.0)

    def test_multiple_valid_entries_all_preserved(self):
        from datetime import date, timedelta
        today = date.today()
        payload = {}
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            payload[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        payload["1999-12-31"] = "injected_bad"
        t = self._write_and_load(payload)
        stats = t.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 3)

    # -- tracker is still usable after partial load -------------------------

    def test_record_usage_after_bad_load(self):
        t = self._write_and_load({"2025-01-01": "bad"})
        t.record_usage(5.0, 25)
        stats = t.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)

    # -- warning is emitted when entries are discarded ----------------------

    def test_warning_logged_on_discard(self):
        self._tmp = tempfile.TemporaryDirectory()
        stats_path = Path(self._tmp.name) / "usage_stats.json"
        data = {
            "daily": {"2025-01-01": "bad_entry"},
            "all_time": {"recordings": 0, "duration_sec": 0.0, "words": 0},
        }
        stats_path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertLogs("KrabEar.Backend.UsageTracker", level="WARNING") as cm:
            tracker = UsageTracker(data_dir=self._tmp.name)
        del tracker  # used only for side-effect (loading triggers warning)
        warn_msgs = [r for r in cm.output if "WARNING" in r]
        self.assertTrue(warn_msgs, "Expected WARNING log when discarding non-dict entries")

    # -- empty daily section loads cleanly ----------------------------------

    def test_empty_daily_loads_cleanly(self):
        t = self._write_and_load({})
        stats = t.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 0)


if __name__ == "__main__":
    unittest.main()
