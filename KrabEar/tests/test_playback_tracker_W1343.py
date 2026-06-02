"""Tests for W1343 fixes in PlaybackTracker and HistoryService.

F1 HIGH: atomic save (tmp+fsync+rename)
F3 MED:  privacy_mode_enabled gate in record_playback
F4 LOW:  remove_stats + cascade delete wired from HistoryService.handle_delete_history_item
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup for standalone / unittest execution
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.playback_tracker import PlaybackTracker  # noqa: E402


# ===========================================================================
# F1 — atomic write (tmp+fsync+rename)
# ===========================================================================

class TestAtomicSave(unittest.TestCase):
    """F1 HIGH: _save() must use atomic tmp→rename pattern."""

    def test_save_atomic_via_tmp_rename(self):
        """_save() writes to a tmp file then renames — final file must be valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            tracker.record_playback("item_atomic", duration_listened_sec=5.0)

            path = Path(tmpdir) / "playback_stats.json"
            self.assertTrue(path.exists(), "playback_stats.json not created")

            # File must be valid JSON — no partial write remnants.
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("item_atomic", data)
            self.assertEqual(data["item_atomic"]["play_count"], 1)

            # No tmp files left behind after successful write.
            tmp_leftovers = list(Path(tmpdir).glob(".playback_stats_tmp_*.json"))
            self.assertEqual(tmp_leftovers, [], f"Stale tmp files: {tmp_leftovers}")

    def test_save_survives_partial_write_crash(self):
        """Simulate a crash mid-write: existing data must stay intact."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            tracker.record_playback("safe_item", duration_listened_sec=10.0)

            path = Path(tmpdir) / "playback_stats.json"
            original_data = path.read_text(encoding="utf-8")

            # Simulate crash: inject an OSError during os.replace so the tmp
            # file never replaces the original.
            with patch("os.replace", side_effect=OSError("simulated crash")):
                # Should not raise — exception is swallowed internally.
                tracker.record_playback("safe_item", duration_listened_sec=3.0)

            # Original file must be intact (untouched by the failed write).
            surviving_data = path.read_text(encoding="utf-8")
            self.assertEqual(surviving_data, original_data,
                             "Original file was corrupted by failed atomic write")

    def test_atomic_save_uses_os_replace(self):
        """Verify _save() calls os.replace (not Path.write_text directly)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir)
            replace_calls = []
            real_replace = os.replace

            def capture_replace(src, dst):
                replace_calls.append((src, dst))
                return real_replace(src, dst)

            with patch("os.replace", side_effect=capture_replace):
                tracker.record_playback("verify_replace", duration_listened_sec=1.0)

            self.assertGreater(len(replace_calls), 0,
                               "os.replace was never called — not using atomic pattern")
            # Destination must be the playback_stats.json path.
            final_dst = str(Path(tmpdir) / "playback_stats.json")
            self.assertTrue(
                any(str(dst) == final_dst for _, dst in replace_calls),
                f"os.replace dst was not playback_stats.json; calls={replace_calls}",
            )


# ===========================================================================
# F3 — privacy mode gate
# ===========================================================================

