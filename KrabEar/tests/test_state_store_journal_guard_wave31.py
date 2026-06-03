"""Tests for wave-31 MED fix: state_store journal-write existence checks
+ set_paste_status throttle classification.

Covers:
  - set_paste_status with non-existent ID → returns False, no file write
  - set_paste_status with existent ID → returns True, write persists
  - delete_history_item with non-existent ID → returns False, no tombstone written
  - delete_history_item with existent ID → returns True (existing behaviour)
  - set_paste_status is no longer in EXCLUDED_METHODS (now rate-limited)
  - set_paste_status is classified as 'light' by IPCThrottle
  - status journal line cap triggers compaction (sub-fix 3)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.ipc_throttle import (  # noqa: E402
    EXCLUDED_METHODS,
    _classify_method,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


def _add(store: StateStore, text: str, **kw) -> str:
    """Add item and return its id."""
    item = store.add_history_item(text, **kw)
    return item.id


def _count_lines(path: Path) -> int:
    """Count non-empty lines in an NDJSON file."""
    if not path.exists():
        return 0
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


# ---------------------------------------------------------------------------
# Sub-fix 1: existence check in set_paste_status
# ---------------------------------------------------------------------------

class TestSetPasteStatusExistenceCheck(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_set_paste_status_nonexistent_id_returns_false(self):
        """set_paste_status with a junk/non-existent id must return False."""
        result = self.store.set_paste_status("nonexistent-id-abc123", "ok")
        self.assertFalse(result)

    def test_set_paste_status_nonexistent_id_writes_nothing(self):
        """set_paste_status with a junk id must not write anything to the status journal."""
        before = _count_lines(self.store.status_path)
        self.store.set_paste_status("junk-id-xyz", "ok")
        after = _count_lines(self.store.status_path)
        self.assertEqual(before, after, "Status journal must not grow for junk IDs")

    def test_set_paste_status_existent_id_returns_true(self):
        """set_paste_status with a real id must return True."""
        item_id = _add(self.store, "hello world")
        result = self.store.set_paste_status(item_id, "ok")
        self.assertTrue(result)

    def test_set_paste_status_existent_id_persists_status(self):
        """set_paste_status with a real id must update the displayed paste_status."""
        item_id = _add(self.store, "hello world", paste_status="failed")
        self.store.set_paste_status(item_id, "ok")
        item = self.store.get_history_item_by_id(item_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.paste_status, "ok")

    def test_set_paste_status_empty_id_returns_false(self):
        """Empty id must return False (pre-existing check, still works)."""
        result = self.store.set_paste_status("", "ok")
        self.assertFalse(result)

    def test_set_paste_status_whitespace_id_returns_false(self):
        """Whitespace-only id must return False."""
        result = self.store.set_paste_status("   ", "ok")
        self.assertFalse(result)

    def test_set_paste_status_deleted_id_returns_false(self):
        """set_paste_status after item is tombstoned must return False."""
        item_id = _add(self.store, "temp item")
        # Delete the item so it becomes a tombstone
        self.store.delete_history_item(item_id)
        # Now try to update its paste_status — should be rejected
        result = self.store.set_paste_status(item_id, "ok")
        self.assertFalse(result)

    def test_set_paste_status_journal_does_not_grow_on_spam(self):
        """Spamming junk IDs must not grow the status journal."""
        before = _count_lines(self.store.status_path)
        for i in range(20):
            self.store.set_paste_status(f"junk-{i}", "ok")
        after = _count_lines(self.store.status_path)
        self.assertEqual(before, after, "Journal must not grow from junk-id spam")


# ---------------------------------------------------------------------------
# Sub-fix 1: existence check in delete_history_item
# ---------------------------------------------------------------------------

class TestDeleteHistoryItemExistenceCheck(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_delete_nonexistent_id_returns_false(self):
        """delete_history_item with a junk id must return False."""
        result = self.store.delete_history_item("nonexistent-id-xyz")
        self.assertFalse(result)

    def test_delete_nonexistent_id_writes_no_tombstone(self):
        """delete_history_item with a junk id must not write to tombstones."""
        before = _count_lines(self.store.tombstones_path)
        self.store.delete_history_item("junk-tombstone-id")
        after = _count_lines(self.store.tombstones_path)
        self.assertEqual(before, after, "Tombstone journal must not grow for junk IDs")

    def test_delete_existent_id_returns_true(self):
        """delete_history_item with a real id must return True."""
        item_id = _add(self.store, "item to delete")
        result = self.store.delete_history_item(item_id)
        self.assertTrue(result)

    def test_delete_existent_id_removes_from_active(self):
        """delete_history_item with a real id must tombstone it from history."""
        item_id = _add(self.store, "item to delete")
        self.store.delete_history_item(item_id)
        item = self.store.get_history_item_by_id(item_id)
        self.assertIsNone(item)

    def test_delete_empty_id_returns_false(self):
        """Empty id must return False (pre-existing check, still works)."""
        result = self.store.delete_history_item("")
        self.assertFalse(result)

    def test_delete_already_deleted_returns_false(self):
        """Double-deleting an id must return False on the second attempt."""
        item_id = _add(self.store, "double delete me")
        first = self.store.delete_history_item(item_id)
        second = self.store.delete_history_item(item_id)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_delete_junk_spam_does_not_grow_tombstones(self):
        """Spamming junk IDs must not grow the tombstones journal."""
        before = _count_lines(self.store.tombstones_path)
        for i in range(20):
            self.store.delete_history_item(f"junk-{i}")
        after = _count_lines(self.store.tombstones_path)
        self.assertEqual(before, after, "Tombstone journal must not grow from junk-id spam")


# ---------------------------------------------------------------------------
# Sub-fix 2: set_paste_status removed from EXCLUDED_METHODS
# ---------------------------------------------------------------------------

class TestSetPasteStatusThrottleClassification(unittest.TestCase):

    def test_set_paste_status_not_in_excluded_methods(self):
        """set_paste_status must no longer be in EXCLUDED_METHODS after wave-31 fix."""
        self.assertNotIn(
            "set_paste_status",
            EXCLUDED_METHODS,
            "set_paste_status was moved to light-bucket to prevent journal spam DoS",
        )

    def test_set_paste_status_classified_as_light(self):
        """set_paste_status must be classified as 'light' (not excluded, not heavy/medium)."""
        category = _classify_method("set_paste_status")
        self.assertEqual(
            category,
            "light",
            f"Expected 'light' bucket for set_paste_status, got '{category}'",
        )

    def test_excluded_methods_still_contains_ping_and_recording(self):
        """Core lifecycle methods must still be excluded from throttling."""
        for method in ("ping", "start_recording", "stop_recording", "get_recording_state"):
            self.assertIn(method, EXCLUDED_METHODS, f"{method} must remain excluded")


# ---------------------------------------------------------------------------
# Sub-fix 3: status journal line cap triggers compaction
# ---------------------------------------------------------------------------

class TestStatusJournalLineCap(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.store = _make_store(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_status_journal_cap_constant_is_defined(self):
        """_STATUS_JOURNAL_LINE_CAP must be defined on StateStore."""
        self.assertTrue(
            hasattr(StateStore, "_STATUS_JOURNAL_LINE_CAP"),
            "StateStore must define _STATUS_JOURNAL_LINE_CAP",
        )
        self.assertIsInstance(StateStore._STATUS_JOURNAL_LINE_CAP, int)
        self.assertGreater(StateStore._STATUS_JOURNAL_LINE_CAP, 0)

    def test_maybe_compact_triggers_on_status_journal_overflow(self):
        """maybe_compact must return True and compact when status journal exceeds cap."""
        item_id = _add(self.store, "test item for cap test")

        # Artificially bloat the status journal beyond cap by writing raw entries.
        cap = StateStore._STATUS_JOURNAL_LINE_CAP
        with self.store.status_path.open("a", encoding="utf-8") as fh:
            for i in range(cap + 1):
                fh.write(json.dumps({"id": item_id, "paste_status": "ok"}) + "\n")

        # maybe_compact should detect overflow and trigger compaction.
        result = self.store.maybe_compact()
        self.assertTrue(result, "maybe_compact must return True when status journal overflows")

        # After compaction, status journal should be within a sensible size.
        remaining = _count_lines(self.store.status_path)
        self.assertLess(
            remaining,
            cap,
            f"Status journal must shrink after compaction (got {remaining} lines)",
        )

    def test_maybe_compact_does_not_compact_below_both_thresholds(self):
        """maybe_compact must return False when both history size and status lines are within limits."""
        item_id = _add(self.store, "tiny history item")
        self.store.set_paste_status(item_id, "ok")

        # history.ndjson is tiny (well below compact_threshold_bytes)
        # status journal has 1 line (well below cap)
        result = self.store.maybe_compact()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
