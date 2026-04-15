"""Тесты системы избранного (favorites/bookmarks) для истории Krab Ear."""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _make_service(store: StateStore) -> HistoryService:
    return HistoryService(store=store)


def _add_item(service: HistoryService, text: str = "test item") -> str:
    result = service.handle_add_history_item({"text": text})
    return result["id"]


class TestToggleFavorite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_toggle_favorite_on(self):
        """Первый toggle устанавливает favorite=True."""
        item_id = _add_item(self.svc)
        result = self.svc.handle_toggle_favorite({"id": item_id})
        self.assertEqual(result["id"], item_id)
        self.assertTrue(result["favorite"])

    def test_toggle_favorite_off(self):
        """Второй toggle снимает favorite обратно в False."""
        item_id = _add_item(self.svc)
        self.svc.handle_toggle_favorite({"id": item_id})
        result = self.svc.handle_toggle_favorite({"id": item_id})
        self.assertFalse(result["favorite"])

    def test_toggle_favorite_missing_id(self):
        """Пустой id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_toggle_favorite({"id": ""})

    def test_toggle_favorite_unknown_id(self):
        """Несуществующий id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_toggle_favorite({"id": "nonexistent-id"})

    def test_toggle_persists_across_reload(self):
        """Флаг избранного сохраняется после пересоздания store."""
        item_id = _add_item(self.svc)
        self.svc.handle_toggle_favorite({"id": item_id})

        # Перезагружаем store из того же каталога
        new_store = _make_store(Path(self._tmp.name))
        new_svc = _make_service(new_store)
        result = new_svc.handle_is_favorite({"id": item_id})
        self.assertTrue(result["favorite"])


class TestIsFavorite(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_is_favorite_default_false(self):
        """Новый элемент не является избранным по умолчанию."""
        item_id = _add_item(self.svc)
        result = self.svc.handle_is_favorite({"id": item_id})
        self.assertEqual(result["id"], item_id)
        self.assertFalse(result["favorite"])

    def test_is_favorite_after_toggle(self):
        """После toggle is_favorite возвращает True."""
        item_id = _add_item(self.svc)
        self.svc.handle_toggle_favorite({"id": item_id})
        result = self.svc.handle_is_favorite({"id": item_id})
        self.assertTrue(result["favorite"])

    def test_is_favorite_missing_id(self):
        """Пустой id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_is_favorite({"id": ""})

    def test_is_favorite_unknown_id(self):
        """Несуществующий id вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_is_favorite({"id": "does-not-exist"})


class TestGetFavorites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_service(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_favorites_empty(self):
        """При отсутствии избранных возвращает пустой список."""
        _add_item(self.svc, "not favorite")
        result = self.svc.handle_get_favorites({})
        self.assertEqual(result["items"], [])
        self.assertEqual(result["count"], 0)

    def test_get_favorites_returns_only_favorited(self):
        """Возвращает только помеченные элементы."""
        id1 = _add_item(self.svc, "item one")
        id2 = _add_item(self.svc, "item two")
        _add_item(self.svc, "item three")
        self.svc.handle_toggle_favorite({"id": id1})
        self.svc.handle_toggle_favorite({"id": id2})

        result = self.svc.handle_get_favorites({})
        self.assertEqual(result["count"], 2)
        ids = [item["id"] for item in result["items"]]
        self.assertIn(id1, ids)
        self.assertIn(id2, ids)

    def test_get_favorites_sorted_newest_first(self):
        """Избранные возвращаются новые первыми (обратный порядок вставки)."""
        id1 = _add_item(self.svc, "older item")
        id2 = _add_item(self.svc, "newer item")
        self.svc.handle_toggle_favorite({"id": id1})
        self.svc.handle_toggle_favorite({"id": id2})

        result = self.svc.handle_get_favorites({})
        self.assertEqual(result["items"][0]["id"], id2)
        self.assertEqual(result["items"][1]["id"], id1)

    def test_get_favorites_removes_unfavorited(self):
        """После снятия метки элемент исчезает из get_favorites."""
        item_id = _add_item(self.svc, "going in and out")
        self.svc.handle_toggle_favorite({"id": item_id})
        self.assertEqual(self.svc.handle_get_favorites({})["count"], 1)
        # снимаем метку
        self.svc.handle_toggle_favorite({"id": item_id})
        self.assertEqual(self.svc.handle_get_favorites({})["count"], 0)

    def test_favorite_field_in_to_dict(self):
        """Поле favorite сериализуется в to_dict элемента."""
        item_id = _add_item(self.svc, "check field")
        self.svc.handle_toggle_favorite({"id": item_id})
        result = self.svc.handle_get_favorites({})
        self.assertTrue(result["items"][0]["favorite"])


if __name__ == "__main__":
    unittest.main()
