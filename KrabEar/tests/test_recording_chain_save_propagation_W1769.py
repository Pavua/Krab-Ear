"""RC-4 W1769 — _save() silent-failure regression guard.

Раньше RecordingChainManager._save() глотал ошибку записи
(`except Exception: logger.exception(...)` + return), из-за чего:

  (a) мутирующие IPC-операции рапортовали успех, хотя на диск ничего не легло
      (ложный успех — теряется при перезапуске backend);
  (b) delete_all_chains() (privacy-purge) сообщал об «успешной» очистке, в то
      время как recording_chains.json с открытыми именами цепочек (потенциально
      PII) оставался на диске.

Фикс: _save() пробрасывает исключение наружу.  Мутирующие handle_*-операции
возвращают error-envelope `{"ok": False, "error": ...}`; delete_all_chains()
пробрасывает сбой, чтобы HistoryService.handle_purge_all_data зарегистрировал
шаг "chains" в secondary_errors (а не отрапортовал чистый успех).

Эти тесты ПАДАЮТ на старом коде (swallow) и ПРОХОДЯТ на исправленном.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# noqa: E402
from backend.recording_chain import RecordingChainManager  # noqa: E402


class FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir


def _patch_atomic_write_to_raise() -> Any:
    """Контекст-менеджер: финальный атомарный rename падает с OSError.

    Патчим Path.replace (последний шаг атомарной записи) — tmp-файл успевает
    записаться, но переименование в recording_chains.json не происходит, ровно
    как при read-only / full диске.  Это самый показательный сценарий для
    privacy-purge: исходный файл с PII остаётся на месте.
    """
    return mock.patch.object(
        Path, "replace", side_effect=OSError(28, "No space left on device")
    )


class SaveErrorPropagationTestCase(unittest.TestCase):

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._store = FakeStore(data_dir=self._tmpdir)
        self._mgr = RecordingChainManager(store=self._store)

    # ------------------------------------------------------------------
    # (1) _save() пробрасывает ошибку наружу, а не глотает
    # ------------------------------------------------------------------

    def test_save_propagates_oserror(self) -> None:
        """_save() должен пробросить OSError, а не вернуться нормально."""
        with _patch_atomic_write_to_raise():
            with self.assertRaises(OSError):
                self._mgr._save()

    # ------------------------------------------------------------------
    # (2) Мутирующая IPC-операция возвращает error-envelope
    # ------------------------------------------------------------------

    def test_handle_add_to_chain_returns_error_envelope_on_persist_failure(
        self,
    ) -> None:
        """add_to_chain не должен рапортовать ok=True, если запись не легла."""
        chain_id = self._mgr.start_chain("Совещание")
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_add_to_chain(
                {"chain_id": chain_id, "item_id": "item-1"}
            )
        self.assertIsInstance(resp, dict)
        self.assertFalse(
            resp.get("ok", False),
            "persist-сбой должен давать ok=False, а не ложный успех",
        )
        self.assertIn("error", resp)
        self.assertIn("persist_failed", str(resp.get("error", "")))

    def test_handle_start_chain_returns_error_envelope_on_persist_failure(
        self,
    ) -> None:
        """start_chain не должен возвращать chain_id, если запись не легла."""
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_start_chain({"name": "Новая цепочка"})
        self.assertFalse(resp.get("ok", False))
        self.assertNotIn("chain_id", resp)
        self.assertIn("error", resp)

    def test_handle_end_chain_returns_error_envelope_on_persist_failure(
        self,
    ) -> None:
        chain_id = self._mgr.start_chain("Закрываемая")
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_end_chain({"chain_id": chain_id})
        self.assertFalse(resp.get("ok", False))
        self.assertIn("error", resp)

    def test_handle_unlink_returns_error_envelope_on_persist_failure(
        self,
    ) -> None:
        chain_id = self._mgr.start_chain("Отвязка")
        self._mgr.add_to_chain(chain_id, "item-1")
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_unlink_recording_from_chain(
                {"chain_id": chain_id, "item_id": "item-1"}
            )
        self.assertFalse(resp.get("ok", False))
        self.assertIn("error", resp)

    def test_validation_errors_still_raise_not_swallowed(self) -> None:
        """Валидационные ошибки (пустой chain_id) по-прежнему бросаются,
        а не маскируются под persist-сбой."""
        with self.assertRaises(ValueError):
            self._mgr.handle_add_to_chain({"chain_id": "", "item_id": "x"})

    # ------------------------------------------------------------------
    # (3) privacy-purge: delete_all_chains пробрасывает сбой, а не врёт
    # ------------------------------------------------------------------

    def test_delete_all_chains_propagates_failure_not_false_success(self) -> None:
        """delete_all_chains() при сбое записи должен пробросить исключение,
        чтобы privacy-purge не отрапортовал чистый успех.

        Дополнительно проверяем, что файл с именем цепочки (PII) НЕ исчез — это
        ровно тот сценарий, ради которого нужна пропагация: память очищена, а
        cleartext на диске пережил «успешную» очистку.
        """
        self._mgr.start_chain("Секретное совещание PII")
        chains_path = Path(self._tmpdir) / "recording_chains.json"
        self.assertTrue(chains_path.exists(), "файл должен существовать до purge")

        with _patch_atomic_write_to_raise():
            with self.assertRaises(OSError):
                self._mgr.delete_all_chains()

        # Файл с открытым именем цепочки всё ещё на диске → пропагация даёт
        # вызывающему шанс зарегистрировать сбой вместо ложного «очищено».
        self.assertTrue(
            chains_path.exists(),
            "cleartext-файл пережил сбойную очистку — поэтому сбой ОБЯЗАН всплыть",
        )

    def test_purge_caller_surfaces_chains_failure(self) -> None:
        """Симуляция privacy-purge wiring: вызывающий ловит исключение из
        delete_all_chains() и регистрирует шаг "chains" в secondary_errors
        (ровно как HistoryService.handle_purge_all_data:1713-1719)."""
        self._mgr.start_chain("Цепочка для очистки")
        secondary_errors: list[str] = []

        with _patch_atomic_write_to_raise():
            try:
                self._mgr.delete_all_chains()
            except Exception:
                secondary_errors.append("chains")

        self.assertIn(
            "chains",
            secondary_errors,
            "сбой очистки цепочек должен попадать в secondary_errors, "
            "а не теряться в проглоченном исключении",
        )

    # ------------------------------------------------------------------
    # Happy-path не сломан: успешная запись по-прежнему персистится
    # ------------------------------------------------------------------

    def test_save_success_path_unchanged(self) -> None:
        chain_id = self._mgr.start_chain("Обычная цепочка")
        chains_path = Path(self._tmpdir) / "recording_chains.json"
        self.assertTrue(chains_path.exists())
        # Перезагрузка из файла видит цепочку → данные реально на диске.
        reloaded = RecordingChainManager(store=self._store)
        names = {c["name"] for c in reloaded.list_chains()}
        self.assertIn("Обычная цепочка", names)
        # И delete_all_chains в норме чистит файл.
        deleted = reloaded.delete_all_chains()
        self.assertGreaterEqual(deleted, 1)
        self.assertEqual(reloaded.list_chains(), [])
        # chain_id валиден (не None / не пустой) на happy-path.
        self.assertTrue(chain_id)


if __name__ == "__main__":
    unittest.main()


class FailedSaveRollsBackMemoryTestCase(unittest.TestCase):
    """G3 audio-05 A05-08: отклонённая мутация не остаётся в памяти.

    Раньше после ошибки записи изменение оставалось в self._data: API отвечал
    ok=False, но list/get показывали «несохранённое», а СЛЕДУЮЩАЯ удачная
    запись молча сохраняла отклонённую операцию на диск.
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self._tmpdir, True)
        self._mgr = RecordingChainManager(store=FakeStore(data_dir=self._tmpdir))
        self._chain_id = self._mgr.start_chain("База")
        self._mgr.add_to_chain(self._chain_id, "item-keep")

    def _reloaded(self) -> RecordingChainManager:
        return RecordingChainManager(store=FakeStore(data_dir=self._tmpdir))

    def test_rejected_add_not_persisted_by_next_save(self) -> None:
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_add_to_chain(
                {"chain_id": self._chain_id, "item_id": "item-rejected"}
            )
        self.assertFalse(resp.get("ok", False))
        self.assertNotIn("item-rejected",
                         self._mgr.get_chain(self._chain_id)["item_ids"])
        self._mgr.add_to_chain(self._chain_id, "item-next")
        self.assertEqual(
            self._reloaded().get_chain(self._chain_id)["item_ids"],
            ["item-keep", "item-next"],
        )

    def test_rejected_start_chain_not_listed(self) -> None:
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_start_chain({"name": "Отклонённая"})
        self.assertFalse(resp.get("ok", False))
        names = [c["name"] for c in self._mgr.list_chains(limit=50)]
        self.assertNotIn("Отклонённая", names)

    def test_rejected_end_chain_keeps_chain_open(self) -> None:
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_end_chain({"chain_id": self._chain_id})
        self.assertFalse(resp.get("ok", False))
        self.assertIsNone(self._mgr.get_chain(self._chain_id)["ended_at"])

    def test_rejected_unlink_keeps_item(self) -> None:
        with _patch_atomic_write_to_raise():
            resp = self._mgr.handle_unlink_recording_from_chain(
                {"chain_id": self._chain_id, "item_id": "item-keep"}
            )
        self.assertFalse(resp.get("ok", False))
        self.assertIn("item-keep", self._mgr.get_chain(self._chain_id)["item_ids"])

    def test_rejected_replace_keeps_old_ids(self) -> None:
        with _patch_atomic_write_to_raise():
            with self.assertRaises(OSError):
                self._mgr.replace_items_in_chain(self._chain_id, ["item-keep"], "merged")
        self.assertEqual(self._mgr.get_chain(self._chain_id)["item_ids"], ["item-keep"])
