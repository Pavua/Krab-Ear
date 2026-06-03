"""Tests for wave-36 LOW fixes:
  D1 — state_store.import_history_ndjson must update _active_ids so that
       set_paste_status works for imported items without a false-negative.
  D2 — bookmarks.add must truncate note at MAX_NOTE_LEN (2000 chars).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.bookmarks import BookmarkManager, MAX_NOTE_LEN  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(Path(tmp_dir) / "data")


def _write_ndjson(path: Path, items: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")


# ---------------------------------------------------------------------------
# D1: import_history_ndjson updates _active_ids
# ---------------------------------------------------------------------------

class TestImportHistoryActiveIds(unittest.TestCase):
    """import_history_ndjson must keep _active_ids in sync so that subsequent
    set_paste_status calls on imported item ids succeed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_import_file(self, items: list[dict]) -> Path:
        p = Path(self._tmp.name) / "import.ndjson"
        _write_ndjson(p, items)
        return p

    def test_set_paste_status_works_after_import(self):
        """After import, set_paste_status on an imported id must return True."""
        item = {
            "id": "imported-id-001",
            "ts": "2026-01-01T10:00:00",
            "text": "hello",
            "paste_status": "ok",
            "source_text": "",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
            "chat_id": "",
            "message_id": "",
            "cleaned_text": "",
            "llm_applied": False,
            "llm_latency_ms": 0,
            "privacy_mode": False,
        }
        import_file = self._make_import_file([item])

        # Prime _active_ids by calling set_paste_status on a regular item first
        regular_item = self.store.add_history_item("regular", paste_status="ok")
        self.store._ensure_active_ids_unlocked()  # force lazy init
        # Confirm _active_ids is now initialised
        self.assertIsNotNone(self.store._active_ids)
        self.assertIn(regular_item.id, self.store._active_ids)

        # Import external items
        result = self.store.import_history_ndjson(import_file)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["skipped"], 0)

        # _active_ids must now include the imported id
        self.assertIn("imported-id-001", self.store._active_ids)

        # set_paste_status must succeed (returns True) for the imported id
        ok = self.store.set_paste_status("imported-id-001", "pasted")
        self.assertTrue(ok, "set_paste_status should return True for imported item")

    def test_active_ids_none_not_broken_by_import(self):
        """When _active_ids is None (lazy init not yet done), import must not crash
        and _active_ids must remain None (lazy init will happen on first use)."""
        item = {
            "id": "imported-id-002",
            "ts": "2026-01-01T11:00:00",
            "text": "world",
            "paste_status": "ok",
            "source_text": "",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
            "chat_id": "",
            "message_id": "",
            "cleaned_text": "",
            "llm_applied": False,
            "llm_latency_ms": 0,
            "privacy_mode": False,
        }
        import_file = self._make_import_file([item])
        # Do NOT prime _active_ids — it stays None
        self.assertIsNone(self.store._active_ids)

        result = self.store.import_history_ndjson(import_file)
        self.assertEqual(result["imported"], 1)
        # _active_ids is still None (lazy) — no crash, no corruption
        self.assertIsNone(self.store._active_ids)

        # But calling set_paste_status now works fine via lazy init
        ok = self.store.set_paste_status("imported-id-002", "pasted")
        self.assertTrue(ok)

    def test_import_multiple_items_all_added_to_active_ids(self):
        """All newly imported items must appear in _active_ids."""
        items = []
        for i in range(5):
            items.append({
                "id": f"bulk-id-{i}",
                "ts": f"2026-01-0{i + 1}T10:00:00",
                "text": f"text {i}",
                "paste_status": "ok",
                "source_text": "",
                "translated_text": "",
                "translation_mode": "off",
                "source_lang": "",
                "target_lang": "",
                "translation_status": "not_requested",
                "translation_engine": "",
                "chat_id": "",
                "message_id": "",
                "cleaned_text": "",
                "llm_applied": False,
                "llm_latency_ms": 0,
                "privacy_mode": False,
            })
        import_file = self._make_import_file(items)

        # Prime _active_ids
        self.store.add_history_item("seed", paste_status="ok")
        # Trigger lazy init
        with self.store._lock():
            self.store._ensure_active_ids_unlocked()

        result = self.store.import_history_ndjson(import_file)
        self.assertEqual(result["imported"], 5)

        for i in range(5):
            self.assertIn(f"bulk-id-{i}", self.store._active_ids)
            ok = self.store.set_paste_status(f"bulk-id-{i}", "pasted")
            self.assertTrue(ok, f"set_paste_status must succeed for bulk-id-{i}")

    def test_duplicate_import_not_added_to_active_ids_twice(self):
        """Importing the same id twice must not add it twice (set semantics)."""
        item = {
            "id": "dup-id-001",
            "ts": "2026-02-01T10:00:00",
            "text": "dup text",
            "paste_status": "ok",
            "source_text": "",
            "translated_text": "",
            "translation_mode": "off",
            "source_lang": "",
            "target_lang": "",
            "translation_status": "not_requested",
            "translation_engine": "",
            "chat_id": "",
            "message_id": "",
            "cleaned_text": "",
            "llm_applied": False,
            "llm_latency_ms": 0,
            "privacy_mode": False,
        }
        import_file = self._make_import_file([item])

        # Prime _active_ids
        self.store.add_history_item("seed2", paste_status="ok")
        with self.store._lock():
            self.store._ensure_active_ids_unlocked()
        initial_count = len(self.store._active_ids)

        # First import
        r1 = self.store.import_history_ndjson(import_file)
        self.assertEqual(r1["imported"], 1)
        self.assertEqual(len(self.store._active_ids), initial_count + 1)

        # Second import of same file — must be skipped
        r2 = self.store.import_history_ndjson(import_file)
        self.assertEqual(r2["imported"], 0)
        self.assertEqual(r2["skipped"], 1)
        # _active_ids size unchanged
        self.assertEqual(len(self.store._active_ids), initial_count + 1)


