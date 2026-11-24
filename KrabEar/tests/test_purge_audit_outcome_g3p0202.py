"""G3-P02-02: purge не выдаёт complete=True без ПОДТВЕРЖДЁННОЙ записи обязательного аудита.

Контракт (ревью G3-P02-02, проект A–D):
  * реальный `PrivacyAuditLogger.log_event` ловит ошибку записи ВНУТРИ и ничего не
    бросает — try/except вокруг него в purge её не видел, и `complete=True` мог
    уйти без записи комплаенс-журнала;
  * строгий вызов `log_event_strict` возвращает один из трёх исходов:
    confirmed / not_written (сбой ДО записи) / unknown (сбой во время/после);
  * факт удаления и итог аудита хранятся раздельно: ответ остаётся `ok=True` с
    честными счётчиками, а поле `privacy_audit` сообщает исход аудита;
  * полный успех (`complete=True`) — только при `confirmed`;
  * прежний необязательный `log_event` не менялся (None, без исключений).
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from _settings_test_helpers import safe_backend_for_settings  # noqa: F401  (изоляция настроек)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.privacy_audit as pa_module  # noqa: E402
from backend.privacy_audit import (  # noqa: E402
    AUDIT_CONFIRMED,
    AUDIT_NOT_WRITTEN,
    AUDIT_UNKNOWN,
    PrivacyAuditLogger,
)
from test_purge_all_data_w1730 import _make_svc  # noqa: E402


def _logger_with_unwritable_path(root: str) -> PrivacyAuditLogger:
    """Логгер, у которого ЗАПИСЬ упадёт до первого байта.

    Конструктор сам создаёт каталог ключа, поэтому логгер строится на нормальном
    пути, а путь журнала подменяется уже после: родитель — обычный файл, mkdir
    падает в `_ensure_parent` (фаза «до записи»).
    """
    log = PrivacyAuditLogger(log_path=Path(root) / "ok" / "privacy_audit.log")
    blocker = Path(root) / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")
    log._log_path = blocker / "privacy_audit.log"
    return log


def _flaky_fsync_for(path: Path):
    """fsync, который падает только для файла аудита (остальные fsync — настоящие)."""
    path.touch()
    target_ino = path.stat().st_ino
    real_fsync = os.fsync

    def flaky_fsync(fd: int) -> None:
        if os.fstat(fd).st_ino == target_ino:
            raise OSError(errno.EIO, "fsync аудита упал")
        real_fsync(fd)

    return flaky_fsync


class _StubAudit:
    """Подмена логгера аудита с заданным поведением строгого вызова."""

    def __init__(self, result: Any = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.calls = 0

    def log_event_strict(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._result

    def log_event(self, **kwargs: Any) -> None:  # прежний API — purge его звать не должен
        raise AssertionError("purge обязан звать log_event_strict, а не log_event")


class PrivacyAuditLoggerOutcomeTests(unittest.TestCase):
    """Юнит: три исхода строгой записи + неизменность log_event."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = self._td.name
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)

    def test_confirmed_when_fsync_succeeds(self) -> None:
        log = PrivacyAuditLogger(log_path=Path(self.root) / "a.log")
        out = log.log_event_strict("privacy", "x", {"k": 1})
        self.assertEqual(out, AUDIT_CONFIRMED)
        # подтверждение не пустое слово: запись реально читается из файла
        self.assertEqual([e["action"] for e in log.read_entries()], ["x"])

    def test_not_written_when_failure_precedes_write(self) -> None:
        log = _logger_with_unwritable_path(self.root)
        self.assertEqual(log.log_event_strict("privacy", "x"), AUDIT_NOT_WRITTEN)

    def test_unknown_when_fsync_fails_after_write_started(self) -> None:
        path = Path(self.root) / "a.log"
        log = PrivacyAuditLogger(log_path=path)
        with mock.patch.object(pa_module.os, "fsync", _flaky_fsync_for(path)):
            out = log.log_event_strict("privacy", "x")
        # запись могла лечь на диск — «не записано» утверждать нельзя
        self.assertEqual(out, AUDIT_UNKNOWN)

    def test_legacy_log_event_unchanged_returns_none_and_swallows(self) -> None:
        good = PrivacyAuditLogger(log_path=Path(self.root) / "ok.log")
        self.assertIsNone(good.log_event("privacy", "x"))
        bad = _logger_with_unwritable_path(self.root)
        self.assertIsNone(bad.log_event("privacy", "x"))  # не бросает


