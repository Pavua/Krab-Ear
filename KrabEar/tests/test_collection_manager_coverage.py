"""Pure unit tests for CollectionManager — named collections of history items, CRUD + bulk."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

# Ensure backend package is importable when run standalone
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "KrabEar"))

from backend.collection_manager import CollectionManager  # noqa: E402


def _make_store(tmp_dir: str) -> SimpleNamespace:
    """Minimal fake store that satisfies CollectionManager's interface."""
    store = SimpleNamespace()
    store.data_dir = tmp_dir
    return store


class TestCreateCollection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))

    def test_create_collection_with_name(self) -> None:
        result = self._mgr.create_collection("Favourites", "my faves")
        self.assertEqual(result["name"], "Favourites")
        self.assertEqual(result["description"], "my faves")
        self.assertEqual(result["item_count"], 0)
        self.assertIn("created_at", result)

    def test_create_collection_duplicate_name_fails(self) -> None:
        self._mgr.create_collection("Dup")
        with self.assertRaises(ValueError) as ctx:
            self._mgr.create_collection("Dup")
        self.assertIn("уже существует", str(ctx.exception))

    def test_create_collection_empty_name_fails(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.create_collection("   ")


class TestItemCRUD(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))
        self._mgr.create_collection("Work")

    def test_add_item_to_collection(self) -> None:
        result = self._mgr.add_to_collection("Work", "item-001")
        self.assertEqual(result["name"], "Work")
        self.assertEqual(result["item_count"], 1)

    def test_add_same_item_twice_is_idempotent(self) -> None:
        self._mgr.add_to_collection("Work", "item-001")
        result = self._mgr.add_to_collection("Work", "item-001")
        self.assertEqual(result["item_count"], 1)

    def test_remove_item_from_collection(self) -> None:
        self._mgr.add_to_collection("Work", "item-001")
        result = self._mgr.remove_from_collection("Work", "item-001")
        self.assertEqual(result["item_count"], 0)

    def test_remove_nonexistent_item_is_safe(self) -> None:
        result = self._mgr.remove_from_collection("Work", "ghost")
        self.assertEqual(result["item_count"], 0)

    def test_add_to_missing_collection_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.add_to_collection("NoSuchCollection", "item-001")

    def test_add_empty_item_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("Work", "  ")


class TestDeleteCollection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))

    def test_delete_collection_removes_all_items(self) -> None:
        self._mgr.create_collection("Temp")
        self._mgr.add_to_collection("Temp", "item-1")
        self._mgr.add_to_collection("Temp", "item-2")
        deleted = self._mgr.delete_collection("Temp")
        self.assertTrue(deleted)
        cols = self._mgr.list_collections()
        self.assertFalse(any(c["name"] == "Temp" for c in cols))

    def test_delete_nonexistent_returns_false(self) -> None:
        result = self._mgr.delete_collection("Ghost")
        self.assertFalse(result)


class TestListCollections(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))

    def test_list_collections_returns_all(self) -> None:
        self._mgr.create_collection("Alpha")
        self._mgr.create_collection("Beta")
        self._mgr.create_collection("Gamma")
        cols = self._mgr.list_collections()
        names = {c["name"] for c in cols}
        self.assertSetEqual(names, {"Alpha", "Beta", "Gamma"})

    def test_list_empty_when_no_collections(self) -> None:
        cols = self._mgr.list_collections()
        self.assertEqual(cols, [])

    def test_get_collection_by_name(self) -> None:
        self._mgr.create_collection("Music", "tunes")
        self._mgr.add_to_collection("Music", "track-1")
        cols = self._mgr.list_collections()
        music = next(c for c in cols if c["name"] == "Music")
        self.assertEqual(music["description"], "tunes")
        self.assertEqual(music["item_count"], 1)


class TestBulkOperations(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))
        self._mgr.create_collection("Batch")

    def test_bulk_add_items(self) -> None:
        ids = [f"item-{i}" for i in range(5)]
        for item_id in ids:
            self._mgr.add_to_collection("Batch", item_id)
        cols = self._mgr.list_collections()
        batch = next(c for c in cols if c["name"] == "Batch")
        self.assertEqual(batch["item_count"], 5)

    def test_bulk_remove_items(self) -> None:
        ids = [f"item-{i}" for i in range(5)]
        for item_id in ids:
            self._mgr.add_to_collection("Batch", item_id)
        # Remove first 3
        for item_id in ids[:3]:
            self._mgr.remove_from_collection("Batch", item_id)
        cols = self._mgr.list_collections()
        batch = next(c for c in cols if c["name"] == "Batch")
        self.assertEqual(batch["item_count"], 2)


class TestRenameCollection(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._mgr = CollectionManager(_make_store(self._tmp))

    def test_rename_collection(self) -> None:
        self._mgr.create_collection("OldName")
        self._mgr.add_to_collection("OldName", "item-1")
        result = self._mgr.rename_collection("OldName", "NewName")
        self.assertEqual(result["name"], "NewName")
        self.assertEqual(result["item_count"], 1)
        cols = self._mgr.list_collections()
        names = {c["name"] for c in cols}
        self.assertIn("NewName", names)
        self.assertNotIn("OldName", names)

    def test_rename_to_existing_name_fails(self) -> None:
        self._mgr.create_collection("A")
        self._mgr.create_collection("B")
        with self.assertRaises(ValueError):
            self._mgr.rename_collection("A", "B")

    def test_rename_nonexistent_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.rename_collection("Ghost", "NewName")

    def test_rename_to_same_name_is_noop(self) -> None:
        self._mgr.create_collection("Same")
        result = self._mgr.rename_collection("Same", "Same")
        self.assertEqual(result["name"], "Same")


class TestPersistence(unittest.TestCase):
    def test_persistence_to_disk_and_reload(self) -> None:
        tmp = tempfile.mkdtemp()
        mgr1 = CollectionManager(_make_store(tmp))
        mgr1.create_collection("Persisted", "desc")
        mgr1.add_to_collection("Persisted", "item-A")
        mgr1.add_to_collection("Persisted", "item-B")

        # New instance loads from disk
        mgr2 = CollectionManager(_make_store(tmp))
        cols = mgr2.list_collections()
        self.assertEqual(len(cols), 1)
        col = cols[0]
        self.assertEqual(col["name"], "Persisted")
        self.assertEqual(col["description"], "desc")
        self.assertEqual(col["item_count"], 2)

    def test_collections_file_is_valid_json(self) -> None:
        tmp = tempfile.mkdtemp()
        mgr = CollectionManager(_make_store(tmp))
        mgr.create_collection("JsonCheck")
        raw = Path(tmp, "collections.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        self.assertIn("collections", data)
        self.assertIn("JsonCheck", data["collections"])


class TestConcurrentAccess(unittest.TestCase):
    def test_concurrent_access_safe(self) -> None:
        tmp = tempfile.mkdtemp()
        mgr = CollectionManager(_make_store(tmp))
        mgr.create_collection("Concurrent")

        errors: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(10):
                    mgr.add_to_collection("Concurrent", f"t{thread_id}-item-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        cols = mgr.list_collections()
        total = next(c for c in cols if c["name"] == "Concurrent")["item_count"]
        self.assertEqual(total, 50)


if __name__ == "__main__":
    unittest.main()
