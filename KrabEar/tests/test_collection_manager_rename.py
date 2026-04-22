"""Unit-тесты для CollectionManager.rename_collection и доп. покрытие."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.collection_manager import CollectionManager


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

    def add_fake_item(self, item_id: str, text: str) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


class RenameCollectionTestCase(unittest.TestCase):
    """Тесты rename_collection."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = CollectionManager(store=self._store)

    def test_rename_basic(self) -> None:
        """Успешное переименование: старое имя исчезает, новое появляется."""
        self._mgr.create_collection("Старое", "описание")
        result = self._mgr.rename_collection("Старое", "Новое")
        self.assertEqual(result["name"], "Новое")
        names = [c["name"] for c in self._mgr.list_collections()]
        self.assertIn("Новое", names)
        self.assertNotIn("Старое", names)

    def test_rename_preserves_items(self) -> None:
        """После переименования элементы коллекции сохраняются."""
        self._mgr.create_collection("Оригинал")
        for i in range(5):
            self._store.add_fake_item(f"id{i}", f"текст {i}")
            self._mgr.add_to_collection("Оригинал", f"id{i}")
        self._mgr.rename_collection("Оригинал", "Переименованная")
        result = self._mgr.list_collections()
        col = next(c for c in result if c["name"] == "Переименованная")
        self.assertEqual(col["item_count"], 5)

    def test_rename_preserves_description(self) -> None:
        """Описание коллекции сохраняется при переименовании."""
        self._mgr.create_collection("Имя", "Описание сохраняется")
        self._mgr.rename_collection("Имя", "НовоеИмя")
        col = next(
            c for c in self._mgr.list_collections() if c["name"] == "НовоеИмя"
        )
        self.assertEqual(col["description"] if "description" in col else "", "Описание сохраняется")

    def test_rename_persisted_to_file(self) -> None:
        """После переименования изменения сохраняются в JSON-файл."""
        self._mgr.create_collection("ДоПерименов")
        self._mgr.rename_collection("ДоПерименов", "ПослеПереименов")
        data = json.loads(
            (Path(self._tmpdir) / "collections.json").read_text(encoding="utf-8")
        )
        self.assertIn("ПослеПереименов", data["collections"])
        self.assertNotIn("ДоПерименов", data["collections"])

    def test_rename_nonexistent_raises_key_error(self) -> None:
        """Переименование несуществующей коллекции → KeyError."""
        with self.assertRaises(KeyError):
            self._mgr.rename_collection("НеСуществует", "НовоеИмя")

    def test_rename_to_empty_name_raises_value_error(self) -> None:
        """Переименование в пустую строку → ValueError."""
        self._mgr.create_collection("ТестИмя")
        with self.assertRaises(ValueError):
            self._mgr.rename_collection("ТестИмя", "  ")

    def test_rename_to_existing_name_raises_value_error(self) -> None:
        """Переименование в уже занятое имя → ValueError."""
        self._mgr.create_collection("А")
        self._mgr.create_collection("Б")
        with self.assertRaises(ValueError):
            self._mgr.rename_collection("А", "Б")

    def test_rename_to_same_name_is_noop(self) -> None:
        """Переименование в то же самое имя — допустимо (идемпотентно)."""
        self._mgr.create_collection("Одно")
        result = self._mgr.rename_collection("Одно", "Одно")
        self.assertEqual(result["name"], "Одно")
        names = [c["name"] for c in self._mgr.list_collections()]
        self.assertEqual(names.count("Одно"), 1)

    def test_rename_loaded_by_new_manager(self) -> None:
        """Переименование персистится: новый менеджер видит новое имя."""
        self._mgr.create_collection("ОригИмя")
        self._mgr.rename_collection("ОригИмя", "НовоеИмяПерсист")
        mgr2 = CollectionManager(store=self._store)
        names = [c["name"] for c in mgr2.list_collections()]
        self.assertIn("НовоеИмяПерсист", names)
        self.assertNotIn("ОригИмя", names)

    def test_get_items_works_after_rename(self) -> None:
        """get_collection_items работает после переименования."""
        self._mgr.create_collection("ИмяА")
        self._store.add_fake_item("x99", "текст х99")
        self._mgr.add_to_collection("ИмяА", "x99")
        self._mgr.rename_collection("ИмяА", "ИмяБ")
        items = self._mgr.get_collection_items("ИмяБ")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "x99")

    def test_handle_rename_collection_ipc(self) -> None:
        """IPC-обработчик handle_rename_collection работает корректно."""
        self._mgr.create_collection("ИпцОриг")
        result = self._mgr.handle_rename_collection(
            {"old_name": "ИпцОриг", "new_name": "ИпцНовое"}
        )
        self.assertEqual(result["name"], "ИпцНовое")

    def test_handle_rename_missing_old_name_raises(self) -> None:
        """IPC без old_name → RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._mgr.handle_rename_collection({"new_name": "Х"})

    def test_handle_rename_missing_new_name_raises(self) -> None:
        """IPC без new_name → RuntimeError."""
        self._mgr.create_collection("СуществТест")
        with self.assertRaises(RuntimeError):
            self._mgr.handle_rename_collection({"old_name": "СуществТест"})

    def test_handle_rename_nonexistent_raises_runtime(self) -> None:
        """IPC переименование несуществующей коллекции → RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._mgr.handle_rename_collection(
                {"old_name": "НеТакое", "new_name": "НовоеИмя"}
            )


if __name__ == "__main__":
    unittest.main()
