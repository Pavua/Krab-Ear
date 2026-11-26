"""Частичный итог unarchive_items привязан к ID записей, а не к позиции или счётчику (карта ревью dots, 26.11).

Раньше сбой восстановления одной записи не попадал ни в unarchived_count, ни в not_found: клиент видел
«восстановлено 1 из 2» и не знал, КАКАЯ запись осталась в архиве. Теперь ответ перечисляет restored_ids и
failed_ids, а при сбое — complete=False + reason.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager  # noqa: E402
from test_archive_manager import FakeStore  # noqa: E402


class _FailingRestoreStore(FakeStore):
    """Восстановление записей с id из `fail_ids` падает (как диск/блокировка в проде)."""

    def __init__(self, data_dir: str, fail_ids: set[str]) -> None:
        super().__init__(data_dir)
        self._fail_ids = fail_ids

    def restore_history_item_raw(self, raw_dict):  # type: ignore[override]
        if str(raw_dict.get("id")) in self._fail_ids:
            raise OSError("диск недоступен")
        return super().restore_history_item_raw(raw_dict)


class UnarchivePartialIdsTestCase(unittest.TestCase):
    def _mgr(self, fail_ids: set[str]):
        tmp = tempfile.mkdtemp()
        store = _FailingRestoreStore(tmp, fail_ids)
        for i in ("a1", "a2", "a3"):
            store.add_fake_item(i, f"запись {i}")
        mgr = ArchiveManager(store=store)
        self.addCleanup(lambda: None)
        mgr.archive_items(item_ids=["a1", "a2", "a3"])
        return mgr

    def test_mixed_batch_names_every_outcome_by_id(self) -> None:
        mgr = self._mgr({"a2"})
        res = mgr.unarchive_items(["a1", "a2", "a3", "ghost"])
        self.assertEqual(res["unarchived_count"], 2)
        self.assertEqual(sorted(res["restored_ids"]), ["a1", "a3"])
        self.assertEqual(res["failed_ids"], ["a2"])
        self.assertEqual(res["not_found"], ["ghost"])
        self.assertIs(res["complete"], False)
        self.assertEqual(res["reason"], "restore_failed")
        # Упавшая запись осталась в архиве — её можно восстановить повторно.
        self.assertEqual([i["id"] for i in mgr.list_archived()], ["a2"])

    def test_full_success_is_complete_and_lists_ids(self) -> None:
        mgr = self._mgr(set())
        res = mgr.unarchive_items(["a1", "a2"])
        self.assertEqual(sorted(res["restored_ids"]), ["a1", "a2"])
        self.assertEqual(res["failed_ids"], [])
        self.assertIs(res["complete"], True)
        self.assertNotIn("reason", res)

    def test_ipc_handler_passes_the_ids_through(self) -> None:
        mgr = self._mgr({"a3"})
        res = mgr.handle_unarchive_items({"item_ids": ["a1", "a3"]})
        self.assertEqual(res["restored_ids"], ["a1"])
        self.assertEqual(res["failed_ids"], ["a3"])
        self.assertIs(res["complete"], False)

    def test_empty_request_has_the_same_shape(self) -> None:
        mgr = self._mgr(set())
        res = mgr.unarchive_items([])
        self.assertEqual(res["restored_ids"], [])
        self.assertEqual(res["failed_ids"], [])
        self.assertIs(res["complete"], True)


if __name__ == "__main__":
    unittest.main()
