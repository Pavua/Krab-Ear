"""Tests for wave-25 privacy-mode gates.

Covers:
  A1 — AnalyticsService.handle_get_activity_calendar returns empty when privacy_mode=True
  A2 — SharingManager.handle_prepare_share returns error when privacy_mode=True
  A3 — PlaybackTracker.record_playback is no-op when privacy_mode=True
  A3c — PlaybackTracker.record_playback enforces 10_000 key cap (DoS guard)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.analytics_service import AnalyticsService  # noqa: E402
from backend.playback_tracker import PlaybackTracker  # noqa: E402
from backend.sharing_manager import SharingManager  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStore:
    """Minimal fake StateStore."""

    def __init__(self, items=None):
        self._items = items or []
        self.data_dir = "."

    class _CM:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _lock(self):
        return self._CM()

    def _load_active_items_unlocked(self):
        return list(self._items)

    def _load_active_items_with_lock(self):
        return list(self._items)


def _make_analytics(privacy_on: bool = False) -> AnalyticsService:
    return AnalyticsService(
        analytics_dashboard=MagicMock(),
        sentiment_trends=MagicMock(),
        activity_calendar=MagicMock(),
        keyword_cloud_gen=MagicMock(),
        timeline_view=MagicMock(),
        store=_FakeStore(),
        settings_get=lambda k, d: privacy_on if k == "privacy_mode_enabled" else d,
    )


# ---------------------------------------------------------------------------
# A1 — AnalyticsService.handle_get_activity_calendar
# ---------------------------------------------------------------------------

class TestActivityCalendarPrivacyGate(unittest.TestCase):

    def test_privacy_on_returns_empty_schema(self) -> None:
        svc = _make_analytics(privacy_on=True)
        result = svc.handle_get_activity_calendar({})
        # Must return empty payload, never touch store/calendar
        self.assertEqual(result["days"], [])
        self.assertEqual(result["weeks"], [])
        self.assertEqual(result["current_streak"], 0)
        self.assertEqual(result["longest_streak"], 0)
        self.assertEqual(result["total_days"], 0)
        self.assertEqual(result["total_recordings"], 0)
        self.assertEqual(result["months_covered"], 0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        self.assertTrue(result.get("ok", True))  # ok key present or absent is fine, but no error

    def test_privacy_on_does_not_touch_store(self) -> None:
        activity_calendar = MagicMock()
        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=MagicMock(),
            activity_calendar=activity_calendar,
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
            settings_get=lambda k, d: True if k == "privacy_mode_enabled" else d,
        )
        svc.handle_get_activity_calendar({"include_svg": True})
        activity_calendar.generate_calendar.assert_not_called()
        activity_calendar.generate_calendar_svg.assert_not_called()

    def test_privacy_off_delegates_to_calendar(self) -> None:
        activity_calendar = MagicMock()
        fake_cal = MagicMock()
        fake_cal.to_dict.return_value = {"days": [1, 2], "total_recordings": 3,
                                         "weeks": [], "current_streak": 1,
                                         "longest_streak": 2, "total_days": 5,
                                         "months_covered": 1}
        activity_calendar.generate_calendar.return_value = fake_cal

        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=MagicMock(),
            activity_calendar=activity_calendar,
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
            settings_get=lambda k, d: False if k == "privacy_mode_enabled" else d,
        )
        result = svc.handle_get_activity_calendar({"months": 3})
        activity_calendar.generate_calendar.assert_called_once()
        self.assertEqual(result["total_recordings"], 3)

    def test_privacy_on_no_reason_key_with_sentiment_parity(self) -> None:
        """Verify the response key set is consistent with sibling handlers."""
        svc = _make_analytics(privacy_on=True)
        result = svc.handle_get_activity_calendar({})
        self.assertIn("reason", result)
        self.assertEqual(result["reason"], "privacy_mode_active")

    def test_privacy_setting_default_false(self) -> None:
        """When no settings_get is wired, privacy gate must be off (default-safe)."""
        svc = AnalyticsService(
            analytics_dashboard=MagicMock(),
            sentiment_trends=MagicMock(),
            activity_calendar=MagicMock(),
            keyword_cloud_gen=MagicMock(),
            timeline_view=MagicMock(),
            store=_FakeStore(),
            # settings_get=None → uses lambda k, d: d (returns default)
        )
        # Should not raise, and should call the underlying calendar
        svc._activity_calendar.generate_calendar.return_value = MagicMock(
            to_dict=lambda: {"days": [], "weeks": [], "current_streak": 0,
                             "longest_streak": 0, "total_days": 0,
                             "total_recordings": 0, "months_covered": 0}
        )
        result = svc.handle_get_activity_calendar({})
        # No "reason" key when privacy is off
        self.assertNotIn("reason", result)


# ---------------------------------------------------------------------------
# A2 — SharingManager.handle_prepare_share
# ---------------------------------------------------------------------------

class TestSharingManagerPrivacyGate(unittest.TestCase):

    def _make_store(self) -> Any:
        store = MagicMock()
        store.data_dir = tempfile.mkdtemp()
        return store

    def test_privacy_on_blocks_prepare_share(self) -> None:
        store = self._make_store()
        mgr = SharingManager(
            store=store,
            privacy_mode_fn=lambda: True,
        )
        result = mgr.handle_prepare_share({"item_ids": ["id1"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "privacy_mode_active")
        self.assertIn("Sharing disabled in privacy mode", result["error"])

    def test_privacy_on_no_disk_write(self) -> None:
        """Privacy gate must return before any disk writes."""
        store = self._make_store()
        mgr = SharingManager(
            store=store,
            privacy_mode_fn=lambda: True,
        )
        shares_dir = Path(store.data_dir) / "shares"
        before = set(os.listdir(shares_dir)) if shares_dir.exists() else set()
        mgr.handle_prepare_share({"item_ids": ["id1", "id2"]})
        after = set(os.listdir(shares_dir)) if shares_dir.exists() else set()
        # No new files written
        new_files = after - before
        share_files = {f for f in new_files if f.startswith("krabear_share_")}
        self.assertEqual(len(share_files), 0, f"Unexpected share files written: {share_files}")

    def test_privacy_off_proceeds_normally(self) -> None:
        """When privacy is off the handler runs (may raise on missing items — that's fine)."""
        store = self._make_store()
        # item_ids that resolve to nothing — will hit "no items" warning path
        store.get_history_item_by_id = MagicMock(return_value=None)
        mgr = SharingManager(
            store=store,
            privacy_mode_fn=lambda: False,
        )
        result = mgr.handle_prepare_share({"item_ids": ["id1"]})
        # Should not return privacy_mode_active
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")
        # May have "warning: no_items_found" but no privacy block
        self.assertNotIn("Sharing disabled", result.get("error", ""))

    def test_privacy_fn_none_allows_sharing(self) -> None:
        """Without a privacy_mode_fn (None), sharing is allowed."""
        store = self._make_store()
        store.get_history_item_by_id = MagicMock(return_value=None)
        mgr = SharingManager(store=store)  # privacy_mode_fn=None
        result = mgr.handle_prepare_share({"item_ids": ["id1"]})
        self.assertNotEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_toggle(self) -> None:
        """Callable is evaluated per-call, not cached at construction time."""
        store = self._make_store()
        privacy_on = [False]
        mgr = SharingManager(
            store=store,
            privacy_mode_fn=lambda: privacy_on[0],
        )
        store.get_history_item_by_id = MagicMock(return_value=None)
        # Off → allowed
        result_off = mgr.handle_prepare_share({"item_ids": ["id1"]})
        self.assertNotEqual(result_off.get("reason"), "privacy_mode_active")
        # On → blocked
        privacy_on[0] = True
        result_on = mgr.handle_prepare_share({"item_ids": ["id1"]})
        self.assertEqual(result_on["reason"], "privacy_mode_active")


# ---------------------------------------------------------------------------
# A3 — PlaybackTracker privacy gate + DoS cap
# ---------------------------------------------------------------------------

class TestPlaybackTrackerPrivacyGate(unittest.TestCase):

    def test_privacy_on_via_fn_returns_noop(self) -> None:
        tracker = PlaybackTracker(privacy_mode_fn=lambda: True)
        result = tracker.record_playback("item-1", 30.0)
        self.assertEqual(result.get("ok"), True)
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_on_via_fn_does_not_persist(self) -> None:
        tracker = PlaybackTracker(privacy_mode_fn=lambda: True)
        tracker.record_playback("item-1", 30.0)
        # Stats must remain empty
        stats = tracker.get_playback_stats("item-1")
        self.assertEqual(stats["play_count"], 0)

    def test_privacy_off_via_fn_records_normally(self) -> None:
        tracker = PlaybackTracker(privacy_mode_fn=lambda: False)
        result = tracker.record_playback("item-2", 15.0)
        self.assertEqual(result["item_id"], "item-2")
        self.assertEqual(result["play_count"], 1)
        self.assertAlmostEqual(result["total_listened_sec"], 15.0)

    def test_privacy_fn_evaluated_per_call(self) -> None:
        privacy_on = [False]
        tracker = PlaybackTracker(privacy_mode_fn=lambda: privacy_on[0])
        # First call: off → records
        tracker.record_playback("item-3", 5.0)
        self.assertEqual(tracker.get_playback_stats("item-3")["play_count"], 1)
        # Toggle on → no-op
        privacy_on[0] = True
        result = tracker.record_playback("item-3", 5.0)
        self.assertEqual(result.get("reason"), "privacy_mode_active")
        # Count unchanged
        self.assertEqual(tracker.get_playback_stats("item-3")["play_count"], 1)
        # Toggle off → records again
        privacy_on[0] = False
        tracker.record_playback("item-3", 5.0)
        self.assertEqual(tracker.get_playback_stats("item-3")["play_count"], 2)

    def test_static_privacy_mode_enabled_still_works(self) -> None:
        """privacy_mode_enabled=True without fn also gates (backward compat)."""
        tracker = PlaybackTracker(privacy_mode_enabled=True)
        result = tracker.record_playback("item-x")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_set_privacy_mode_still_works(self) -> None:
        """set_privacy_mode() backward compat path gating."""
        tracker = PlaybackTracker()
        tracker.set_privacy_mode(True)
        result = tracker.record_playback("item-y")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_fn_takes_priority_over_static_flag(self) -> None:
        """fn=False overrides static enabled=True."""
        tracker = PlaybackTracker(privacy_mode_enabled=True, privacy_mode_fn=lambda: False)
        result = tracker.record_playback("item-z", 1.0)
        # fn says False → recording proceeds
        self.assertEqual(result["play_count"], 1)

    def test_record_returns_dict_with_stats(self) -> None:
        """Normal path: record_playback returns current stats dict."""
        tracker = PlaybackTracker()
        r1 = tracker.record_playback("abc", 10.0)
        self.assertEqual(r1["play_count"], 1)
        self.assertAlmostEqual(r1["total_listened_sec"], 10.0)
        r2 = tracker.record_playback("abc", 20.0)
        self.assertEqual(r2["play_count"], 2)
        self.assertAlmostEqual(r2["total_listened_sec"], 30.0)


class TestPlaybackTrackerDoSCap(unittest.TestCase):

    def test_cap_at_10000_keys(self) -> None:
        tracker = PlaybackTracker()
        # Fill exactly 10_000 keys
        for i in range(10_000):
            r = tracker.record_playback(f"item-{i}", 0.0)
            self.assertNotEqual(r.get("reason"), "tracker_full",
                                f"Unexpected tracker_full at key {i}")
        self.assertEqual(len(tracker._stats), 10_000)
        # 10_001st new key → tracker_full
        result = tracker.record_playback("item-overflow", 1.0)
        self.assertFalse(result.get("ok", True) and result.get("reason") != "tracker_full",
                         "Expected tracker_full response")
        self.assertEqual(result.get("reason"), "tracker_full")
        # Stats not modified
        self.assertEqual(len(tracker._stats), 10_000)

    def test_cap_does_not_block_existing_key(self) -> None:
        tracker = PlaybackTracker()
        # Fill to cap
        for i in range(10_000):
            tracker.record_playback(f"item-{i}", 0.0)
        # Existing key → still updates fine
        result = tracker.record_playback("item-0", 5.0)
        self.assertNotEqual(result.get("reason"), "tracker_full")
        self.assertGreaterEqual(result.get("play_count", 0), 2)

    def test_cap_off_by_one(self) -> None:
        """Key 9_999 (index 9999) is the last allowed new key."""
        tracker = PlaybackTracker()
        for i in range(9_999):
            tracker.record_playback(f"k{i}")
        # 10_000th key: allowed (fills the cap)
        r = tracker.record_playback("k9999")
        self.assertNotEqual(r.get("reason"), "tracker_full")
        # 10_001st: blocked
        r2 = tracker.record_playback("k10000")
        self.assertEqual(r2.get("reason"), "tracker_full")


class TestPlaybackTrackerWithDisk(unittest.TestCase):
    """Verify privacy gate + cap work correctly with disk persistence."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def test_privacy_on_does_not_write_file(self) -> None:
        tracker = PlaybackTracker(
            data_dir=self._tmpdir,
            privacy_mode_fn=lambda: True,
        )
        tracker.record_playback("item-disk-1", 10.0)
        playback_file = Path(self._tmpdir) / "playback_stats.json"
        if playback_file.exists():
            data = json.loads(playback_file.read_text())
            self.assertNotIn("item-disk-1", data,
                             "Privacy gate must not persist data to disk")

    def test_privacy_off_writes_file(self) -> None:
        tracker = PlaybackTracker(
            data_dir=self._tmpdir,
            privacy_mode_fn=lambda: False,
        )
        tracker.record_playback("item-disk-2", 7.0)
        playback_file = Path(self._tmpdir) / "playback_stats.json"
        self.assertTrue(playback_file.exists(), "File must be written when privacy is off")
        import json
        data = json.loads(playback_file.read_text())
        self.assertIn("item-disk-2", data)


# ---------------------------------------------------------------------------
# handle_record_playback IPC shim — returns privacy no-op correctly
# ---------------------------------------------------------------------------

class TestHandleRecordPlaybackIPC(unittest.TestCase):

    def test_ipc_returns_noop_when_privacy_on(self) -> None:
        tracker = PlaybackTracker(privacy_mode_fn=lambda: True)
        result = tracker.handle_record_playback({"item_id": "abc", "duration_listened_sec": 5.0})
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_ipc_returns_stats_when_privacy_off(self) -> None:
        tracker = PlaybackTracker(privacy_mode_fn=lambda: False)
        result = tracker.handle_record_playback({"item_id": "xyz", "duration_listened_sec": 3.0})
        self.assertEqual(result["item_id"], "xyz")
        self.assertEqual(result["play_count"], 1)

    def test_ipc_empty_item_id_raises(self) -> None:
        tracker = PlaybackTracker()
        with self.assertRaises(ValueError):
            tracker.handle_record_playback({"item_id": ""})


if __name__ == "__main__":
    unittest.main()
