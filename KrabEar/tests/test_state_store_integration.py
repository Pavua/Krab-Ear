"""Integration and edge-case tests for StateStore (KrabEar/backend/state_store.py).

Coverage gaps addressed (complementary to test_state_store_dedicated.py):
- Concurrent writes from two independent StateStore instances on the same directory
- Large batch: 1000 appends round-tripped correctly
- Compaction trigger: file size shrinks after manual compact(), content preserved
- Crash recovery: truncated last line is skipped, remaining items survive
- Tombstone filter: multiple deletes - all deleted items absent from get_history_page
- Empty store: load_settings returns defaults, get_history_page returns []
- Multiple malformed NDJSON lines scattered among valid items
"""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore  # noqa: E402
from backend.models import DEFAULT_SETTINGS  # noqa: E402


def _make_store(data_dir: Path, **kwargs) -> StateStore:
    return StateStore(data_dir, **kwargs)


class ThreadsafeTwoInstancesTestCase(unittest.TestCase):
    """Two independent StateStore instances writing to the same dir concurrently."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"

    def test_two_stores_concurrent_no_data_loss(self) -> None:
        """file-lock ensures both instances see the full set of writes."""
        store_a = _make_store(self.data_dir)
        store_b = _make_store(self.data_dir)

        n_each = 25
        errors: list[Exception] = []

        def write_a() -> None:
            try:
                for i in range(n_each):
                    store_a.add_history_item(text=f"store-a-{i}")
            except Exception as exc:
                errors.append(exc)

        def write_b() -> None:
            try:
                for i in range(n_each):
                    store_b.add_history_item(text=f"store-b-{i}")
            except Exception as exc:
                errors.append(exc)

        t_a = threading.Thread(target=write_a)
        t_b = threading.Thread(target=write_b)
        t_a.start()
        t_b.start()
        t_a.join()
        t_b.join()

        self.assertEqual(errors, [], f"Concurrent writes raised: {errors}")

        # Either instance should see all 50 items
        reader = _make_store(self.data_dir)
        count = reader.count_active_items()
        self.assertEqual(count, n_each * 2)


class LargeBatchTestCase(unittest.TestCase):
    """Append 1000 items; read them all back intact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_1000_items_round_trip(self) -> None:
        n = 1000
        expected_texts = [f"batch-item-{i}" for i in range(n)]
        for text in expected_texts:
            self.store.add_history_item(text=text)

        self.assertEqual(self.store.count_active_items(), n)

        # Collect all items via pagination
        all_items: list[dict] = []
        cursor = None
        while True:
            page, cursor = self.store.get_history_page(cursor=cursor, limit=200)
            all_items.extend(page)
            if cursor is None:
                break

        self.assertEqual(len(all_items), n)
        # All texts must be present (order may be newest-first)
        returned_texts = {item["text"] for item in all_items}
        self.assertEqual(returned_texts, set(expected_texts))


class CompactionFileSizeTestCase(unittest.TestCase):
    """compact() physically shrinks history file after many tombstones."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_compact_reduces_file_size(self) -> None:
        n = 50
        items = [self.store.add_history_item(text=f"to-delete-{i}") for i in range(n)]
        # Keep only last 5; delete the rest
        delete = items[:-5]
        for item in delete:
            self.store.delete_history_item(item.id)

        size_before = self.store.history_path.stat().st_size
        self.store.compact()
        size_after = self.store.history_path.stat().st_size

        self.assertLess(size_after, size_before, "File should shrink after compaction")

    def test_compact_preserves_active_content(self) -> None:
        kept = [self.store.add_history_item(text=f"keep-{i}") for i in range(5)]
        gone = [self.store.add_history_item(text=f"gone-{i}") for i in range(5)]
        for item in gone:
            self.store.delete_history_item(item.id)

        self.store.compact()

        page, _ = self.store.get_history_page(cursor=None, limit=100)
        ids = {r["id"] for r in page}

        for item in kept:
            self.assertIn(item.id, ids, f"Kept item {item.id} missing after compact")
        for item in gone:
            self.assertNotIn(item.id, ids, f"Deleted item {item.id} still present after compact")

    def test_tombstones_emptied_after_compact(self) -> None:
        item = self.store.add_history_item(text="ephemeral")
        self.store.delete_history_item(item.id)

        self.assertGreater(self.store.tombstones_path.stat().st_size, 0)
        self.store.compact()
        self.assertEqual(self.store.tombstones_path.stat().st_size, 0)


class CrashRecoveryTestCase(unittest.TestCase):
    """Truncated last line (simulated crash mid-write) is skipped; others survive."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_truncated_last_line_ignored(self) -> None:
        good1 = self.store.add_history_item(text="before-crash")
        good2 = self.store.add_history_item(text="also-before-crash")

        # Simulate crash: append a truncated (incomplete) JSON line.
        # A real crash would leave the file with a partial line that has no
        # closing brace.  We end the line with \n so that the subsequent
        # valid append is written on its own line (mimicking an OS that flushed
        # the partial block before the process died).
        with self.store.history_path.open("a", encoding="utf-8") as fh:
            fh.write('{"id": "crashed-id", "text": "incomplete-\n')

        # New item written after "crash"
        good3 = self.store.add_history_item(text="after-crash")

        page, _ = self.store.get_history_page(cursor=None, limit=50)
        ids = {r["id"] for r in page}

        self.assertIn(good1.id, ids, "Pre-crash item 1 should survive")
        self.assertIn(good2.id, ids, "Pre-crash item 2 should survive")
        self.assertIn(good3.id, ids, "Post-crash item should be present")
        # The corrupt entry must not appear
        self.assertNotIn("crashed-id", ids, "Truncated entry must be ignored")
        # Exactly 3 valid items
        self.assertEqual(len(page), 3)


