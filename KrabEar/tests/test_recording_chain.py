"""Unit-тесты для RecordingChainManager."""

from __future__ import annotations
from backend.recording_chain import RecordingChainManager

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class FakeHistoryItem:
    def __init__(self, item_id: str, text: str, duration_sec: float = 0.0) -> None:
        self.id = item_id
        self.text = text
        self.duration_sec = duration_sec

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "duration_sec": self.duration_sec}


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, FakeHistoryItem] = {}

    def add_fake_item(self, item_id: str, text: str, duration_sec: float = 0.0) -> FakeHistoryItem:
        item = FakeHistoryItem(item_id, text, duration_sec)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str):
        return self._items.get(item_id)


class RecordingChainCRUDTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_start_chain_returns_chain_id(self) -> None:
        chain_id = self._mgr.start_chain("Совещание по проекту")
        self.assertIsInstance(chain_id, str)
        self.assertTrue(len(chain_id) > 0)

    def test_start_chain_empty_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.start_chain("   ")

    def test_add_to_chain_appends_item(self) -> None:
        chain_id = self._mgr.start_chain("Тест")
        self._mgr.add_to_chain(chain_id, "item-1")
        data = self._mgr.get_chain(chain_id)
        self.assertIn("item-1", data["item_ids"])

    def test_add_duplicate_item_ignored(self) -> None:
        chain_id = self._mgr.start_chain("Тест дублей")
        self._mgr.add_to_chain(chain_id, "item-1")
        self._mgr.add_to_chain(chain_id, "item-1")
        data = self._mgr.get_chain(chain_id)
        self.assertEqual(data["item_ids"].count("item-1"), 1)

    def test_end_chain_sets_ended_at(self) -> None:
        chain_id = self._mgr.start_chain("Завершение")
        self._mgr.end_chain(chain_id)
        data = self._mgr.get_chain(chain_id)
        self.assertIsNotNone(data["ended_at"])

    def test_add_to_ended_chain_raises(self) -> None:
        chain_id = self._mgr.start_chain("Закрытая")
        self._mgr.end_chain(chain_id)
        with self.assertRaises(RuntimeError):
            self._mgr.add_to_chain(chain_id, "item-new")

    def test_get_chain_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            self._mgr.get_chain("nonexistent-id")

    def test_list_chains_empty(self) -> None:
        self.assertEqual(self._mgr.list_chains(), [])

    def test_list_chains_returns_summary(self) -> None:
        self._mgr.start_chain("Цепочка A")
        self._mgr.start_chain("Цепочка B")
        chains = self._mgr.list_chains()
        names = {c["name"] for c in chains}
        self.assertIn("Цепочка A", names)
        self.assertIn("Цепочка B", names)

    def test_list_chains_limit(self) -> None:
        for i in range(5):
            self._mgr.start_chain(f"Цепочка {i}")
        chains = self._mgr.list_chains(limit=3)
        self.assertEqual(len(chains), 3)

    def test_get_chain_totals_duration_and_words(self) -> None:
        self._store.add_fake_item("a", "один два три", duration_sec=10.0)
        self._store.add_fake_item("b", "четыре пять", duration_sec=5.0)
        chain_id = self._mgr.start_chain("Суммарная")
        self._mgr.add_to_chain(chain_id, "a")
        self._mgr.add_to_chain(chain_id, "b")
        data = self._mgr.get_chain(chain_id)
        self.assertAlmostEqual(data["total_duration_sec"], 15.0)
        self.assertEqual(data["total_word_count"], 5)

    def test_merge_chain_text_concatenates(self) -> None:
        self._store.add_fake_item("x", "Первая часть")
        self._store.add_fake_item("y", "Вторая часть")
        chain_id = self._mgr.start_chain("Слияние")
        self._mgr.add_to_chain(chain_id, "x")
        self._mgr.add_to_chain(chain_id, "y")
        text = self._mgr.merge_chain_text(chain_id)
        self.assertIn("Первая часть", text)
        self.assertIn("Вторая часть", text)

    def test_merge_chain_text_empty_chain(self) -> None:
        chain_id = self._mgr.start_chain("Пустая")
        text = self._mgr.merge_chain_text(chain_id)
        self.assertEqual(text, "")

    def test_persistence_survives_reload(self) -> None:
        chain_id = self._mgr.start_chain("Сохранение")
        self._mgr.add_to_chain(chain_id, "item-persist")
        # Перезагружаем менеджер с тем же data_dir
        mgr2 = RecordingChainManager(store=self._store)
        data = mgr2.get_chain(chain_id)
        self.assertIn("item-persist", data["item_ids"])


class RecordingChainIPCHandlersTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_handle_start_chain(self) -> None:
        result = self._mgr.handle_start_chain({"name": "IPC Цепочка"})
        self.assertIn("chain_id", result)

    def test_handle_start_chain_missing_name_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_start_chain({})

    def test_handle_add_to_chain(self) -> None:
        chain_id = self._mgr.start_chain("Добавление IPC")
        result = self._mgr.handle_add_to_chain({"chain_id": chain_id, "item_id": "ipc-item-1"})
        self.assertTrue(result.get("ok"))

    def test_handle_end_chain(self) -> None:
        chain_id = self._mgr.start_chain("Завершение IPC")
        result = self._mgr.handle_end_chain({"chain_id": chain_id})
        self.assertTrue(result.get("ok"))

    def test_handle_get_chain(self) -> None:
        chain_id = self._mgr.start_chain("Получение IPC")
        result = self._mgr.handle_get_chain({"chain_id": chain_id})
        self.assertEqual(result["chain_id"], chain_id)

    def test_handle_list_chains(self) -> None:
        self._mgr.start_chain("Список IPC")
        result = self._mgr.handle_list_chains({})
        self.assertIn("chains", result)
        self.assertIsInstance(result["chains"], list)

    def test_handle_merge_chain_text(self) -> None:
        self._store.add_fake_item("m1", "Текст раз")
        chain_id = self._mgr.start_chain("Слияние IPC")
        self._mgr.add_to_chain(chain_id, "m1")
        result = self._mgr.handle_merge_chain_text({"chain_id": chain_id})
        self.assertIn("Текст раз", result["text"])

    def test_handle_add_missing_chain_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_add_to_chain({"item_id": "x"})

    def test_handle_add_missing_item_id_raises(self) -> None:
        chain_id = self._mgr.start_chain("Пропущен item_id")
        with self.assertRaises(ValueError):
            self._mgr.handle_add_to_chain({"chain_id": chain_id})


if __name__ == "__main__":
    unittest.main()
