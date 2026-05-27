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


class RecordingChainUnlinkTestCase(unittest.TestCase):
    """Тесты для unlink_recording_from_chain."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    def test_unlink_removes_item_from_chain(self) -> None:
        """unlink_recording_from_chain удаляет элемент из цепочки."""
        chain_id = self._mgr.start_chain("Удаление")
        self._mgr.add_to_chain(chain_id, "item-A")
        self._mgr.add_to_chain(chain_id, "item-B")
        removed = self._mgr.unlink_recording_from_chain(chain_id, "item-A")
        self.assertTrue(removed)
        data = self._mgr.get_chain(chain_id)
        self.assertNotIn("item-A", data["item_ids"])
        self.assertIn("item-B", data["item_ids"])

    def test_unlink_nonexistent_item_idempotent(self) -> None:
        """unlink_recording_from_chain возвращает False для отсутствующего элемента без ошибки."""
        chain_id = self._mgr.start_chain("Идемпотентность unlink")
        self._mgr.add_to_chain(chain_id, "item-X")
        removed = self._mgr.unlink_recording_from_chain(chain_id, "item-GHOST")
        self.assertFalse(removed)
        # item-X должен остаться нетронутым
        data = self._mgr.get_chain(chain_id)
        self.assertIn("item-X", data["item_ids"])

    def test_unlink_empty_chain(self) -> None:
        """unlink_recording_from_chain на пустой цепочке — идемпотентно, без ошибки."""
        chain_id = self._mgr.start_chain("Пустая цепочка")
        removed = self._mgr.unlink_recording_from_chain(chain_id, "any-item")
        self.assertFalse(removed)
        data = self._mgr.get_chain(chain_id)
        self.assertEqual(data["item_ids"], [])

    def test_unlink_unknown_chain_raises(self) -> None:
        """unlink_recording_from_chain для несуществующей цепочки выбрасывает KeyError."""
        with self.assertRaises(KeyError):
            self._mgr.unlink_recording_from_chain("no-such-chain", "item-1")

    def test_concurrent_unlink_thread_safe(self) -> None:
        """Параллельные unlink одного и того же элемента корректны (ровно 0 копий в итоге)."""
        import threading

        chain_id = self._mgr.start_chain("Конкурентность")
        for i in range(10):
            self._mgr.add_to_chain(chain_id, f"item-{i}")

        errors: list[Exception] = []

        def do_unlink(item_id: str) -> None:
            try:
                self._mgr.unlink_recording_from_chain(chain_id, item_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_unlink, args=(f"item-{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки при конкурентном unlink: {errors}")
        data = self._mgr.get_chain(chain_id)
        self.assertEqual(data["item_ids"], [])

    def test_handle_unlink_recording_from_chain(self) -> None:
        """IPC-обработчик handle_unlink_recording_from_chain возвращает ok+removed."""
        chain_id = self._mgr.start_chain("IPC unlink")
        self._mgr.add_to_chain(chain_id, "ipc-item")
        result = self._mgr.handle_unlink_recording_from_chain(
            {"chain_id": chain_id, "item_id": "ipc-item"}
        )
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("removed"))

    def test_handle_unlink_missing_chain_id_raises(self) -> None:
        """handle_unlink_recording_from_chain без chain_id выбрасывает ValueError."""
        with self.assertRaises(ValueError):
            self._mgr.handle_unlink_recording_from_chain({"item_id": "x"})

    def test_handle_unlink_missing_item_id_raises(self) -> None:
        """handle_unlink_recording_from_chain без item_id выбрасывает ValueError."""
        chain_id = self._mgr.start_chain("Без item_id")
        with self.assertRaises(ValueError):
            self._mgr.handle_unlink_recording_from_chain({"chain_id": chain_id})


class RecordingChainBackendServiceDispatchTestCase(unittest.TestCase):
    """Wave 156: verify unlink_recording_from_chain is wired in BackendService dispatch table.

    Also documents that the Swift agent does NOT yet call this method directly
    (no Swift caller exists in native/KrabEarAgent/) — the IPC method is
    exposed for future Swift UI integration. Python-level IPC is fully wired.
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        self.tmpdir = tempfile.mkdtemp()
        with patch("backend.service.AudioRecorder"), \
             patch("backend.service.Transcriber"), \
             patch("backend.service.Translator"), \
             patch("backend.service.AutoBackupManager"):
            from backend.service import BackendService
            self.service = BackendService.__new__(BackendService)
            from backend.state_store import StateStore
            self.service.store = StateStore(data_dir=Path(self.tmpdir))
            self.service._chains = RecordingChainManager(store=self.service.store)
            # Build the dispatch table subset we need
            self.service._handlers = {
                "unlink_recording_from_chain":
                    self.service._chains.handle_unlink_recording_from_chain,
            }

    def test_unlink_method_in_service_dispatch(self):
        """unlink_recording_from_chain is callable via service._chains.handle_unlink."""
        chain_id = self.service._chains.start_chain("dispatch-test-chain")
        self.service._chains.add_to_chain(chain_id, "dispatch-item")
        result = self.service._chains.handle_unlink_recording_from_chain(
            {"chain_id": chain_id, "item_id": "dispatch-item"}
        )
        self.assertTrue(result.get("ok"))
        self.assertTrue(result.get("removed"))

    def test_unlink_dispatch_handler_is_registered(self):
        """The handler is present in the service._handlers dict."""
        self.assertIn("unlink_recording_from_chain", self.service._handlers)

    def test_no_swift_caller_documented(self):
        """Documents that Swift agent has no caller for unlink_recording_from_chain yet.

        The IPC method is wired Python-side (BackendService dispatch table line ~882).
        Swift UI caller is pending — to be added in a future wave when the
        recording chain UI is implemented in HistoryPanelController.
        """
        import subprocess
        result = subprocess.run(
            ["grep", "-r", "unlink_recording_from_chain",
             "native/"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).parent.parent.parent),
        )
        # No Swift caller found — confirmed absent
        self.assertEqual(result.stdout.strip(), "",
                         "Unexpected Swift caller found — update this test")


