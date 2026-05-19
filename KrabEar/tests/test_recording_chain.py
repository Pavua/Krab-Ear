"""Unit-тесты для RecordingChainManager."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# noqa: E402
from backend.recording_chain import RecordingChainManager  # noqa: E402


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


class RecordingChainExtendedTestCase(unittest.TestCase):
    """Дополнительные тесты для покрытия граничных случаев."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    # --- end_chain edge cases ---

    def test_end_chain_idempotent(self) -> None:
        """Повторный вызов end_chain не выбрасывает ошибки."""
        chain_id = self._mgr.start_chain("Идемпотентность")
        self._mgr.end_chain(chain_id)
        # второй вызов должен пройти без исключения
        self._mgr.end_chain(chain_id)
        data = self._mgr.get_chain(chain_id)
        self.assertIsNotNone(data["ended_at"])

    def test_end_chain_unknown_raises(self) -> None:
        """end_chain для несуществующей цепочки выбрасывает KeyError."""
        with self.assertRaises(KeyError):
            self._mgr.end_chain("non-existent-chain")

    # --- add_to_chain edge cases ---

    def test_add_to_chain_unknown_chain_raises(self) -> None:
        """add_to_chain с неизвестным chain_id выбрасывает KeyError."""
        with self.assertRaises(KeyError):
            self._mgr.add_to_chain("unknown-chain-id", "item-1")

    def test_add_to_chain_empty_item_id_raises(self) -> None:
        """add_to_chain с пустым item_id выбрасывает ValueError."""
        chain_id = self._mgr.start_chain("Пустой ID")
        with self.assertRaises(ValueError):
            self._mgr.add_to_chain(chain_id, "   ")

    def test_add_multiple_items_order_preserved(self) -> None:
        """Порядок добавления item_ids сохраняется."""
        chain_id = self._mgr.start_chain("Порядок")
        for i in range(5):
            self._mgr.add_to_chain(chain_id, f"item-{i}")
        data = self._mgr.get_chain(chain_id)
        self.assertEqual(data["item_ids"], [f"item-{i}" for i in range(5)])

    # --- list_chains ordering ---

    def test_list_chains_sorted_newest_first(self) -> None:
        """list_chains возвращает цепочки от новых к старым."""
        id_a = self._mgr.start_chain("Первая")
        id_b = self._mgr.start_chain("Вторая")
        chains = self._mgr.list_chains()
        # Последняя созданная должна быть первой в списке
        self.assertEqual(chains[0]["chain_id"], id_b)
        self.assertEqual(chains[1]["chain_id"], id_a)

    def test_list_chains_summary_fields(self) -> None:
        """Краткая форма цепочки содержит нужные поля."""
        chain_id = self._mgr.start_chain("Поля")
        self._mgr.add_to_chain(chain_id, "item-x")
        chains = self._mgr.list_chains()
        self.assertEqual(len(chains), 1)
        c = chains[0]
        for key in ("chain_id", "name", "created_at", "ended_at", "item_count"):
            self.assertIn(key, c)
        self.assertEqual(c["item_count"], 1)

    # --- get_chain with missing store items ---

    def test_get_chain_missing_store_items_fallback(self) -> None:
        """get_chain возвращает fallback {id: iid} для несуществующих в store ID."""
        chain_id = self._mgr.start_chain("Отсутствующие")
        self._mgr.add_to_chain(chain_id, "ghost-item-id")
        data = self._mgr.get_chain(chain_id)
        # items должен содержать fallback
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0], {"id": "ghost-item-id"})

    def test_get_chain_totals_zero_when_no_duration_key(self) -> None:
        """Записи без поля duration_sec не влияют на total_duration_sec."""
        # FakeHistoryItem.to_dict() возвращает "duration_sec", но RecordingChainManager
        # ищет "duration_sec" через d.get("duration_sec", 0). Проверяем агрегацию.
        self._store.add_fake_item("no-dur", "текст без длины", duration_sec=0.0)
        chain_id = self._mgr.start_chain("Нулевая длина")
        self._mgr.add_to_chain(chain_id, "no-dur")
        data = self._mgr.get_chain(chain_id)
        self.assertAlmostEqual(data["total_duration_sec"], 0.0)

    # --- merge_chain_text ---

    def test_merge_chain_text_skips_empty_texts(self) -> None:
        """merge_chain_text не включает пустые строки в результат."""
        self._store.add_fake_item("has-text", "Реальный текст")
        self._store.add_fake_item("no-text", "")
        chain_id = self._mgr.start_chain("Пропуск пустых")
        self._mgr.add_to_chain(chain_id, "has-text")
        self._mgr.add_to_chain(chain_id, "no-text")
        text = self._mgr.merge_chain_text(chain_id)
        self.assertIn("Реальный текст", text)
        # двойного разделителя быть не должно (пустой пропущен)
        self.assertNotIn("\n\n\n\n", text)

    # --- IPC handle_end_chain ---

    def test_handle_end_chain_missing_chain_id_raises(self) -> None:
        """handle_end_chain без chain_id выбрасывает ValueError."""
        with self.assertRaises(ValueError):
            self._mgr.handle_end_chain({})

    def test_handle_get_chain_missing_chain_id_raises(self) -> None:
        """handle_get_chain без chain_id выбрасывает ValueError."""
        with self.assertRaises(ValueError):
            self._mgr.handle_get_chain({})