class TombstoneFilterTestCase(unittest.TestCase):
    """Multiple deletes: all deleted items absent; survivors intact."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_multiple_deletes_all_absent(self) -> None:
        all_items = [self.store.add_history_item(text=f"item-{i}") for i in range(10)]
        delete_indices = [0, 2, 4, 6, 8]
        keep_indices = [1, 3, 5, 7, 9]

        for i in delete_indices:
            self.store.delete_history_item(all_items[i].id)

        page, _ = self.store.get_history_page(cursor=None, limit=50)
        returned_ids = {r["id"] for r in page}

        for i in delete_indices:
            self.assertNotIn(
                all_items[i].id, returned_ids,
                f"Deleted item at index {i} should be absent"
            )
        for i in keep_indices:
            self.assertIn(
                all_items[i].id, returned_ids,
                f"Kept item at index {i} should be present"
            )

        self.assertEqual(len(page), 5)

    def test_delete_all_then_load_empty(self) -> None:
        items = [self.store.add_history_item(text=f"del-all-{i}") for i in range(5)]
        for item in items:
            self.store.delete_history_item(item.id)

        page, cursor = self.store.get_history_page(cursor=None, limit=50)
        self.assertEqual(page, [])
        self.assertIsNone(cursor)


class EmptyStoreTestCase(unittest.TestCase):
    """Fresh empty store: load_settings returns defaults, history returns []."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_load_settings_returns_defaults_on_empty_store(self) -> None:
        loaded = self.store.load_settings()
        for key, value in DEFAULT_SETTINGS.items():
            self.assertIn(key, loaded, f"Default key {key!r} missing")
            self.assertEqual(
                loaded[key], value,
                f"Default value for {key!r} mismatch: {loaded[key]!r} != {value!r}"
            )

    def test_get_history_page_returns_empty_list(self) -> None:
        page, cursor = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(page, [])
        self.assertIsNone(cursor)

    def test_count_active_items_zero(self) -> None:
        self.assertEqual(self.store.count_active_items(), 0)

    def test_get_history_stats_all_zeros(self) -> None:
        stats = self.store.get_history_stats()
        self.assertEqual(stats["active_count"], 0)
        self.assertEqual(stats["history_lines"], 0)
        self.assertEqual(stats["tombstones_lines"], 0)


class MalformedNdjsonTestCase(unittest.TestCase):
    """Multiple bad lines interspersed with valid records: parser skips them all."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name) / "data")

    def test_multiple_bad_lines_do_not_break_parsing(self) -> None:
        good1 = self.store.add_history_item(text="valid-1")
        good2 = self.store.add_history_item(text="valid-2")

        # Inject a variety of bad lines directly
        bad_lines = [
            "not json at all\n",
            "{missing: quotes}\n",
            "null\n",                          # valid JSON but not a dict
            "[1, 2, 3]\n",                     # array, not dict
            '{"partial": true\n',              # unclosed brace
            "\n",                              # blank line
        ]
        with self.store.history_path.open("a", encoding="utf-8") as fh:
            for line in bad_lines:
                fh.write(line)

        good3 = self.store.add_history_item(text="valid-3")

        page, _ = self.store.get_history_page(cursor=None, limit=50)
        ids = {r["id"] for r in page}

        self.assertIn(good1.id, ids)
        self.assertIn(good2.id, ids)
        self.assertIn(good3.id, ids)
        # Exactly the 3 valid items; bad lines silently skipped
        self.assertEqual(len(page), 3)

    def test_entirely_corrupt_history_returns_empty(self) -> None:
        # Overwrite history with only corrupt lines
        self.store.history_path.write_text(
            "garbage\n{broken\nnull\n[]\n",
            encoding="utf-8",
        )
        page, cursor = self.store.get_history_page(cursor=None, limit=10)
        self.assertEqual(page, [])
        self.assertIsNone(cursor)


if __name__ == "__main__":
    unittest.main()
