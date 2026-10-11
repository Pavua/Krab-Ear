"""Терминальный результат миграции шифрования доступен через get_history_encryption_status.

Раньше итог фонового потока (backup_cleanup_failed, verification_failed, ...) уходил ТОЛЬКО в
событие history_encryption.migrate.progress; подписчик SSE мог его пропустить, и UI показывал
«Готово» при оставшемся plaintext .bak. Теперь последний терминальный статус хранится в памяти
процесса и отдаётся аддитивным ключом `last_migration_status`.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _service(migrate_result=None, migrate_exc=None):
    from backend.service import BackendService

    obj = object.__new__(BackendService)
    store = MagicMock()
    if migrate_exc is not None:
        store.migrate_history_encryption.side_effect = migrate_exc
    else:
        store.migrate_history_encryption.return_value = migrate_result
    store.get_history_encryption_status.return_value = {
        "enabled": True, "total": 3, "encrypted": 3, "plaintext": 0, "pct": 100,
    }
    object.__setattr__(obj, "store", store)
    return obj


def _run_migration(obj) -> dict:
    with patch("backend.history_crypto.build_history_crypto", return_value=object()):
        started = obj._handle_migrate_history_encryption({})
    for t in threading.enumerate():
        if t.name == "history-enc-migration":
            t.join(timeout=10)
    return started


class MigrationTerminalStatusTest(unittest.TestCase):
    def test_no_migration_yet_reports_none(self) -> None:
        obj = _service({"ok": True, "total": 3, "encrypted": 3})
        self.assertIsNone(obj._handle_get_history_encryption_status({})["last_migration_status"])

    def test_done_is_recorded(self) -> None:
        obj = _service({"ok": True, "total": 3, "encrypted": 3})
        self.assertTrue(_run_migration(obj)["ok"])
        status = obj._handle_get_history_encryption_status({})
        self.assertEqual(status["last_migration_status"], "done")
        self.assertFalse(status["migrating"])

    def test_backup_cleanup_failed_is_recorded(self) -> None:
        obj = _service({"ok": False, "reason": "backup_cleanup_failed", "total": 3, "encrypted": 3})
        _run_migration(obj)
        self.assertEqual(
            obj._handle_get_history_encryption_status({})["last_migration_status"],
            "backup_cleanup_failed",
        )

    def test_exception_is_recorded_as_error(self) -> None:
        obj = _service(migrate_exc=OSError("boom"))
        _run_migration(obj)
        status = obj._handle_get_history_encryption_status({})
        self.assertEqual(status["last_migration_status"], "error")
        self.assertFalse(status["migrating"])

    def test_new_run_resets_previous_status(self) -> None:
        obj = _service({"ok": False, "reason": "verification_failed", "total": 1, "encrypted": 0})
        _run_migration(obj)
        self.assertEqual(
            obj._handle_get_history_encryption_status({})["last_migration_status"],
            "verification_failed",
        )
        obj.store.migrate_history_encryption.return_value = {"ok": True, "total": 1, "encrypted": 1}
        gate = threading.Event()
        real = obj.store.migrate_history_encryption

        def slow(progress_cb=None):
            gate.wait(timeout=5)
            return {"ok": True, "total": 1, "encrypted": 1}

        obj.store.migrate_history_encryption = slow
        with patch("backend.history_crypto.build_history_crypto", return_value=object()):
            obj._handle_migrate_history_encryption({})
        mid = obj._handle_get_history_encryption_status({})
        self.assertIsNone(mid["last_migration_status"])
        self.assertTrue(mid["migrating"])
        gate.set()
        for t in threading.enumerate():
            if t.name == "history-enc-migration":
                t.join(timeout=10)
        self.assertEqual(
            obj._handle_get_history_encryption_status({})["last_migration_status"], "done"
        )
        del real


if __name__ == "__main__":
    unittest.main()
