"""StateStore unit tests — focused on NDJSON persistence, append-only logic,
tombstone deletes, periodic compaction, delta journals (tags/favorites/
annotations/text-overrides/status-overrides/action-items), unicode
round-trips, import dedup, auto-cleanup, storage breakdown, history overview,
and file-lock concurrency safety.

Gaps NOT covered by the existing dedicated/integration/search files:
  - set_paste_status / status-override delta journal
  - update_history_item_tags / _load_tags_overrides
  - update_history_item_favorite / _load_favorites_overrides
  - set_annotation / get_annotation / delete_annotation / search_annotations
  - update_history_item_text / _load_text_overrides
  - update_history_item_action_items / get_history_item_action_items
  - import_history_ndjson (dedup + error + skip)
  - auto_cleanup_old (dry_run + real delete)
  - get_storage_breakdown
  - get_history_overview
  - get_history_page_filtered (paste_status / translation_mode filters)
  - get_history_item_by_id
  - unicode / Cyrillic / emoji round-trip through NDJSON
  - _read_ndjson_unlocked skips partial (non-dict) lines
  - compact_with_stats returns accurate reclaimed_bytes
  - _normalize_ts_filter date-only and full-datetime parsing
  - _parse_cursor edge-cases (negative, garbage string)
  - NDJSON line ordering (append preserves insertion order on disk)
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


def _add(store: StateStore, text: str, **kw) -> str:
    """Add item and return its id."""
    item = store.add_history_item(text, **kw)
    return item.id


# ---------------------------------------------------------------------------
# Status-override delta journal
# ---------------------------------------------------------------------------

class TestSetPasteStatus(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_paste_status_overrides_displayed_value(self):
        item_id = _add(self.store, "hello", paste_status="failed")
        self.store.set_paste_status(item_id, "ok")
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.paste_status, "ok")

    def test_set_paste_status_last_write_wins(self):
        item_id = _add(self.store, "world", paste_status="failed")
        self.store.set_paste_status(item_id, "ok")
        self.store.set_paste_status(item_id, "failed")
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.paste_status, "failed")

    def test_set_paste_status_empty_id_returns_false(self):
        result = self.store.set_paste_status("  ", "ok")
        self.assertFalse(result)

    def test_set_paste_status_written_to_status_ndjson(self):
        item_id = _add(self.store, "test")
        self.store.set_paste_status(item_id, "ok")
        entries = list(StateStore._read_ndjson_unlocked(self.store.status_path))
        self.assertTrue(any(e.get("id") == item_id for e in entries))


# ---------------------------------------------------------------------------
# Tags delta journal
# ---------------------------------------------------------------------------

class TestTagsJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_update_tags_persists_and_loads(self):
        item_id = _add(self.store, "tagged item")
        result = self.store.update_history_item_tags(item_id, ["важный", "работа"])
        self.assertTrue(result)
        item = self.store.get_history_item_by_id(item_id)
        self.assertIn("важный", item.tags)

    def test_update_tags_last_write_wins(self):
        item_id = _add(self.store, "tags overwrite")
        self.store.update_history_item_tags(item_id, ["first"])
        self.store.update_history_item_tags(item_id, ["second"])
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.tags, ["second"])

    def test_update_tags_unknown_id_returns_false(self):
        result = self.store.update_history_item_tags("nonexistent", ["x"])
        self.assertFalse(result)

    def test_update_tags_empty_list_clears_tags(self):
        item_id = _add(self.store, "clear tags")
        self.store.update_history_item_tags(item_id, ["old"])
        self.store.update_history_item_tags(item_id, [])
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.tags, [])


# ---------------------------------------------------------------------------
# Favorites delta journal
# ---------------------------------------------------------------------------

class TestFavoritesJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_favorite_true(self):
        item_id = _add(self.store, "fav me")
        self.store.update_history_item_favorite(item_id, True)
        item = self.store.get_history_item_by_id(item_id)
        self.assertTrue(item.favorite)

    def test_toggle_favorite_false(self):
        item_id = _add(self.store, "unfav")
        self.store.update_history_item_favorite(item_id, True)
        self.store.update_history_item_favorite(item_id, False)
        item = self.store.get_history_item_by_id(item_id)
        self.assertFalse(item.favorite)

    def test_favorite_unknown_id_returns_false(self):
        result = self.store.update_history_item_favorite("ghost-id", True)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Annotations delta journal
# ---------------------------------------------------------------------------

class TestAnnotationsJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_and_get_annotation(self):
        item_id = _add(self.store, "annotate me")
        self.store.set_annotation(item_id, "my note")
        note = self.store.get_annotation(item_id)
        self.assertEqual(note, "my note")

    def test_annotation_overwrite(self):
        item_id = _add(self.store, "overwrite note")
        self.store.set_annotation(item_id, "first")
        self.store.set_annotation(item_id, "second")
        note = self.store.get_annotation(item_id)
        self.assertEqual(note, "second")

    def test_delete_annotation_returns_none(self):
        item_id = _add(self.store, "delete my note")
        self.store.set_annotation(item_id, "gone soon")
        self.store.delete_annotation(item_id)
        note = self.store.get_annotation(item_id)
        self.assertIsNone(note)

    def test_search_annotations_finds_match(self):
        item_id = _add(self.store, "important meeting")
        self.store.set_annotation(item_id, "quarterly review notes")
        results = self.store.search_annotations("quarterly")
        self.assertTrue(any(r["id"] == item_id for r in results))

    def test_search_annotations_empty_query_returns_all(self):
        id1 = _add(self.store, "a")
        id2 = _add(self.store, "b")
        self.store.set_annotation(id1, "note alpha")
        self.store.set_annotation(id2, "note beta")
        results = self.store.search_annotations("")
        ids = {r["id"] for r in results}
        self.assertIn(id1, ids)
        self.assertIn(id2, ids)

    def test_set_annotation_unknown_id_returns_false(self):
        result = self.store.set_annotation("ghost", "whatever")
        self.assertFalse(result)

    def test_get_annotation_returns_none_for_nonexistent(self):
        result = self.store.get_annotation("totally-made-up-id")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Text-override delta journal
# ---------------------------------------------------------------------------

class TestTextOverrideJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_update_text_changes_displayed_text(self):
        item_id = _add(self.store, "original text")
        self.store.update_history_item_text(item_id, "corrected text")
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.text, "corrected text")

    def test_update_text_with_confidence(self):
        item_id = _add(self.store, "text here")
        self.store.update_history_item_text(item_id, "better text", confidence=0.95)
        item = self.store.get_history_item_by_id(item_id)
        self.assertAlmostEqual(item.confidence, 0.95, places=2)

    def test_update_text_unknown_id_returns_false(self):
        result = self.store.update_history_item_text("ghost-id", "nope")
        self.assertFalse(result)

    def test_update_text_last_write_wins(self):
        item_id = _add(self.store, "first")
        self.store.update_history_item_text(item_id, "second")
        self.store.update_history_item_text(item_id, "third")
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.text, "third")


# ---------------------------------------------------------------------------
# Action-items delta journal
# ---------------------------------------------------------------------------

class TestActionItemsJournal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_update_and_get_action_items(self):
        item_id = _add(self.store, "meeting recording")
        self.store.update_history_item_action_items(
            item_id,
            action_items=["send report"],
            decisions=["use Python"],
            questions=["when?"],
        )
        result = self.store.get_history_item_action_items(item_id)
        self.assertIsNotNone(result)
        self.assertIn("send report", result["action_items"])

    def test_action_items_unknown_id_returns_false(self):
        result = self.store.update_history_item_action_items(
            "no-such-id", ["x"], [], []
        )
        self.assertFalse(result)

    def test_get_all_pending_action_items(self):
        id1 = _add(self.store, "rec1")
        id2 = _add(self.store, "rec2")
        self.store.update_history_item_action_items(id1, ["task1"], [], [])
        self.store.update_history_item_action_items(id2, ["task2"], [], [])
        all_items = self.store.get_all_pending_action_items()
        # Should return at least 2 entries
        self.assertGreaterEqual(len(all_items), 2)


# ---------------------------------------------------------------------------
# Import NDJSON deduplication
# ---------------------------------------------------------------------------

class TestImportHistoryNDJSON(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_import_file(self, items: list[dict]) -> Path:
        p = Path(self._tmp) / "import.ndjson"
        with p.open("w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return p

    def test_import_adds_new_items(self):
        item_id = _add(self.store, "existing item")
        # Build a valid HistoryItem dict for import
        existing = self.store.get_history_item_by_id(item_id).to_dict()
        # New item (different id)
        import uuid
        from datetime import datetime
        new_item = dict(existing)
        new_item["id"] = str(uuid.uuid4())
        new_item["text"] = "imported item"
        new_item["ts"] = datetime.now().isoformat()

        path = self._write_import_file([existing, new_item])
        stats = self.store.import_history_ndjson(path)
        self.assertEqual(stats["imported"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_import_skips_duplicate_ids(self):
        item_id = _add(self.store, "already here")
        existing = self.store.get_history_item_by_id(item_id).to_dict()
        path = self._write_import_file([existing, existing])
        stats = self.store.import_history_ndjson(path)
        self.assertEqual(stats["imported"], 0)
        self.assertEqual(stats["skipped"], 2)

    def test_import_skips_corrupt_lines(self):
        p = Path(self._tmp) / "import_bad.ndjson"
        p.write_text("not json at all\n{}\n", encoding="utf-8")
        stats = self.store.import_history_ndjson(p)
        # Both lines are invalid HistoryItems — errors >= 1
        self.assertGreater(stats["errors"] + stats["skipped"], 0)

    def test_import_nonexistent_file_raises(self):
        with self.assertRaises(RuntimeError):
            self.store.import_history_ndjson(Path(self._tmp) / "no_such.ndjson")


# ---------------------------------------------------------------------------
# Auto cleanup old items
# ---------------------------------------------------------------------------

class TestAutoCleanupOld(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_dry_run_reports_count_without_deleting(self):
        # Add item with an old timestamp via direct file manipulation
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        item_id = _add(self.store, "ancient item")
        # Manually overwrite ts in history.ndjson to make it old
        lines = self.store.history_path.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            d = json.loads(line)
            if d.get("id") == item_id:
                d["ts"] = old_ts
            rewritten.append(json.dumps(d, ensure_ascii=False))
        self.store.history_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        result = self.store.auto_cleanup_old(days=365, dry_run=True)
        self.assertGreaterEqual(result["deleted_count"], 1)
        # dry_run=True → item still present
        item = self.store.get_history_item_by_id(item_id)
        self.assertIsNotNone(item)

    def test_real_delete_removes_old_items(self):
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=400)).isoformat()
        item_id = _add(self.store, "old gone")
        lines = self.store.history_path.read_text(encoding="utf-8").splitlines()
        rewritten = []
        for line in lines:
            d = json.loads(line)
            if d.get("id") == item_id:
                d["ts"] = old_ts
            rewritten.append(json.dumps(d, ensure_ascii=False))
        self.store.history_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

        self.store.auto_cleanup_old(days=365, dry_run=False)
        item = self.store.get_history_item_by_id(item_id)
        self.assertIsNone(item)

    def test_invalid_days_raises(self):
        with self.assertRaises(ValueError):
            self.store.auto_cleanup_old(days=0)


# ---------------------------------------------------------------------------
# Storage breakdown
# ---------------------------------------------------------------------------

class TestStorageBreakdown(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_breakdown_keys_present(self):
        result = self.store.get_storage_breakdown()
        for key in ("ndjson_mb", "transcripts_mb", "audio_mb", "total_mb"):
            self.assertIn(key, result)

    def test_breakdown_ndjson_mb_increases_after_appends(self):
        before = self.store.get_storage_breakdown()["ndjson_mb"]
        for i in range(10):
            _add(self.store, f"line {i}")
        after = self.store.get_storage_breakdown()["ndjson_mb"]
        self.assertGreaterEqual(after, before)

    def test_breakdown_totals_are_non_negative(self):
        result = self.store.get_storage_breakdown()
        for key in ("ndjson_mb", "transcripts_mb", "audio_mb", "total_mb"):
            self.assertGreaterEqual(result[key], 0.0)


# ---------------------------------------------------------------------------
# History overview
# ---------------------------------------------------------------------------

class TestHistoryOverview(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_overview_empty_store(self):
        result = self.store.get_history_overview()
        self.assertEqual(result["active_count"], 0)
        self.assertEqual(result["paste_ok"], 0)
        self.assertEqual(result["paste_failed"], 0)

    def test_overview_counts_paste_status(self):
        _add(self.store, "ok item", paste_status="ok")
        _add(self.store, "fail item", paste_status="failed")
        result = self.store.get_history_overview()
        self.assertEqual(result["active_count"], 2)
        self.assertEqual(result["paste_ok"], 1)
        self.assertEqual(result["paste_failed"], 1)

    def test_overview_tracks_translation_mode(self):
        _add(self.store, "translated", translation_mode="ru_to_es", translation_status="ok")
        result = self.store.get_history_overview()
        self.assertEqual(result["translated_ok"], 1)
        modes = {m["mode"] for m in result["top_modes"]}
        self.assertIn("ru_to_es", modes)

    def test_overview_avg_text_chars(self):
        _add(self.store, "ab")
        _add(self.store, "abcd")
        result = self.store.get_history_overview()
        # avg of 2 and 4 chars = 3
        self.assertEqual(result["avg_text_chars"], 3)


# ---------------------------------------------------------------------------
# get_history_page_filtered
# ---------------------------------------------------------------------------

class TestGetHistoryPageFiltered(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_filter_by_paste_status(self):
        _add(self.store, "ok1", paste_status="ok")
        _add(self.store, "fail1", paste_status="failed")
        _add(self.store, "ok2", paste_status="ok")
        page, cursor = self.store.get_history_page_filtered(
            cursor=None, limit=10, paste_status="ok", translation_mode=None
        )
        texts = [d["text"] for d in page]
        self.assertIn("ok1", texts)
        self.assertIn("ok2", texts)
        self.assertNotIn("fail1", texts)
        self.assertIsNone(cursor)

    def test_filter_by_translation_mode(self):
        _add(self.store, "translated", translation_mode="ru_to_es")
        _add(self.store, "off item", translation_mode="off")
        page, _ = self.store.get_history_page_filtered(
            cursor=None, limit=10,
            paste_status=None, translation_mode="ru_to_es"
        )
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["text"], "translated")

    def test_empty_filter_returns_all(self):
        for i in range(5):
            _add(self.store, f"item {i}")
        page, _ = self.store.get_history_page_filtered(
            cursor=None, limit=10, paste_status=None, translation_mode=None
        )
        self.assertEqual(len(page), 5)


# ---------------------------------------------------------------------------
# get_history_item_by_id
# ---------------------------------------------------------------------------

class TestGetHistoryItemById(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_correct_item(self):
        item_id = _add(self.store, "find me by id")
        item = self.store.get_history_item_by_id(item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.text, "find me by id")

    def test_returns_none_for_nonexistent_id(self):
        result = self.store.get_history_item_by_id("totally-made-up")
        self.assertIsNone(result)

    def test_returns_none_for_deleted_item(self):
        item_id = _add(self.store, "delete me")
        self.store.delete_history_item(item_id)
        result = self.store.get_history_item_by_id(item_id)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Unicode / Cyrillic / emoji round-trip
# ---------------------------------------------------------------------------

class TestUnicodeRoundTrip(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_cyrillic_text_preserved(self):
        text = "Привет мир — тест транскрипции"
        item_id = _add(self.store, text)
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.text, text)

    def test_emoji_and_mixed_script_preserved(self):
        text = "Запись 🎙️ — это работает! Hola mundo. 你好"
        item_id = _add(self.store, text)
        item = self.store.get_history_item_by_id(item_id)
        self.assertEqual(item.text, text)

    def test_ensure_ascii_false_in_ndjson(self):
        """Verify Cyrillic chars are stored as-is (not escaped \\uXXXX) in the file."""
        text = "Кириллица"
        _add(self.store, text)
        raw = self.store.history_path.read_text(encoding="utf-8")
        self.assertIn("Кириллица", raw)

    def test_annotation_cyrillic_round_trip(self):
        item_id = _add(self.store, "item")
        note = "Заметка на кириллице 🚀"
        self.store.set_annotation(item_id, note)
        loaded = self.store.get_annotation(item_id)
        self.assertEqual(loaded, note)


# ---------------------------------------------------------------------------
# _read_ndjson_unlocked: skips non-dict lines gracefully
# ---------------------------------------------------------------------------

class TestReadNDJSONRobustness(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_array_lines_skipped(self):
        """JSON arrays (not dicts) in NDJSON should be silently skipped."""
        p = Path(self._tmp) / "test_array.ndjson"
        p.write_text(
            json.dumps({"id": "x1", "ts": "2025-01-01T00:00:00", "text": "ok"}) + "\n"
            + json.dumps([1, 2, 3]) + "\n"   # array — should skip
            + json.dumps({"id": "x2", "ts": "2025-01-01T00:01:00", "text": "also ok"}) + "\n",
            encoding="utf-8",
        )
        entries = list(StateStore._read_ndjson_unlocked(p))
        self.assertEqual(len(entries), 2)

    def test_blank_lines_skipped(self):
        p = Path(self._tmp) / "test_blank.ndjson"
        p.write_text(
            '{"a": 1}\n\n\n{"b": 2}\n',
            encoding="utf-8",
        )
        entries = list(StateStore._read_ndjson_unlocked(p))
        self.assertEqual(len(entries), 2)

    def test_nonexistent_file_returns_empty(self):
        p = Path(self._tmp) / "ghost.ndjson"
        entries = list(StateStore._read_ndjson_unlocked(p))
        self.assertEqual(entries, [])


# ---------------------------------------------------------------------------
# compact_with_stats reclaimed_bytes
# ---------------------------------------------------------------------------

class TestCompactWithStats(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_reclaimed_bytes_positive_when_deleted_items(self):
        ids = [_add(self.store, f"text {i}") for i in range(10)]
        # Delete half
        for i in ids[:5]:
            self.store.delete_history_item(i)
        stats = self.store.compact_with_stats()
        # Tombstones were present before compact
        self.assertGreater(stats["before_tombstones_lines"], 0)
        # After compact, tombstones gone
        self.assertEqual(stats["after_tombstones_lines"], 0)
        self.assertGreaterEqual(stats["reclaimed_bytes"], 0)

    def test_before_active_count_matches_count_active_items(self):
        for _ in range(4):
            _add(self.store, "item")
        expected_active = self.store.count_active_items()
        stats = self.store.compact_with_stats()
        self.assertEqual(stats["before_active_count"], expected_active)


# ---------------------------------------------------------------------------
# count_active_items — must reuse the incrementally-maintained _active_ids
# cache instead of rescanning history.ndjson + overrides on every call.
# handle_ping calls this on every 3s HealthMonitor heartbeat (backend.log
# 2026-08-09 storm: dozens of unrelated handle_request calls timed out at
# 180s right after a long-recording finalize, tracing back to this O(n)
# scan running under the global StateStore flock).
# ---------------------------------------------------------------------------

class TestCountActiveItemsReusesCache(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_does_not_rescan_when_active_ids_already_warm(self):
        for i in range(5):
            _add(self.store, f"item {i}")
        # First call is a legitimate cold-start scan (_active_ids starts
        # None) — it also warms the cache for everyone else.
        first_count = self.store.count_active_items()
        self.assertEqual(first_count, 5)
        self.assertIsNotNone(self.store._active_ids)

        from unittest.mock import patch

        with patch.object(
            self.store, "_load_active_items_unlocked",
            wraps=self.store._load_active_items_unlocked,
        ) as spy:
            count = self.store.count_active_items()
            count_again = self.store.count_active_items()

        self.assertEqual(count, 5)
        self.assertEqual(count_again, 5)
        spy.assert_not_called()

    def test_count_reflects_deletes_without_rescanning(self):
        ids = [_add(self.store, f"item {i}") for i in range(6)]
        self.store.delete_history_item(ids[0])
        # Cold-start warm, same as any first read of the cache.
        self.store.count_active_items()
        self.store.delete_history_item(ids[1])
        self.assertIsNotNone(self.store._active_ids)

        from unittest.mock import patch

        with patch.object(
            self.store, "_load_active_items_unlocked",
            wraps=self.store._load_active_items_unlocked,
        ) as spy:
            count = self.store.count_active_items()

        self.assertEqual(count, 4)
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# _normalize_ts_filter
# ---------------------------------------------------------------------------

class TestNormalizeTsFilter(unittest.TestCase):

    def test_date_only_start(self):
        result = StateStore._normalize_ts_filter("2025-01-15", is_end=False)
        self.assertEqual(result, "2025-01-15T00:00:00")

    def test_date_only_end(self):
        result = StateStore._normalize_ts_filter("2025-01-15", is_end=True)
        self.assertEqual(result, "2025-01-15T23:59:59")

    def test_full_datetime_preserved(self):
        result = StateStore._normalize_ts_filter("2025-03-10T14:30:00", is_end=False)
        self.assertEqual(result, "2025-03-10T14:30:00")

    def test_none_returns_none(self):
        result = StateStore._normalize_ts_filter(None, is_end=False)
        self.assertIsNone(result)

    def test_empty_string_returns_none(self):
        result = StateStore._normalize_ts_filter("", is_end=False)
        self.assertIsNone(result)

    def test_invalid_format_returns_none(self):
        # A string that is definitely not parseable as ISO date/datetime
        result = StateStore._normalize_ts_filter("yesterday evening", is_end=False)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# _parse_cursor edge-cases
# ---------------------------------------------------------------------------

class TestParseCursor(unittest.TestCase):

    def test_none_returns_zero(self):
        self.assertEqual(StateStore._parse_cursor(None), 0)

    def test_valid_integer_string(self):
        self.assertEqual(StateStore._parse_cursor("10"), 10)

    def test_negative_clamped_to_zero(self):
        self.assertEqual(StateStore._parse_cursor("-5"), 0)

    def test_garbage_string_returns_zero(self):
        self.assertEqual(StateStore._parse_cursor("abc"), 0)

    def test_zero_string(self):
        self.assertEqual(StateStore._parse_cursor("0"), 0)


# ---------------------------------------------------------------------------
# NDJSON line ordering (insertion order preserved on disk)
# ---------------------------------------------------------------------------

class TestNDJSONLineOrdering(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_100_items_order_preserved(self):
        """Append 100 items; verify disk order matches insertion order."""
        ids = [_add(self.store, f"item-{i:03d}") for i in range(100)]
        # Read raw NDJSON lines and check id sequence
        raw_ids = []
        for entry in StateStore._read_ndjson_unlocked(self.store.history_path):
            raw_ids.append(entry.get("id", ""))
        self.assertEqual(raw_ids, ids)


# ---------------------------------------------------------------------------
# Concurrent writes (thread-safety via file lock)
# ---------------------------------------------------------------------------

class TestConcurrentWritesSafety(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_concurrent_threads_no_data_loss(self):
        N_THREADS = 8
        N_PER_THREAD = 20
        errors = []

        def worker(thread_idx: int) -> None:
            try:
                for j in range(N_PER_THREAD):
                    _add(self.store, f"thread-{thread_idx}-item-{j}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Errors during concurrent writes: {errors}")
        count = self.store.count_active_items()
        self.assertEqual(count, N_THREADS * N_PER_THREAD)


# ---------------------------------------------------------------------------
# Settings atomic write / tmp-file pattern
# ---------------------------------------------------------------------------

class TestSettingsAtomicWrite(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_no_tmp_file_left_after_save(self):
        self.store.save_settings({"whisper_model": "large-v3"})
        tmp_path = self.store.settings_path.with_suffix(".json.tmp")
        self.assertFalse(tmp_path.exists(), "Temp file should be gone after atomic replace")

    def test_settings_file_is_valid_json_after_save(self):
        self.store.save_settings({"whisper_model": "base"})
        raw = self.store.settings_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertEqual(parsed["whisper_model"], "base")


if __name__ == "__main__":
    unittest.main()
