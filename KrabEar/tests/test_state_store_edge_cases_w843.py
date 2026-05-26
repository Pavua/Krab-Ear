"""Wave 843 — StateStore edge-case unit tests.

Covers gaps not addressed by the existing test_state_store* suite:

1. test_malformed_status_journal_entries_skipped
   Corrupt JSON lines + valid-JSON-but-no-id entries in history_status.ndjson
   do not crash and do not corrupt surviving status overrides.

2. test_tombstone_without_id_key_does_not_delete_item
   A tombstone entry that is valid JSON but has no "id" key must not cause any
   active item to disappear.

3. test_delete_item_after_compaction_hides_it
   Add an item → compact (history.ndjson rewritten, tombstones cleared) →
   delete the item → item must be absent from the next load.

4. test_read_ndjson_blank_lines_only_returns_nothing
   A file containing only blank lines and whitespace must yield zero records.

5. test_compact_with_stats_no_deletes_reclaimed_bytes_zero
   When no items are deleted, reclaimed_bytes must be 0 (or ≤ 0 after
   compaction rewrites the file identically).

6. test_concurrent_status_writes_two_store_instances
   Two independent StateStore instances sharing the same data directory write
   paste-status overrides concurrently; all updates must survive with no
   interleaved-line corruption.

7. test_import_ndjson_file_with_all_invalid_entries
   An import file whose every line is either blank, invalid JSON, or a valid
   JSON object lacking the required id/ts/text fields must report
   errors > 0 and imported == 0.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str | Path, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


def _add(store: StateStore, text: str = "hello", **kw) -> str:
    item = store.add_history_item(text, **kw)
    return item.id


def _raw_lines(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8") as fh:
        return [ln.rstrip("\n") for ln in fh if ln.strip()]


# ---------------------------------------------------------------------------
# 1. Malformed status journal entries are skipped gracefully
# ---------------------------------------------------------------------------

class TestMalformedStatusJournalEntries(unittest.TestCase):
    """Corrupt/missing-id lines in history_status.ndjson must not crash or
    corrupt the surviving override entries."""

    def test_malformed_status_journal_entries_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            item_id = _add(store, "status test", paste_status="failed")
            # Write a valid status override for the real item.
            store.set_paste_status(item_id, "ok")

            # Inject garbage directly into the status delta journal.
            with store.status_path.open("a", encoding="utf-8") as fh:
                # Truncated JSON
                fh.write("{broken\n")
                # Valid JSON that is a list, not a dict
                fh.write(json.dumps([1, 2, 3]) + "\n")
                # Valid JSON dict but no "id" key
                fh.write(json.dumps({"paste_status": "failed"}) + "\n")
                # Valid JSON dict with empty "id"
                fh.write(json.dumps({"id": "", "paste_status": "failed"}) + "\n")

            # The real item's status must still be "ok" despite the garbage.
            item = store.get_history_item_by_id(item_id)
            self.assertIsNotNone(item, "item must still exist after injecting garbage into status journal")
            self.assertEqual(item.paste_status, "ok",
                             "Valid status override must survive malformed lines")

            # Loading active items must not raise.
            active = store._load_active_items_with_lock()
            ids = {i.id for i in active}
            self.assertIn(item_id, ids)


# ---------------------------------------------------------------------------
# 2. Tombstone without "id" key must not delete any item
# ---------------------------------------------------------------------------

class TestTombstoneWithoutIdKey(unittest.TestCase):
    """A tombstone entry that is valid JSON but lacks the 'id' key must not
    delete any active item."""

    def test_tombstone_without_id_key_does_not_delete_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            item_id = _add(store, "should survive")

            # Inject a tombstone entry that has no "id" field.
            with store.tombstones_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"not_id": "something"}) + "\n")
                fh.write(json.dumps({"id": ""}) + "\n")       # empty id — should also be ignored
                fh.write(json.dumps({"id": "   "}) + "\n")    # whitespace-only id

            active = store._load_active_items_with_lock()
            ids = {i.id for i in active}
            self.assertIn(item_id, ids,
                          "Item must not be deleted by a tombstone with no valid id key")
            self.assertEqual(len(active), 1)


# ---------------------------------------------------------------------------
# 3. Delete an item AFTER compaction hides it
# ---------------------------------------------------------------------------

class TestDeleteAfterCompaction(unittest.TestCase):
    """After compaction the tombstone journal is cleared.  A new tombstone
    written after compaction must still hide the item."""

    def test_delete_item_after_compaction_hides_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            keep_id = _add(store, "keep me")
            delete_id = _add(store, "delete me after compact")

            # Compact: history.ndjson rewritten, tombstones cleared.
            store.compact()

            # Verify tombstones file is now empty.
            self.assertEqual(store.tombstones_path.stat().st_size, 0,
                             "tombstones file must be empty right after compact()")

            # Now delete the item post-compaction.
            result = store.delete_history_item(delete_id)
            self.assertTrue(result, "delete_history_item must return True for existing id")

            # The deleted item must no longer appear.
            active = store._load_active_items_with_lock()
            ids = {i.id for i in active}
            self.assertNotIn(delete_id, ids,
                             "Item deleted post-compaction must not appear in active items")
            self.assertIn(keep_id, ids,
                          "Non-deleted item must remain after post-compaction delete")


# ---------------------------------------------------------------------------
# 4. _read_ndjson_unlocked on blank-only file yields nothing
# ---------------------------------------------------------------------------

class TestReadNdjsonBlankLines(unittest.TestCase):
    """A file containing only blank lines / whitespace must yield zero records."""

    def test_read_ndjson_blank_lines_only_returns_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "blank.ndjson"
            target.write_text("\n   \n\n\t\n", encoding="utf-8")
            records = list(StateStore._read_ndjson_unlocked(target))
            self.assertEqual(records, [],
                             "_read_ndjson_unlocked must return nothing for a blank-only file")


# ---------------------------------------------------------------------------
# 5. compact_with_stats: reclaimed_bytes is 0 when nothing was deleted
# ---------------------------------------------------------------------------

class TestCompactWithStatsNoDeletes(unittest.TestCase):
    """When compact_with_stats() is called with no tombstones, reclaimed_bytes
    should be <= 0 (the compaction rewrites the file identically in size, and
    the delta journals are empty, so no bytes are reclaimed)."""

    def test_compact_with_stats_no_deletes_reclaimed_bytes_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            for i in range(5):
                _add(store, f"item {i}")

            stats = store.compact_with_stats()

            self.assertIn("reclaimed_bytes", stats)
            # No items deleted → status and tombstone journals were empty →
            # no bytes reclaimed (may even be slightly negative due to
            # tmp-file overhead, but must be close to 0).
            self.assertLessEqual(stats["reclaimed_bytes"], 0,
                                 "No deletes → reclaimed_bytes must be ≤ 0")
            # Active count must be preserved.
            self.assertEqual(stats["after_active_count"], 5)
            self.assertEqual(stats["before_active_count"], 5)


# ---------------------------------------------------------------------------
# 6. Two StateStore instances writing status overrides concurrently
# ---------------------------------------------------------------------------

class TestConcurrentStatusWritesTwoInstances(unittest.TestCase):
    """Two independent StateStore instances targeting the same data directory
    write paste-status overrides concurrently.  Every update must be
    persistent, and no line must be interleaved/corrupted."""

    THREADS_PER_STORE = 20

    def test_concurrent_status_writes_two_store_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_a = _make_store(tmp)
            store_b = _make_store(tmp)  # independent instance, same directory

            # Pre-populate items via store_a (both instances share the file).
            ids_a = [_add(store_a, f"a-item-{i}") for i in range(self.THREADS_PER_STORE)]
            ids_b = [_add(store_a, f"b-item-{i}") for i in range(self.THREADS_PER_STORE)]

            def write_status_a(item_id: str) -> bool:
                return store_a.set_paste_status(item_id, "ok")

            def write_status_b(item_id: str) -> bool:
                return store_b.set_paste_status(item_id, "ok")

            with ThreadPoolExecutor(max_workers=self.THREADS_PER_STORE * 2) as ex:
                futures_a = [ex.submit(write_status_a, iid) for iid in ids_a]
                futures_b = [ex.submit(write_status_b, iid) for iid in ids_b]
                results = [f.result() for f in futures_a + futures_b]

            # All writes must have returned True.
            self.assertTrue(all(results), "All set_paste_status calls must succeed")

            # Every line in status.ndjson must be valid JSON.
            lines = _raw_lines(store_a.status_path)
            for line in lines:
                parsed = json.loads(line)  # must not raise
                self.assertIsInstance(parsed, dict,
                                      f"Status line must be a JSON dict: {line!r}")

            # The total number of status entries must be exactly
            # THREADS_PER_STORE * 2 (one per item).
            self.assertEqual(len(lines), self.THREADS_PER_STORE * 2)


# ---------------------------------------------------------------------------
# 7. import_history_ndjson with entirely invalid file
# ---------------------------------------------------------------------------

class TestImportNdjsonAllInvalid(unittest.TestCase):
    """An import file where every entry is invalid must produce imported==0
    and errors > 0."""

    def test_import_ndjson_file_with_all_invalid_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            import_file = Path(tmp) / "bad_import.ndjson"
            with import_file.open("w", encoding="utf-8") as fh:
                # Truncated JSON
                fh.write("{not valid json\n")
                # JSON array (not a dict)
                fh.write(json.dumps(["a", "b"]) + "\n")
                # Valid dict but missing "id"
                fh.write(json.dumps({"ts": "2026-01-01T00:00:00", "text": "hi"}) + "\n")
                # Valid dict but missing "text"
                fh.write(json.dumps({"id": "x1", "ts": "2026-01-01T00:00:00"}) + "\n")
                # Valid dict but missing "ts"
                fh.write(json.dumps({"id": "x2", "text": "hello"}) + "\n")
                # Blank line
                fh.write("\n")

            stats = store.import_history_ndjson(import_file)

            self.assertEqual(stats["imported"], 0,
                             "No valid entries should be imported from an all-invalid file")
            self.assertGreater(stats["errors"], 0,
                               "errors counter must reflect the invalid entries")
            self.assertEqual(stats["skipped"], 0)

            # Store must remain empty.
            self.assertEqual(store.count_active_items(), 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
