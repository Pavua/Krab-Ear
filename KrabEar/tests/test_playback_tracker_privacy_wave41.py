"""Тесты privacy-gate для IPC read-обработчиков PlaybackTracker (wave-41 HIGH).

handle_get_playback_stats / handle_get_most_replayed / handle_get_never_played
должны возвращать пустые/нулевые ответы при privacy_mode=True.
WRITE-путь (record_playback) остаётся gated — проверяется как регрессия.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.playback_tracker import PlaybackTracker  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str):
        self._id = item_id

    def to_dict(self) -> dict:
        return {"id": self._id, "text": f"текст {self._id}"}


class FakeStore:
    def __init__(self, items: list[str]):
        self._items = [FakeHistoryItem(iid) for iid in items]

    def get_history_page_filtered(self, cursor=None, limit=50, **_):
        start = int(cursor) if cursor is not None else 0
        end = start + limit
        page = self._items[start:end]
        next_cursor = str(end) if end < len(self._items) else None
        return page, next_cursor


def _tracker_with_privacy(enabled: bool) -> PlaybackTracker:
    """Returns an in-memory PlaybackTracker with privacy mode controlled by a flag."""
    flag = [enabled]
    tracker = PlaybackTracker(privacy_mode_fn=lambda: flag[0])
    # Pre-populate some data so we can verify gating hides it.
    # Temporarily disable privacy so record_playback actually records.
    flag[0] = False
    tracker.record_playback("item-A", duration_listened_sec=5.0)
    tracker.record_playback("item-A", duration_listened_sec=3.0)
    tracker.record_playback("item-B", duration_listened_sec=10.0)
    flag[0] = enabled
    return tracker


# ===========================================================================
# Test class
# ===========================================================================

class TestPlaybackTrackerPrivacyGate(unittest.TestCase):
    """Privacy gate for IPC read handlers (wave-41 HIGH)."""

    # ------------------------------------------------------------------
    # handle_get_playback_stats
    # ------------------------------------------------------------------

    def test_get_playback_stats_privacy_on_returns_empty(self):
        """handle_get_playback_stats must return zeroed schema-parity dict in privacy mode."""
        tracker = _tracker_with_privacy(enabled=True)
        result = tracker.handle_get_playback_stats({"item_id": "item-A"})
        self.assertEqual(result["play_count"], 0)
        self.assertEqual(result["total_listened_sec"], 0.0)
        self.assertIsNone(result["last_played"])

    def test_get_playback_stats_privacy_off_returns_data(self):
        """handle_get_playback_stats must return real data when privacy mode is off."""
        tracker = _tracker_with_privacy(enabled=False)
        result = tracker.handle_get_playback_stats({"item_id": "item-A"})
        self.assertEqual(result["play_count"], 2)
        self.assertGreater(result["total_listened_sec"], 0.0)

    def test_get_playback_stats_privacy_on_schema_parity(self):
        """Returned dict must have the expected keys even in privacy mode."""
        tracker = _tracker_with_privacy(enabled=True)
        result = tracker.handle_get_playback_stats({"item_id": "x"})
        for key in ("play_count", "total_listened_sec", "last_played"):
            self.assertIn(key, result, f"Missing key {key!r} in privacy-mode response")

    # ------------------------------------------------------------------
    # handle_get_most_replayed
    # ------------------------------------------------------------------

    def test_get_most_replayed_privacy_on_returns_empty_list(self):
        """handle_get_most_replayed must return empty items list in privacy mode."""
        tracker = _tracker_with_privacy(enabled=True)
        result = tracker.handle_get_most_replayed({"limit": 10})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_get_most_replayed_privacy_off_returns_data(self):
        """handle_get_most_replayed returns real items when privacy mode is off."""
        tracker = _tracker_with_privacy(enabled=False)
        result = tracker.handle_get_most_replayed({"limit": 10})
        self.assertGreater(result["count"], 0)
        self.assertGreater(len(result["items"]), 0)

    # ------------------------------------------------------------------
    # handle_get_never_played
    # ------------------------------------------------------------------

    def test_get_never_played_privacy_on_returns_empty_list(self):
        """handle_get_never_played must return empty items list in privacy mode."""
        store = FakeStore(["item-C", "item-D"])
        tracker = _tracker_with_privacy(enabled=True)
        result = tracker.handle_get_never_played({"limit": 50}, store=store)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_get_never_played_privacy_off_returns_data(self):
        """handle_get_never_played returns real items when privacy mode is off."""
        store = FakeStore(["item-E", "item-F"])
        tracker = _tracker_with_privacy(enabled=False)
        result = tracker.handle_get_never_played({"limit": 50}, store=store)
        self.assertGreater(result["count"], 0)

    # ------------------------------------------------------------------
    # Regression: write path (record_playback) still gated
    # ------------------------------------------------------------------

    def test_record_playback_privacy_on_no_op(self):
        """record_playback must remain a no-op when privacy mode is on (regression guard)."""
        tracker = _tracker_with_privacy(enabled=True)
        res = tracker.handle_record_playback({"item_id": "new-item", "duration_listened_sec": 5.0})
        # Should return privacy no-op marker, not stats
        self.assertEqual(res.get("reason"), "privacy_mode_active")
        # Confirm nothing was recorded
        stats = tracker.get_playback_stats("new-item")
        self.assertEqual(stats["play_count"], 0)

    # ------------------------------------------------------------------
    # Dynamic toggle: privacy_mode_fn is evaluated at call time
    # ------------------------------------------------------------------

    def test_privacy_gate_dynamic_toggle(self):
        """Privacy gate responds to runtime toggle without re-construction."""
        flag = [False]
        tracker = PlaybackTracker(privacy_mode_fn=lambda: flag[0])
        tracker.record_playback("dyn-item", duration_listened_sec=2.0)

        # Privacy off → data visible
        result_off = tracker.handle_get_playback_stats({"item_id": "dyn-item"})
        self.assertEqual(result_off["play_count"], 1)

        # Enable privacy at runtime
        flag[0] = True
        result_on = tracker.handle_get_playback_stats({"item_id": "dyn-item"})
        self.assertEqual(result_on["play_count"], 0)

        # Disable again → data re-visible
        flag[0] = False
        result_back = tracker.handle_get_playback_stats({"item_id": "dyn-item"})
        self.assertEqual(result_back["play_count"], 1)


if __name__ == "__main__":
    unittest.main()