class TestPrivacyModeGate(unittest.TestCase):
    """F3 MED: record_playback must be a no-op when privacy_mode_enabled=True."""

    def test_record_playback_skips_in_privacy_mode(self):
        """record_playback() must not write anything when privacy_mode=True."""
        tracker = PlaybackTracker(privacy_mode_enabled=True)
        tracker.record_playback("secret_item", duration_listened_sec=30.0)
        stats = tracker.get_playback_stats("secret_item")
        self.assertEqual(stats["play_count"], 0,
                         "play_count must stay 0 in privacy mode")
        self.assertEqual(stats["total_listened_sec"], 0.0)
        self.assertIsNone(stats["last_played"])

    def test_privacy_mode_skips_disk_write(self):
        """In privacy mode, no file must be written to disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = PlaybackTracker(data_dir=tmpdir, privacy_mode_enabled=True)
            tracker.record_playback("disk_skip", duration_listened_sec=5.0)

            path = Path(tmpdir) / "playback_stats.json"
            # File might be created empty by __init__ loading, but should NOT
            # contain the recorded item.
            if path.exists():
                raw = path.read_text(encoding="utf-8")
                if raw.strip():
                    data = json.loads(raw)
                    self.assertNotIn("disk_skip", data,
                                     "Privacy mode must not persist playback events")

    def test_privacy_mode_false_records_normally(self):
        """When privacy_mode=False (default), recording works as expected."""
        tracker = PlaybackTracker(privacy_mode_enabled=False)
        tracker.record_playback("normal_item", duration_listened_sec=7.0)
        stats = tracker.get_playback_stats("normal_item")
        self.assertEqual(stats["play_count"], 1)
        self.assertAlmostEqual(stats["total_listened_sec"], 7.0)

    def test_set_privacy_mode_toggles_behaviour(self):
        """set_privacy_mode() must toggle the gate at runtime."""
        tracker = PlaybackTracker()
        tracker.record_playback("pre_privacy", duration_listened_sec=3.0)
        self.assertEqual(tracker.get_playback_stats("pre_privacy")["play_count"], 1)

        tracker.set_privacy_mode(True)
        tracker.record_playback("pre_privacy", duration_listened_sec=3.0)
        # Still 1 — second call was silently dropped.
        self.assertEqual(tracker.get_playback_stats("pre_privacy")["play_count"], 1)

        tracker.set_privacy_mode(False)
        tracker.record_playback("pre_privacy", duration_listened_sec=3.0)
        # Now 2 — privacy off again.
        self.assertEqual(tracker.get_playback_stats("pre_privacy")["play_count"], 2)

    def test_privacy_mode_invalid_item_id_still_raises(self):
        """Even in privacy mode, empty item_id must raise ValueError."""
        tracker = PlaybackTracker(privacy_mode_enabled=True)
        with self.assertRaises(ValueError):
            tracker.record_playback("", duration_listened_sec=1.0)


# ===========================================================================
# F4 — remove_stats + cascade wiring
# ===========================================================================

class TestRemoveStats(unittest.TestCase):
    """F4 LOW: remove_stats() removes orphan keys; HistoryService wires it."""

    def test_remove_stats_removes_existing_key(self):
        """remove_stats() deletes the key and returns True."""
        tracker = PlaybackTracker()
        tracker.record_playback("del_me", duration_listened_sec=5.0)
        self.assertEqual(tracker.get_playback_stats("del_me")["play_count"], 1)

        result = tracker.remove_stats("del_me")
        self.assertTrue(result, "remove_stats should return True for existing key")
        self.assertEqual(tracker.get_playback_stats("del_me")["play_count"], 0)

    def test_remove_stats_returns_false_for_missing_key(self):
        """remove_stats() returns False when item has no stats entry."""
        tracker = PlaybackTracker()
        result = tracker.remove_stats("nonexistent_item")
        self.assertFalse(result)

    def test_remove_stats_persists_deletion(self):
        """After remove_stats(), the key must be absent from disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            t1 = PlaybackTracker(data_dir=tmpdir)
            t1.record_playback("persist_del", duration_listened_sec=8.0)
            t1.remove_stats("persist_del")

            path = Path(tmpdir) / "playback_stats.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("persist_del", data)

    def test_remove_stats_empty_id_returns_false(self):
        """remove_stats() with empty/whitespace id returns False without error."""
        tracker = PlaybackTracker()
        self.assertFalse(tracker.remove_stats(""))
        self.assertFalse(tracker.remove_stats("   "))

    def test_remove_stats_only_affects_target_key(self):
        """Removing one key must not disturb others."""
        tracker = PlaybackTracker()
        tracker.record_playback("keep", duration_listened_sec=10.0)
        tracker.record_playback("remove", duration_listened_sec=5.0)

        tracker.remove_stats("remove")
        self.assertEqual(tracker.get_playback_stats("keep")["play_count"], 1)
        self.assertEqual(tracker.get_playback_stats("remove")["play_count"], 0)

    def test_remove_stats_called_on_delete_history_item(self):
        """HistoryService.handle_delete_history_item must call playback_tracker.remove_stats."""
        from backend.history_service import HistoryService

        # Minimal fake store that reports successful deletion.
        mock_store = MagicMock()
        mock_store.delete_history_item.return_value = True
        mock_store.data_dir = Path(tempfile.mkdtemp())
        _lock_obj = threading.RLock()
        mock_store._lock = MagicMock(return_value=_lock_obj)

        # Fake PlaybackTracker with spy on remove_stats.
        mock_tracker = MagicMock(spec=PlaybackTracker)

        svc = HistoryService(
            store=mock_store,
            playback_tracker=mock_tracker,
        )
        svc.handle_delete_history_item({"id": "item_to_delete"})

        mock_tracker.remove_stats.assert_called_once_with("item_to_delete")

    def test_delete_without_tracker_does_not_crash(self):
        """handle_delete_history_item works fine when no playback_tracker injected."""
        from backend.history_service import HistoryService

        mock_store = MagicMock()
        mock_store.delete_history_item.return_value = True
        mock_store.data_dir = Path(tempfile.mkdtemp())
        _lock_obj2 = threading.RLock()
        mock_store._lock = MagicMock(return_value=_lock_obj2)

        svc = HistoryService(store=mock_store)  # no playback_tracker
        result = svc.handle_delete_history_item({"id": "some_id"})
        self.assertTrue(result.get("deleted"))


if __name__ == "__main__":
    unittest.main()
