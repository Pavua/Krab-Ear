"""Тесты для BookmarkManager (backend/bookmarks.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.bookmarks import BookmarkManager  # noqa: E402


class TestBookmarkManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bm = BookmarkManager(data_dir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Basic add
    # ------------------------------------------------------------------

    def test_add_returns_correct_fields(self):
        bm = self.bm.add(session_id="sess1", offset_sec=12.5, note="важно")
        self.assertEqual(bm["session_id"], "sess1")
        self.assertAlmostEqual(bm["offset_sec"], 12.5)
        self.assertEqual(bm["note"], "важно")
        self.assertIn("id", bm)
        self.assertIn("ts", bm)
        self.assertFalse(bm["deleted"])

    def test_add_empty_session_id_uses_live_placeholder(self):
        bm = self.bm.add(session_id="", offset_sec=0.0)
        self.assertEqual(bm["session_id"], "__live__")

    def test_add_rounds_offset_to_3_decimal_places(self):
        bm = self.bm.add(session_id="s", offset_sec=1.23456789)
        self.assertEqual(bm["offset_sec"], 1.235)

    # ------------------------------------------------------------------
    # list_for_item
    # ------------------------------------------------------------------

    def test_list_for_item_filters_by_session(self):
        self.bm.add("session_A", 10.0, "first")
        self.bm.add("session_B", 5.0, "other")
        self.bm.add("session_A", 3.0, "second")

        results = self.bm.list_for_item("session_A")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(b["session_id"] == "session_A" for b in results))

    def test_list_for_item_sorted_by_offset(self):
        self.bm.add("sess", 30.0)
        self.bm.add("sess", 5.0)
        self.bm.add("sess", 15.0)

        results = self.bm.list_for_item("sess")
        offsets = [b["offset_sec"] for b in results]
        self.assertEqual(offsets, sorted(offsets))

    def test_list_for_item_returns_empty_for_unknown_session(self):
        self.bm.add("sess_known", 1.0)
        results = self.bm.list_for_item("sess_unknown")
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # delete (tombstone)
    # ------------------------------------------------------------------

    def test_delete_removes_bookmark(self):
        bm = self.bm.add("sess", 10.0, "to delete")
        bid = bm["id"]

        result = self.bm.delete(bid)
        self.assertTrue(result)

        remaining = self.bm.list_for_item("sess")
        self.assertEqual(remaining, [])

    def test_delete_nonexistent_returns_false(self):
        result = self.bm.delete("nonexistent-id")
        self.assertFalse(result)

    def test_delete_tombstone_written_to_journal(self):
        bm = self.bm.add("sess", 5.0)
        self.bm.delete(bm["id"])

        journal_path = Path(self._tmp.name) / "bookmarks.ndjson"
        lines = [json.loads(ln) for ln in journal_path.read_text().splitlines() if ln.strip()]
        # Должны быть оригинальная запись + tombstone
        self.assertEqual(len(lines), 2)
        tombstone = lines[-1]
        self.assertEqual(tombstone["id"], bm["id"])
        self.assertTrue(tombstone["deleted"])

    # ------------------------------------------------------------------
    # Persistence across reload
    # ------------------------------------------------------------------

    def test_bookmarks_persist_across_reload(self):
        self.bm.add("sess_persist", 42.0, "persistent note")

        # Создаём новый экземпляр поверх того же data_dir
        bm2 = BookmarkManager(data_dir=Path(self._tmp.name))
        results = bm2.list_for_item("sess_persist")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["note"], "persistent note")

    def test_deleted_bookmarks_not_visible_after_reload(self):
        bm = self.bm.add("sess", 1.0, "will be deleted")
        self.bm.delete(bm["id"])

        bm2 = BookmarkManager(data_dir=Path(self._tmp.name))
        results = bm2.list_for_item("sess")
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # update_session_id
    # ------------------------------------------------------------------

    def test_update_session_id_migrates_live_bookmarks(self):
        self.bm.add("__live__", 5.0, "live mark")
        self.bm.add("__live__", 10.0, "another")

        count = self.bm.update_session_id("__live__", "real-item-id")
        self.assertEqual(count, 2)

        results = self.bm.list_for_item("real-item-id")
        self.assertEqual(len(results), 2)

        live_results = self.bm.list_for_item("__live__")
        self.assertEqual(live_results, [])

    def test_update_session_id_no_match_returns_zero(self):
        count = self.bm.update_session_id("nonexistent", "new-id")
        self.assertEqual(count, 0)

    # ------------------------------------------------------------------
    # list_all
    # ------------------------------------------------------------------

    def test_list_all_returns_all_active(self):
        self.bm.add("s1", 1.0)
        bm2 = self.bm.add("s2", 2.0)
        self.bm.add("s3", 3.0)
        self.bm.delete(bm2["id"])

        all_bm = self.bm.list_all()
        self.assertEqual(len(all_bm), 2)
        ids = {b["id"] for b in all_bm}
        self.assertNotIn(bm2["id"], ids)

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def test_handle_add_bookmark_success(self):
        result = self.bm.handle_add_bookmark({
            "session_id": "ipc-sess",
            "offset_sec": 7.5,
            "note": "ipc note",
        })
        self.assertIn("bookmark", result)
        bm = result["bookmark"]
        self.assertEqual(bm["session_id"], "ipc-sess")
        self.assertAlmostEqual(bm["offset_sec"], 7.5)

    def test_handle_add_bookmark_missing_offset_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_add_bookmark({"session_id": "s"})

    def test_handle_add_bookmark_negative_offset_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_add_bookmark({"session_id": "s", "offset_sec": -1.0})

    def test_handle_list_bookmarks_success(self):
        self.bm.add("item-123", 3.0, "note")
        result = self.bm.handle_list_bookmarks({"item_id": "item-123"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["bookmarks"]), 1)

    def test_handle_list_bookmarks_missing_item_id_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_list_bookmarks({})

    def test_handle_delete_bookmark_success(self):
        bm = self.bm.add("sess", 1.0)
        result = self.bm.handle_delete_bookmark({"id": bm["id"]})
        self.assertTrue(result["ok"])

    def test_handle_delete_bookmark_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_delete_bookmark({})

    def test_handle_jump_to_bookmark_success(self):
        bm = self.bm.add("sess", 42.0, "jump here")
        result = self.bm.handle_jump_to_bookmark({"id": bm["id"]})
        self.assertIn("bookmark", result)
        self.assertIn("seek_to_sec", result)
        self.assertAlmostEqual(result["seek_to_sec"], 42.0)

    def test_handle_jump_to_bookmark_not_found_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_jump_to_bookmark({"id": "nonexistent"})

    def test_handle_jump_to_bookmark_missing_id_raises(self):
        with self.assertRaises(ValueError):
            self.bm.handle_jump_to_bookmark({})

    def test_handle_list_all_bookmarks(self):
        self.bm.add("s1", 1.0)
        self.bm.add("s2", 2.0)
        result = self.bm.handle_list_all_bookmarks({})
        self.assertEqual(result["count"], 2)
        self.assertEqual(len(result["bookmarks"]), 2)


if __name__ == "__main__":
    unittest.main()
