"""Тесты системы тегов для истории транскрипций Krab Ear."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.history_service import HistoryService


def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _make_service(store: StateStore) -> HistoryService:
    return HistoryService(store=store)


def _add_item(service: HistoryService, text: str = "hello world") -> str:
    result = service.handle_add_history_item({"text": text})
    return result["id"]


class TestAddTag(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_tag_returns_id_and_tags(self):
        item_id = _add_item(self.svc)
        result = self.svc.handle_add_tag({"id": item_id, "tag": "meeting"})
        self.assertEqual(result["id"], item_id)
        self.assertIn("meeting", result["tags"])

    def test_add_tag_idempotent(self):
        item_id = _add_item(self.svc)
        self.svc.handle_add_tag({"id": item_id, "tag": "important"})
        result = self.svc.handle_add_tag({"id": item_id, "tag": "important"})
        self.assertEqual(result["tags"].count("important"), 1)

    def test_add_multiple_tags(self):
        item_id = _add_item(self.svc)
        self.svc.handle_add_tag({"id": item_id, "tag": "meeting"})
        result = self.svc.handle_add_tag({"id": item_id, "tag": "important"})
        self.assertIn("meeting", result["tags"])
        self.assertIn("important", result["tags"])

    def test_add_tag_missing_id_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_tag({"tag": "meeting"})

    def test_add_tag_missing_tag_raises(self):
        item_id = _add_item(self.svc)
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_tag({"id": item_id})

    def test_add_tag_nonexistent_item_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_add_tag({"id": "nonexistent-id", "tag": "meeting"})


class TestRemoveTag(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_remove_tag(self):
        item_id = _add_item(self.svc)
        self.svc.handle_add_tag({"id": item_id, "tag": "meeting"})
        result = self.svc.handle_remove_tag({"id": item_id, "tag": "meeting"})
        self.assertNotIn("meeting", result["tags"])

    def test_remove_tag_not_present_is_noop(self):
        item_id = _add_item(self.svc)
        self.svc.handle_add_tag({"id": item_id, "tag": "keep"})
        result = self.svc.handle_remove_tag({"id": item_id, "tag": "absent"})
        self.assertIn("keep", result["tags"])

    def test_remove_tag_nonexistent_item_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_remove_tag({"id": "bad-id", "tag": "meeting"})


class TestGetTags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_tags_empty(self):
        item_id = _add_item(self.svc)
        result = self.svc.handle_get_tags({"id": item_id})
        self.assertEqual(result["tags"], [])

    def test_get_tags_after_add(self):
        item_id = _add_item(self.svc)
        self.svc.handle_add_tag({"id": item_id, "tag": "alpha"})
        self.svc.handle_add_tag({"id": item_id, "tag": "beta"})
        result = self.svc.handle_get_tags({"id": item_id})
        self.assertIn("alpha", result["tags"])
        self.assertIn("beta", result["tags"])

    def test_get_tags_nonexistent_item_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_get_tags({"id": "no-such-id"})


class TestSearchByTag(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_by_tag_finds_items(self):
        id1 = _add_item(self.svc, "transcript one")
        id2 = _add_item(self.svc, "transcript two")
        self.svc.handle_add_tag({"id": id1, "tag": "meeting"})
        self.svc.handle_add_tag({"id": id2, "tag": "other"})
        result = self.svc.handle_search_by_tag({"tag": "meeting"})
        ids = [item["id"] for item in result["items"]]
        self.assertIn(id1, ids)
        self.assertNotIn(id2, ids)

    def test_search_by_tag_empty_result(self):
        _add_item(self.svc)
        result = self.svc.handle_search_by_tag({"tag": "nonexistent"})
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["items"], [])

    def test_search_by_tag_missing_tag_raises(self):
        with self.assertRaises(RuntimeError):
            self.svc.handle_search_by_tag({})


class TestListAllTags(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_all_tags_empty(self):
        _add_item(self.svc)
        result = self.svc.handle_list_all_tags({})
        self.assertEqual(result["tags"], [])

    def test_list_all_tags_with_counts(self):
        id1 = _add_item(self.svc, "first")
        id2 = _add_item(self.svc, "second")
        id3 = _add_item(self.svc, "third")
        self.svc.handle_add_tag({"id": id1, "tag": "meeting"})
        self.svc.handle_add_tag({"id": id2, "tag": "meeting"})
        self.svc.handle_add_tag({"id": id3, "tag": "important"})
        result = self.svc.handle_list_all_tags({})
        tags_map = {entry["tag"]: entry["count"] for entry in result["tags"]}
        self.assertEqual(tags_map.get("meeting"), 2)
        self.assertEqual(tags_map.get("important"), 1)

    def test_list_all_tags_sorted_by_count_desc(self):
        id1 = _add_item(self.svc, "a")
        id2 = _add_item(self.svc, "b")
        id3 = _add_item(self.svc, "c")
        self.svc.handle_add_tag({"id": id1, "tag": "rare"})
        self.svc.handle_add_tag({"id": id2, "tag": "common"})
        self.svc.handle_add_tag({"id": id3, "tag": "common"})
        result = self.svc.handle_list_all_tags({})
        counts = [entry["count"] for entry in result["tags"]]
        self.assertEqual(counts, sorted(counts, reverse=True))


class TestTagsPersistence(unittest.TestCase):
    """Проверяет, что теги сохраняются между созданием новых объектов store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_tags_persist_across_store_reload(self):
        data_dir = Path(self._tmp.name)
        store1 = _make_store(data_dir)
        svc1 = _make_service(store1)
        item_id = _add_item(svc1, "persist test")
        svc1.handle_add_tag({"id": item_id, "tag": "persist"})

        # Create a new store instance pointing to the same directory
        store2 = _make_store(data_dir)
        svc2 = _make_service(store2)
        result = svc2.handle_get_tags({"id": item_id})
        self.assertIn("persist", result["tags"])


if __name__ == "__main__":
    unittest.main()