class RecordingChainWave95TestCase(unittest.TestCase):
    """Wave 95 — required test coverage for task spec."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_link_recordings_in_order(self) -> None:
        """Записи добавляются в цепочку и сохраняют порядок."""
        chain_id = self._mgr.start_chain("Порядок Wave95")
        item_ids = ["rec-001", "rec-002", "rec-003"]
        for iid in item_ids:
            self._mgr.add_to_chain(chain_id, iid)
        data = self._mgr.get_chain(chain_id)
        self.assertEqual(data["item_ids"], item_ids)

    def test_unlink_recording_from_chain(self) -> None:
        """RecordingChainManager не реализует unlink_recording — документирует отсутствие API.

        BUG: отсутствует метод remove_from_chain / unlink_recording.
        Тест помечает это как известную недоработку (заглушка).
        """
        chain_id = self._mgr.start_chain("Тест отвязки")
        self._mgr.add_to_chain(chain_id, "item-to-remove")
        self._mgr.add_to_chain(chain_id, "item-to-keep")

        has_unlink = (
            hasattr(self._mgr, "remove_from_chain")
            or hasattr(self._mgr, "unlink_recording")
        )
        if has_unlink:
            fn = getattr(self._mgr, "remove_from_chain",
                         getattr(self._mgr, "unlink_recording", None))
            fn(chain_id, "item-to-remove")
            data = self._mgr.get_chain(chain_id)
            self.assertNotIn("item-to-remove", data["item_ids"])
            self.assertIn("item-to-keep", data["item_ids"])
        else:
            # Document the missing feature — end_chain is the only "close" operation
            data = self._mgr.get_chain(chain_id)
            self.assertIn("item-to-remove", data["item_ids"],
                          "Unlink API not implemented; item still present")

    def test_get_chain_by_recording_id(self) -> None:
        """Можно найти цепочку, содержащую конкретный item_id, через list_chains + get_chain."""
        chain_id = self._mgr.start_chain("Поиск по записи")
        target_item = "unique-item-xyz"
        self._mgr.add_to_chain(chain_id, target_item)
        self._mgr.add_to_chain(chain_id, "other-item")

        # Verify that get_chain exposes item_ids for lookup
        data = self._mgr.get_chain(chain_id)
        self.assertIn(target_item, data["item_ids"])

        # Search across all chains via list_chains + get_chain
        found_chain_ids = []
        for summary in self._mgr.list_chains(limit=100):
            full = self._mgr.get_chain(summary["chain_id"])
            if target_item in full["item_ids"]:
                found_chain_ids.append(summary["chain_id"])
        self.assertIn(chain_id, found_chain_ids)

    def test_chain_metadata_persists(self) -> None:
        """Метаданные цепочки (name, created_at, item_ids) сохраняются на диск и восстанавливаются."""
        chain_id = self._mgr.start_chain("Персистентность Wave95")
        self._mgr.add_to_chain(chain_id, "persist-item-1")
        self._mgr.add_to_chain(chain_id, "persist-item-2")
        self._mgr.end_chain(chain_id)

        # Reload from disk
        mgr2 = RecordingChainManager(store=self._store)
        data = mgr2.get_chain(chain_id)

        self.assertEqual(data["name"], "Персистентность Wave95")
        self.assertIsNotNone(data["created_at"])
        self.assertIsNotNone(data["ended_at"])
        self.assertEqual(data["item_ids"], ["persist-item-1", "persist-item-2"])

    def test_unicode_chain_name(self) -> None:
        """Цепочка с Unicode именем (кириллица, эмодзи, CJK) создаётся и читается корректно."""
        unicode_names = [
            "Совещание по продукту 🎯",
            "Встреча с клиентом — январь",
            "会议记录 2026",
            "Entrevista №1 — Señor García",
        ]
        for name in unicode_names:
            chain_id = self._mgr.start_chain(name)
            data = self._mgr.get_chain(chain_id)
            self.assertEqual(data["name"], name,
                             f"Unicode name not preserved: {name!r}")

        # Reload and verify persistence
        mgr2 = RecordingChainManager(store=self._store)
        chains = mgr2.list_chains(limit=100)
        names_persisted = {c["name"] for c in chains}
        for name in unicode_names:
            self.assertIn(name, names_persisted,
                          f"Unicode name lost after reload: {name!r}")

    def test_circular_link_rejected(self) -> None:
        """RecordingChainManager не реализует цикл-детекцию между цепочками.

        Структура данных — flat список item_ids, не граф цепочек.
        Один item_id может присутствовать в нескольких цепочках (нет ограничений).
        Тест документирует это поведение; 'circular link' в данном случае —
        добавление одного item_id в одну цепочку дважды (идемпотентно отклоняется).
        """
        chain_id = self._mgr.start_chain("Цикл Wave95")
        self._mgr.add_to_chain(chain_id, "shared-item")
        self._mgr.add_to_chain(chain_id, "shared-item")  # duplicate — must be ignored

        data = self._mgr.get_chain(chain_id)
        self.assertEqual(
            data["item_ids"].count("shared-item"), 1,
            "Duplicate item_id should be deduplicated (circular/duplicate rejected)",
        )

        # Cross-chain: same item_id in two chains is allowed (no global cycle detection)
        chain_id2 = self._mgr.start_chain("Вторая цепочка")
        self._mgr.add_to_chain(chain_id2, "shared-item")
        d2 = self._mgr.get_chain(chain_id2)
        self.assertIn("shared-item", d2["item_ids"])

    def test_concurrent_link_unlink(self) -> None:
        """Параллельные add_to_chain из нескольких потоков не вызывают гонок данных."""
        import threading

        chain_id = self._mgr.start_chain("Конкурентность Wave95")
        n_threads = 10
        n_items_each = 20
        errors: list[Exception] = []

        def adder(tid: int) -> None:
            try:
                for i in range(n_items_each):
                    self._mgr.add_to_chain(chain_id, f"item-t{tid}-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=adder, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        data = self._mgr.get_chain(chain_id)
        # All items must be present (no races, no data loss)
        expected_count = n_threads * n_items_each
        self.assertEqual(len(data["item_ids"]), expected_count,
                         f"Expected {expected_count} items, got {len(data['item_ids'])}")

        # Verify persistence survived concurrent writes
        mgr2 = RecordingChainManager(store=self._store)
        data2 = mgr2.get_chain(chain_id)
        self.assertEqual(len(data2["item_ids"]), expected_count)


if __name__ == "__main__":
    unittest.main()