# ---------------------------------------------------------------------------
# D2: bookmark note length cap
# ---------------------------------------------------------------------------

class TestBookmarkNoteLengthCap(unittest.TestCase):
    """add() must truncate notes exceeding MAX_NOTE_LEN characters."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bm = BookmarkManager(data_dir=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_note_within_limit_stored_as_is(self):
        note = "x" * MAX_NOTE_LEN
        bm = self.bm.add(session_id="s", offset_sec=0.0, note=note)
        self.assertEqual(len(bm["note"]), MAX_NOTE_LEN)
        self.assertEqual(bm["note"], note)

    def test_note_exceeding_limit_is_truncated(self):
        long_note = "a" * (MAX_NOTE_LEN + 500)
        bm = self.bm.add(session_id="s", offset_sec=1.0, note=long_note)
        self.assertIn("id", bm, "Should return a valid bookmark dict")
        self.assertEqual(len(bm["note"]), MAX_NOTE_LEN)
        self.assertEqual(bm["note"], "a" * MAX_NOTE_LEN)

    def test_note_exactly_at_limit_not_truncated(self):
        note = "b" * MAX_NOTE_LEN
        bm = self.bm.add(session_id="s", offset_sec=2.0, note=note)
        self.assertEqual(len(bm["note"]), MAX_NOTE_LEN)

    def test_empty_note_ok(self):
        bm = self.bm.add(session_id="s", offset_sec=3.0, note="")
        self.assertEqual(bm["note"], "")

    def test_note_persisted_truncated_in_ndjson(self):
        """Truncated note must be what is written to disk."""
        long_note = "c" * (MAX_NOTE_LEN + 1000)
        bm = self.bm.add(session_id="s", offset_sec=4.0, note=long_note)
        bid = bm["id"]

        loaded = self.bm.get(bid)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded["note"]), MAX_NOTE_LEN)

    def test_handle_add_bookmark_truncates_note(self):
        """IPC handler must also respect the length cap via add()."""
        long_note = "d" * (MAX_NOTE_LEN + 200)
        result = self.bm.handle_add_bookmark({
            "session_id": "sess",
            "offset_sec": 5.0,
            "note": long_note,
        })
        self.assertIn("bookmark", result)
        self.assertEqual(len(result["bookmark"]["note"]), MAX_NOTE_LEN)

    def test_max_note_len_constant_is_2000(self):
        """Ensure the constant value matches the spec."""
        self.assertEqual(MAX_NOTE_LEN, 2000)


if __name__ == "__main__":
    unittest.main()
