"""Wave 86 extras — CollectionManager edge cases not covered by existing tests.

Covers:
- Bulk add/remove with 100+ items
- Concurrent create/delete same collection (thread safety)
- Persistence integrity: reload after each mutation type
- Integration: deleted history items are skipped (not purged from IDs on read)
- Edge cases: unicode names, whitespace-only names, very long names,
  add item to collection then delete collection (item_ids gone),
  remove item that was never added (noop), IPC handler wiring for
  missing params permutations.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.collection_manager import CollectionManager


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str = "текст") -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def delete_item(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


def _make_manager(tmpdir: str) -> tuple[FakeStore, CollectionManager]:
    store = FakeStore(data_dir=tmpdir)
    mgr = CollectionManager(store=store)
    return store, mgr


# ---------------------------------------------------------------------------
# Bulk 100+ items
# ---------------------------------------------------------------------------

class BulkLargeTestCase(unittest.TestCase):
    """Bulk operations with 100+ items."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._mgr = _make_manager(self._tmpdir)
        self._mgr.create_collection("LargeBulk")

    def test_bulk_add_100_items_count(self) -> None:
        for i in range(100):
            self._store.add_fake_item(f"big_{i}")
            self._mgr.add_to_collection("LargeBulk", f"big_{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "LargeBulk")
        self.assertEqual(col["item_count"], 100)

    def test_bulk_add_150_unique_dedup(self) -> None:
        """Adding 150 unique IDs then re-adding 50 of them: count stays 150."""
        for i in range(150):
            self._store.add_fake_item(f"u_{i}")
            self._mgr.add_to_collection("LargeBulk", f"u_{i}")
        # Re-add first 50 — should be idempotent
        for i in range(50):
            self._mgr.add_to_collection("LargeBulk", f"u_{i}")
        col = next(c for c in self._mgr.list_collections() if c["name"] == "LargeBulk")
        self.assertEqual(col["item_count"], 150)

    def test_bulk_remove_100_items(self) -> None:
        for i in range(100):
            self._store.add_fake_item(f"rm_{i}")
            self._mgr.add_to_collection("LargeBulk", f"rm_{i}")
        for i in range(100):
            self._mgr.remove_from_collection("LargeBulk", f"rm_{i}")
        col = next(c for c in self._mgr.list_collections() if c["name"] == "LargeBulk")
        self.assertEqual(col["item_count"], 0)

    def test_bulk_add_persisted_all_ids(self) -> None:
        """After 100 adds the JSON file contains all 100 item_ids."""
        for i in range(100):
            self._store.add_fake_item(f"p_{i}")
            self._mgr.add_to_collection("LargeBulk", f"p_{i}")
        data = json.loads(
            (Path(self._tmpdir) / "collections.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(data["collections"]["LargeBulk"]["item_ids"]), 100)

    def test_bulk_get_items_order_100(self) -> None:
        ids = [f"ord_{i:03d}" for i in range(100)]
        for item_id in ids:
            self._store.add_fake_item(item_id)
            self._mgr.add_to_collection("LargeBulk", item_id)
        items = self._mgr.get_collection_items("LargeBulk")
        self.assertEqual([it["id"] for it in items], ids)

    def test_bulk_add_then_partial_remove_correct_count(self) -> None:
        for i in range(100):
            self._store.add_fake_item(f"pr_{i}")
            self._mgr.add_to_collection("LargeBulk", f"pr_{i}")
        for i in range(37):
            self._mgr.remove_from_collection("LargeBulk", f"pr_{i}")
        col = next(c for c in self._mgr.list_collections() if c["name"] == "LargeBulk")
        self.assertEqual(col["item_count"], 63)


# ---------------------------------------------------------------------------
# Concurrent operations
# ---------------------------------------------------------------------------

class ConcurrentTestCase(unittest.TestCase):
    """Thread-safety: concurrent create/delete on the same collection name."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._mgr = _make_manager(self._tmpdir)

    def test_concurrent_add_to_same_collection(self) -> None:
        """50 threads each add a unique item — no data races, count is 50."""
        self._mgr.create_collection("Concurrent")
        for i in range(50):
            self._store.add_fake_item(f"c_{i}")

        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                self._mgr.add_to_collection("Concurrent", f"c_{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        col = next(c for c in self._mgr.list_collections() if c["name"] == "Concurrent")
        self.assertEqual(col["item_count"], 50)

    def test_concurrent_create_same_name_only_one_succeeds(self) -> None:
        """10 threads race to create a collection named 'Race'."""
        successes = []
        errors = []

        def creator() -> None:
            try:
                self._mgr.create_collection("Race")
                successes.append(True)
            except ValueError:
                errors.append(True)

        threads = [threading.Thread(target=creator) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 1, "Exactly one create must succeed")
        self.assertEqual(len(errors), 9, "All other 9 must fail with ValueError")

    def test_concurrent_create_then_delete_race(self) -> None:
        """One thread creates, another deletes — final state is consistent."""
        self._mgr.create_collection("CD")

        results: list[bool] = []

        def deleter() -> None:
            results.append(self._mgr.delete_collection("CD"))

        def creator() -> None:
            try:
                self._mgr.create_collection("CD")
                results.append(True)
            except ValueError:
                results.append(False)

        t1 = threading.Thread(target=deleter)
        t2 = threading.Thread(target=creator)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # After race: either collection exists or it doesn't — either is fine.
        # What matters is list_collections doesn't raise and is self-consistent.
        cols = self._mgr.list_collections()
        # If CD was re-created after delete, it should appear once.
        count = sum(1 for c in cols if c["name"] == "CD")
        self.assertLessEqual(count, 1)

    def test_concurrent_remove_same_item_no_crash(self) -> None:
        """10 threads simultaneously remove the same item — no exception."""
        self._mgr.create_collection("ConcRemove")
        self._store.add_fake_item("shared")
        self._mgr.add_to_collection("ConcRemove", "shared")

        errors: list[Exception] = []

        def remover() -> None:
            try:
                self._mgr.remove_from_collection("ConcRemove", "shared")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=remover) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        col = next(c for c in self._mgr.list_collections() if c["name"] == "ConcRemove")
        self.assertEqual(col["item_count"], 0)


# ---------------------------------------------------------------------------
# Persistence integrity after each mutation
# ---------------------------------------------------------------------------

class PersistenceReloadTestCase(unittest.TestCase):
    """Every mutation must survive a reload via a fresh CollectionManager."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def _reload(self) -> CollectionManager:
        return CollectionManager(store=self._store)

    def test_create_persists_after_reload(self) -> None:
        self._mgr.create_collection("Persist1", "описание1")
        mgr2 = self._reload()
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("Persist1", names)

    def test_delete_persists_after_reload(self) -> None:
        self._mgr.create_collection("ToDelete")
        self._mgr.delete_collection("ToDelete")
        mgr2 = self._reload()
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertNotIn("ToDelete", names)

    def test_add_item_persists_after_reload(self) -> None:
        self._mgr.create_collection("WithItem")
        self._store.add_fake_item("persist_id")
        self._mgr.add_to_collection("WithItem", "persist_id")
        mgr2 = self._reload()
        col = next(c for c in mgr2.list_collections() if c["name"] == "WithItem")
        self.assertEqual(col["item_count"], 1)

    def test_remove_item_persists_after_reload(self) -> None:
        self._mgr.create_collection("WithRemove")
        self._store.add_fake_item("rem_id")
        self._mgr.add_to_collection("WithRemove", "rem_id")
        self._mgr.remove_from_collection("WithRemove", "rem_id")
        mgr2 = self._reload()
        col = next(c for c in mgr2.list_collections() if c["name"] == "WithRemove")
        self.assertEqual(col["item_count"], 0)

    def test_rename_persists_after_reload(self) -> None:
        self._mgr.create_collection("OldName")
        self._mgr.rename_collection("OldName", "NewName")
        mgr2 = self._reload()
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("NewName", names)
        self.assertNotIn("OldName", names)

    def test_multiple_mutations_persist(self) -> None:
        """Create 3, delete 1, rename 1, add items to 1 — reload is consistent."""
        self._mgr.create_collection("A")
        self._mgr.create_collection("B")
        self._mgr.create_collection("C")
        self._mgr.delete_collection("C")
        self._mgr.rename_collection("B", "B2")
        for i in range(5):
            self._store.add_fake_item(f"m_{i}")
            self._mgr.add_to_collection("A", f"m_{i}")

        mgr2 = self._reload()
        names = {c["name"] for c in mgr2.list_collections()}
        self.assertIn("A", names)
        self.assertIn("B2", names)
        self.assertNotIn("B", names)
        self.assertNotIn("C", names)
        col_a = next(c for c in mgr2.list_collections() if c["name"] == "A")
        self.assertEqual(col_a["item_count"], 5)

    def test_corrupted_json_falls_back_to_empty(self) -> None:
        """If collections.json is corrupt on load, manager starts empty."""
        col_path = Path(self._tmpdir) / "collections.json"
        col_path.write_text("{ invalid json !!!", encoding="utf-8")
        mgr2 = CollectionManager(store=self._store)
        self.assertEqual(mgr2.list_collections(), [])


# ---------------------------------------------------------------------------
# History item lifecycle integration
# ---------------------------------------------------------------------------

class HistoryItemLifecycleTestCase(unittest.TestCase):
    """Integration: get_collection_items skips items removed from history."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._mgr = _make_manager(self._tmpdir)
        self._mgr.create_collection("Live")

    def test_deleted_history_item_skipped_in_get_items(self) -> None:
        """item_ids retains the ID after store delete; get_collection_items skips it."""
        self._store.add_fake_item("alive")
        self._store.add_fake_item("dead")
        self._mgr.add_to_collection("Live", "alive")
        self._mgr.add_to_collection("Live", "dead")
        self._store.delete_item("dead")

        items = self._mgr.get_collection_items("Live")
        ids = [it["id"] for it in items]
        self.assertIn("alive", ids)
        self.assertNotIn("dead", ids)

    def test_item_count_does_not_drop_when_history_deleted(self) -> None:
        """item_count (from list_collections) counts stored IDs, not live items."""
        self._store.add_fake_item("x")
        self._mgr.add_to_collection("Live", "x")
        self._store.delete_item("x")
        # item_count reflects raw item_ids in the collection JSON
        col = next(c for c in self._mgr.list_collections() if c["name"] == "Live")
        self.assertEqual(col["item_count"], 1)

    def test_all_items_deleted_get_returns_empty_list(self) -> None:
        for i in range(5):
            self._store.add_fake_item(f"del_{i}")
            self._mgr.add_to_collection("Live", f"del_{i}")
        for i in range(5):
            self._store.delete_item(f"del_{i}")
        items = self._mgr.get_collection_items("Live")
        self.assertEqual(items, [])

    def test_partial_history_deletion_returns_survivors(self) -> None:
        for i in range(6):
            self._store.add_fake_item(f"s_{i}")
            self._mgr.add_to_collection("Live", f"s_{i}")
        # Delete even-indexed items
        for i in range(0, 6, 2):
            self._store.delete_item(f"s_{i}")
        items = self._mgr.get_collection_items("Live")
        ids = [it["id"] for it in items]
        self.assertEqual(ids, ["s_1", "s_3", "s_5"])

    def test_delete_collection_releases_item_ids(self) -> None:
        """Deleting a collection removes its item_ids from persistence."""
        self._store.add_fake_item("k1")
        self._mgr.add_to_collection("Live", "k1")
        self._mgr.delete_collection("Live")
        data = json.loads(
            (Path(self._tmpdir) / "collections.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("Live", data["collections"])


# ---------------------------------------------------------------------------
# Unicode and edge-case names
# ---------------------------------------------------------------------------

class EdgeCaseNamesTestCase(unittest.TestCase):
    """Unicode names, emoji, very long names, whitespace-only."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._mgr = _make_manager(self._tmpdir)

    def test_unicode_name_cyrillic(self) -> None:
        result = self._mgr.create_collection("Записи о встречах")
        self.assertEqual(result["name"], "Записи о встречах")

    def test_unicode_name_emoji(self) -> None:
        result = self._mgr.create_collection("🎙️ Подкаст")
        self.assertEqual(result["name"], "🎙️ Подкаст")

    def test_unicode_name_chinese(self) -> None:
        result = self._mgr.create_collection("会议记录")
        self.assertEqual(result["name"], "会议记录")

    def test_unicode_name_arabic(self) -> None:
        result = self._mgr.create_collection("سجلات")
        self.assertEqual(result["name"], "سجلات")

    def test_whitespace_only_name_raises(self) -> None:
        for bad in ("   ", "\t", "\n", "\r\n"):
            with self.assertRaises(ValueError, msg=f"should fail for {repr(bad)}"):
                self._mgr.create_collection(bad)

    def test_very_long_name_rejected(self) -> None:
        """wave-34: 200-char cap added — names >200 chars are now rejected."""
        long_name = "А" * 500
        with self.assertRaises(ValueError):
            self._mgr.create_collection(long_name)

    def test_max_length_name_accepted(self) -> None:
        """Names exactly at the 200-char limit are still accepted."""
        max_name = "А" * 200
        result = self._mgr.create_collection(max_name)
        self.assertEqual(result["name"], max_name)

    def test_leading_trailing_whitespace_stripped(self) -> None:
        """Name with surrounding spaces is stored stripped."""
        result = self._mgr.create_collection("  Пробелы  ")
        self.assertEqual(result["name"], "Пробелы")

    def test_unicode_name_persisted_correctly(self) -> None:
        self._mgr.create_collection("日本語テスト")
        data = json.loads(
            (Path(self._tmpdir) / "collections.json").read_text(encoding="utf-8")
        )
        self.assertIn("日本語テスト", data["collections"])

    def test_duplicate_unicode_name_raises(self) -> None:
        self._mgr.create_collection("Дубль 🔁")
        with self.assertRaises(ValueError):
            self._mgr.create_collection("Дубль 🔁")

    def test_unicode_item_id_accepted(self) -> None:
        """item_ids can be unicode strings without raising."""
        self._mgr.create_collection("UID")
        self._store.add_fake_item("запись-001")
        result = self._mgr.add_to_collection("UID", "запись-001")
        self.assertEqual(result["item_count"], 1)

    def test_whitespace_item_id_raises(self) -> None:
        self._mgr.create_collection("WSItem")
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("WSItem", "   ")

    def test_name_with_special_json_chars(self) -> None:
        """Names with quotes/backslashes persist and reload correctly."""
        name = 'Col "with" quotes & \\backslash'
        self._mgr.create_collection(name)
        mgr2 = CollectionManager(store=self._store)
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn(name, names)


# ---------------------------------------------------------------------------
# Additional IPC handler permutations
# ---------------------------------------------------------------------------

class IPCHandlerPermutationsTestCase(unittest.TestCase):
    """Missing/empty/None params permutations for all IPC handlers."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store, self._mgr = _make_manager(self._tmpdir)
        self._mgr.create_collection("Existing")

    def test_handle_create_empty_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_create_collection({"name": "  "})

    def test_handle_delete_empty_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_delete_collection({"name": ""})

    def test_handle_delete_nonexistent_returns_false(self) -> None:
        result = self._mgr.handle_delete_collection({"name": "НеСуществует"})
        self.assertFalse(result["deleted"])

    def test_handle_add_missing_collection_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection({"item_id": "x"})

    def test_handle_add_empty_item_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection({"collection_name": "Existing", "item_id": ""})

    def test_handle_remove_missing_collection_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_remove_from_collection({"item_id": "x"})

    def test_handle_remove_empty_item_id_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_remove_from_collection({"collection_name": "Existing", "item_id": "  "})

    def test_handle_get_items_missing_collection_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_collection_items({"collection_name": "НеСуществует"})

    def test_handle_rename_missing_both_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_rename_collection({})

    def test_handle_list_collections_always_returns_dict(self) -> None:
        result = self._mgr.handle_list_collections({})
        self.assertIn("collections", result)
        self.assertIsInstance(result["collections"], list)

    def test_handle_get_items_empty_collection_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_collection_items({"collection_name": "  "})


if __name__ == "__main__":
    unittest.main()
