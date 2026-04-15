"""Unit-тесты для CollectionManager."""

from __future__ import annotations
from backend.collection_manager import CollectionManager

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeHistoryItem:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text}


class FakeStore:
    """Минимальный фейк StateStore для тестов CollectionManager."""

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


class CollectionManagerCRUDTestCase(unittest.TestCase):
    """Тесты создания, удаления и списка коллекций."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_create_collection_returns_dict(self) -> None:
        result = self._mgr.create_collection("Работа", "Рабочие записи")
        self.assertEqual(result["name"], "Работа")
        self.assertEqual(result["description"], "Рабочие записи")
        self.assertEqual(result["item_count"], 0)
        self.assertIn("created_at", result)

    def test_create_collection_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.create_collection("   ")

    def test_create_duplicate_collection_raises(self) -> None:
        self._mgr.create_collection("Дубль")
        with self.assertRaises(ValueError):
            self._mgr.create_collection("Дубль")

    def test_list_collections_empty(self) -> None:
        result = self._mgr.list_collections()
        self.assertEqual(result, [])

    def test_list_collections_after_create(self) -> None:
        self._mgr.create_collection("A")
        self._mgr.create_collection("B")
        result = self._mgr.list_collections()
        names = {c["name"] for c in result}
        self.assertIn("A", names)
        self.assertIn("B", names)

    def test_delete_existing_collection(self) -> None:
        self._mgr.create_collection("УдалитьМеня")
        deleted = self._mgr.delete_collection("УдалитьМеня")
        self.assertTrue(deleted)
        names = [c["name"] for c in self._mgr.list_collections()]
        self.assertNotIn("УдалитьМеня", names)

    def test_delete_nonexistent_collection_returns_false(self) -> None:
        result = self._mgr.delete_collection("НеСуществует")
        self.assertFalse(result)

    def test_collections_persisted_to_file(self) -> None:
        self._mgr.create_collection("Персист")
        path = Path(self._tmpdir) / "collections.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("Персист", data["collections"])

    def test_collections_loaded_from_existing_file(self) -> None:
        """Новый менеджер должен подхватить уже созданные коллекции."""
        self._mgr.create_collection("Сохранённая")
        mgr2 = CollectionManager(store=self._store)
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("Сохранённая", names)


class CollectionManagerItemsTestCase(unittest.TestCase):
    """Тесты добавления/удаления элементов и получения содержимого коллекции."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)
        self._mgr.create_collection("Тест")

    def test_add_to_collection_increments_count(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        result = self._mgr.add_to_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 1)

    def test_add_same_item_twice_is_idempotent(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        self._mgr.add_to_collection("Тест", "id1")
        result = self._mgr.add_to_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 1)

    def test_add_to_nonexistent_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.add_to_collection("НеСуществует", "id1")

    def test_add_empty_item_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("Тест", "  ")

    def test_remove_from_collection(self) -> None:
        self._store.add_fake_item("id1", "текст 1")
        self._mgr.add_to_collection("Тест", "id1")
        result = self._mgr.remove_from_collection("Тест", "id1")
        self.assertEqual(result["item_count"], 0)

    def test_remove_nonexistent_item_is_noop(self) -> None:
        result = self._mgr.remove_from_collection("Тест", "несуществующий")
        self.assertEqual(result["item_count"], 0)

    def test_remove_from_nonexistent_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.remove_from_collection("НеТа", "id1")

    def test_get_collection_items_returns_history(self) -> None:
        self._store.add_fake_item("id1", "привет мир")
        self._mgr.add_to_collection("Тест", "id1")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "id1")
        self.assertEqual(items[0]["text"], "привет мир")

    def test_get_collection_items_skips_deleted_items(self) -> None:
        """Записи, удалённые из истории, не должны попадать в результат."""
        self._mgr.add_to_collection("Тест", "не-существует")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual(items, [])

    def test_get_collection_items_unknown_collection_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.get_collection_items("НеСуществует")

    def test_multiple_items_order_preserved(self) -> None:
        for i in range(3):
            self._store.add_fake_item(f"id{i}", f"текст {i}")
            self._mgr.add_to_collection("Тест", f"id{i}")
        items = self._mgr.get_collection_items("Тест")
        self.assertEqual([it["id"] for it in items], ["id0", "id1", "id2"])


class CollectionManagerIPCHandlersTestCase(unittest.TestCase):
    """Тесты IPC-обработчиков."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_handle_create_collection(self) -> None:
        result = self._mgr.handle_create_collection({"name": "ИПЦ", "description": "тест"})
        self.assertEqual(result["name"], "ИПЦ")

    def test_handle_create_collection_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_create_collection({"description": "без имени"})

    def test_handle_delete_collection(self) -> None:
        self._mgr.create_collection("Удалить")
        result = self._mgr.handle_delete_collection({"name": "Удалить"})
        self.assertTrue(result["deleted"])

    def test_handle_delete_collection_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_delete_collection({})

    def test_handle_list_collections(self) -> None:
        self._mgr.create_collection("Список")
        result = self._mgr.handle_list_collections({})
        self.assertIn("collections", result)
        names = [c["name"] for c in result["collections"]]
        self.assertIn("Список", names)

    def test_handle_add_to_collection(self) -> None:
        self._mgr.create_collection("ДобавитьСюда")
        self._store.add_fake_item("x1", "текст")
        result = self._mgr.handle_add_to_collection(
            {"collection_name": "ДобавитьСюда", "item_id": "x1"}
        )
        self.assertEqual(result["item_count"], 1)

    def test_handle_add_to_collection_missing_params_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection({"collection_name": "X"})

    def test_handle_remove_from_collection(self) -> None:
        self._mgr.create_collection("УдалитьИз")
        self._store.add_fake_item("x2", "текст")
        self._mgr.add_to_collection("УдалитьИз", "x2")
        result = self._mgr.handle_remove_from_collection(
            {"collection_name": "УдалитьИз", "item_id": "x2"}
        )
        self.assertEqual(result["item_count"], 0)

    def test_handle_get_collection_items(self) -> None:
        self._mgr.create_collection("ПолучитьИз")
        self._store.add_fake_item("y1", "содержимое")
        self._mgr.add_to_collection("ПолучитьИз", "y1")
        result = self._mgr.handle_get_collection_items({"collection_name": "ПолучитьИз"})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["id"], "y1")

    def test_handle_get_collection_items_missing_name_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_get_collection_items({})

    def test_handle_add_to_nonexistent_collection_raises_runtime(self) -> None:
        with self.assertRaises(RuntimeError):
            self._mgr.handle_add_to_collection(
                {"collection_name": "НеСуществует", "item_id": "id1"}
            )

    def test_item_count_in_list_reflects_members(self) -> None:
        self._mgr.create_collection("Счётчик")
        for i in range(5):
            self._store.add_fake_item(f"cnt{i}", f"текст {i}")
            self._mgr.add_to_collection("Счётчик", f"cnt{i}")
        cols = self._mgr.list_collections()
        col = next(c for c in cols if c["name"] == "Счётчик")
        self.assertEqual(col["item_count"], 5)


if __name__ == "__main__":
    unittest.main()