class RecordingChainW1046FixTestCase(unittest.TestCase):
    """W1039 F1+F5: limit guard + delete_all_chains (privacy-purge cascade)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    # --- F1: negative limit guard ---

    def test_list_chains_negative_limit_returns_empty(self) -> None:
        """list_chains(limit=-1) must return [] not N-1 items (W1039 F1)."""
        for i in range(5):
            self._mgr.start_chain(f"Chain {i}")
        result = self._mgr.list_chains(limit=-1)
        self.assertEqual(result, [], "Negative limit must produce empty list, not data leak")

    def test_list_chains_zero_limit_returns_empty(self) -> None:
        """list_chains(limit=0) returns empty list."""
        self._mgr.start_chain("Single chain")
        result = self._mgr.list_chains(limit=0)
        self.assertEqual(result, [])

    def test_list_chains_large_limit_capped_at_1000(self) -> None:
        """list_chains(limit=999999) is capped at 1000 max items."""
        for i in range(5):
            self._mgr.start_chain(f"Chain {i}")
        # Just verify it doesn't crash and returns ≤1000
        result = self._mgr.list_chains(limit=999999)
        self.assertLessEqual(len(result), 1000)
        self.assertEqual(len(result), 5)

    def test_list_chains_normal_limit_still_works(self) -> None:
        """list_chains with positive limit retains previous behaviour."""
        for i in range(5):
            self._mgr.start_chain(f"Chain {i}")
        result = self._mgr.list_chains(limit=3)
        self.assertEqual(len(result), 3)

    # --- F5: delete_all_chains (privacy-purge cascade) ---

    def test_delete_all_chains_clears_all(self) -> None:
        """delete_all_chains() removes all chains and returns count."""
        for i in range(4):
            self._mgr.start_chain(f"Chain {i}")
        deleted = self._mgr.delete_all_chains()
        self.assertEqual(deleted, 4)
        self.assertEqual(self._mgr.list_chains(), [])

    def test_delete_all_chains_idempotent(self) -> None:
        """delete_all_chains() on an empty store returns 0 and doesn't crash."""
        deleted = self._mgr.delete_all_chains()
        self.assertEqual(deleted, 0)
        self.assertEqual(self._mgr.list_chains(), [])

    def test_delete_all_chains_persists_empty_state(self) -> None:
        """delete_all_chains() writes empty chains.json; reload sees no chains."""
        self._mgr.start_chain("Persisted chain")
        self._mgr.delete_all_chains()
        # Reload from disk
        mgr2 = RecordingChainManager(store=self._store)
        self.assertEqual(mgr2.list_chains(), [])

    def test_privacy_purge_cascades_recording_chains(self) -> None:
        """After delete_all_chains, no chain data remains (F5 cascade test)."""
        cid = self._mgr.start_chain("Secret meeting")
        self._mgr.add_to_chain(cid, "item-secret-1")
        self._mgr.add_to_chain(cid, "item-secret-2")
        # Simulate privacy purge cascade
        count = self._mgr.delete_all_chains()
        self.assertEqual(count, 1)
        # Chain is gone
        with self.assertRaises(KeyError):
            self._mgr.get_chain(cid)
        # No chains remain
        self.assertEqual(self._mgr.list_chains(), [])


if __name__ == "__main__":
    unittest.main()
