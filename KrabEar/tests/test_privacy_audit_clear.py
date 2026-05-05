"""Тесты clear_privacy_audit_log IPC handler + PrivacyAuditLogger.clear().

3 теста:
  test_clear_removes_existing_log  — файл существует → clear() удаляет его.
  test_clear_handles_missing_file  — файл отсутствует → clear() возвращает ok=True (идемпотент).
  test_clear_returns_ok            — handler через BackendService возвращает {ok: True}.
"""

from __future__ import annotations

import json
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Настройка PYTHONPATH для standalone запуска
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "KrabEar"))


class TestPrivacyAuditClear(unittest.TestCase):
    """Тесты PrivacyAuditLogger.clear() и хендлера _handle_clear_privacy_audit_log."""

    def setUp(self) -> None:
        # Сбрасываем singleton перед каждым тестом
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_logger(self) -> "PrivacyAuditLogger":  # noqa: F821
        from backend.privacy_audit import PrivacyAuditLogger, get_privacy_audit_logger
        PrivacyAuditLogger.reset_instance()
        log_path = Path(self._tmpdir) / "privacy_audit.log"
        return get_privacy_audit_logger(log_path=log_path)

    # ------------------------------------------------------------------
    # test_clear_removes_existing_log
    # ------------------------------------------------------------------
    def test_clear_removes_existing_log(self) -> None:
        """Если файл лога существует, clear() удаляет его."""
        audit = self._make_logger()

        # Записываем тестовое событие
        audit.log_event(category="sentry", action="blocked", details={"dsn": "redacted"})
        self.assertTrue(audit._log_path.exists(), "Файл лога должен существовать после записи")
        self.assertEqual(audit.total_count(), 1)

        # Очищаем
        audit.clear()

        self.assertFalse(audit._log_path.exists(), "Файл лога должен быть удалён после clear()")
        self.assertEqual(audit.total_count(), 0, "total_count() должен вернуть 0 после clear()")
        self.assertEqual(audit.read_entries(), [], "read_entries() должен вернуть [] после clear()")

    # ------------------------------------------------------------------
    # test_clear_handles_missing_file
    # ------------------------------------------------------------------
    def test_clear_handles_missing_file(self) -> None:
        """Если файл лога отсутствует, clear() завершается без ошибок (идемпотентно)."""
        audit = self._make_logger()

        # Убеждаемся, что файла нет
        self.assertFalse(audit._log_path.exists())

        # clear() не должен бросать исключение
        try:
            audit.clear()
        except Exception as exc:  # pragma: no cover
            self.fail(f"clear() не должен бросать исключение при отсутствии файла: {exc}")

        # После clear() состояние корректное
        self.assertFalse(audit._log_path.exists())
        self.assertEqual(audit.total_count(), 0)

    # ------------------------------------------------------------------
    # test_clear_returns_ok
    # ------------------------------------------------------------------
    def test_clear_returns_ok(self) -> None:
        """_handle_clear_privacy_audit_log возвращает {ok: True}."""
        log_path = Path(self._tmpdir) / "privacy_audit.log"

        # Пишем тестовую строку напрямую в файл
        log_path.write_text(
            json.dumps({"ts": "2026-05-05T00:00:00+00:00", "category": "test", "action": "dummy", "details": {}})
            + "\n",
            encoding="utf-8",
        )
        self.assertTrue(log_path.exists())

        # Подменяем get_privacy_audit_logger чтобы избежать default path
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

        with patch("backend.service.get_privacy_audit_logger") as mock_factory:
            from backend.privacy_audit import PrivacyAuditLogger as PAL, get_privacy_audit_logger as real_factory
            PAL.reset_instance()
            real_instance = real_factory(log_path=log_path)
            mock_factory.return_value = real_instance

            # Создаём минимальный stub BackendService с нужным handler'ом
            from backend.service import BackendService

            # Вызываем handler напрямую через unbound метод
            result = BackendService._handle_clear_privacy_audit_log(
                MagicMock(spec=BackendService),
                params={},
            )

        self.assertIn("ok", result, "Ответ должен содержать ключ 'ok'")
        self.assertTrue(result["ok"], "ok должен быть True")


if __name__ == "__main__":
    unittest.main()