class PurgeAuditOutcomeTests(unittest.TestCase):
    """Интеграция: ответ purge различает удаление и подтверждение аудита."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = self._td.name
        PrivacyAuditLogger.reset_instance()
        self.addCleanup(PrivacyAuditLogger.reset_instance)

    def _purge_with_logger(self, logger_obj: Any, items: list[str] | None = None) -> dict[str, Any]:
        svc = _make_svc(self.root, item_ids=items or ["a"])
        # Парный успех: без патча удаление ключа в тестовом окружении само даёт
        # errors=["encryption_key"] и complete=False — проверки complete ниже стали бы
        # вхолостую. С патчем единственный источник неуспеха — исход аудита.
        with mock.patch("backend.crypto_keystore.delete_history_key", return_value=True), \
                mock.patch.object(pa_module, "get_privacy_audit_logger", lambda **kw: logger_obj):
            return svc.handle_purge_all_data({"confirm": True})

    def test_real_logger_write_failure_is_not_complete(self) -> None:
        """Корень P1: реальный логгер глотает сбой — complete не вправе быть True."""
        result = self._purge_with_logger(_logger_with_unwritable_path(self.root))
        # факт удаления сохранён и честен
        self.assertTrue(result["ok"])
        self.assertEqual(result["history_deleted"], 1)
        # аудит доказанно не записан
        self.assertEqual(result["privacy_audit"], "not_written")
        self.assertIs(result["complete"], False)
        self.assertEqual(result["errors"], ["privacy_audit"])

    def test_real_logger_unknown_outcome_is_not_complete(self) -> None:
        path = Path(self.root) / "audit.log"
        with mock.patch.object(pa_module.os, "fsync", _flaky_fsync_for(path)):
            result = self._purge_with_logger(PrivacyAuditLogger(log_path=path))
        self.assertTrue(result["ok"])
        self.assertEqual(result["history_deleted"], 1)
        self.assertEqual(result["privacy_audit"], "unknown")
        self.assertIs(result["complete"], False)
        self.assertIn("privacy_audit", result["errors"])

    def test_confirmed_real_logger_keeps_normal_response(self) -> None:
        result = self._purge_with_logger(
            PrivacyAuditLogger(log_path=Path(self.root) / "audit.log")
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["privacy_audit"], "confirmed")
        self.assertNotIn("privacy_audit", result["errors"])
        # единственный путь к complete=True: подтверждённый аудит + остальные шаги успешны
        self.assertIs(result["complete"], True)
        self.assertEqual(result["errors"], [])
        # прежние поля ответа на месте (совместимость потребителей)
        for key in ("history_deleted", "complete", "errors", "encryption_key_shredded"):
            self.assertIn(key, result)

    def test_exception_on_strict_call_is_unknown_not_complete(self) -> None:
        stub = _StubAudit(exc=OSError("disk full"))
        result = self._purge_with_logger(stub)
        self.assertEqual(stub.calls, 1)
        self.assertTrue(result["ok"])
        self.assertEqual(result["privacy_audit"], "unknown")
        self.assertIs(result["complete"], False)
        self.assertIn("privacy_audit", result["errors"])

    def test_explicit_not_written_from_logger_is_not_complete(self) -> None:
        result = self._purge_with_logger(_StubAudit(result=AUDIT_NOT_WRITTEN))
        self.assertEqual(result["privacy_audit"], "not_written")
        self.assertIs(result["complete"], False)

    def test_unexpected_return_value_is_unknown(self) -> None:
        """True/None/произвольный объект вместо константы — не подтверждение (fail-closed)."""
        for garbage in (True, None, "ok", object()):
            with self.subTest(garbage=garbage):
                result = self._purge_with_logger(_StubAudit(result=garbage))
                self.assertEqual(result["privacy_audit"], "unknown")
                self.assertIs(result["complete"], False)

    def test_logger_without_strict_api_is_unknown(self) -> None:
        class OldLogger:
            def log_event(self, **kwargs: Any) -> None:
                return None

        result = self._purge_with_logger(OldLogger())
        self.assertEqual(result["privacy_audit"], "unknown")
        self.assertIs(result["complete"], False)

    def test_purge_is_not_repeated_on_audit_failure(self) -> None:
        """Сбой аудита не запускает удаление повторно и не откатывает его."""
        stub = _StubAudit(result=AUDIT_NOT_WRITTEN)
        result = self._purge_with_logger(stub, items=["a", "b"])
        self.assertEqual(stub.calls, 1)  # один вызов аудита, без повторов
        self.assertEqual(result["history_deleted"], 2)


if __name__ == "__main__":
    unittest.main()
