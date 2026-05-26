"""Тесты PrivacyAuditLogger.clear() — W957 security fix.

Тесты:
  test_clear_removes_existing_log      — файл существует → clear() удаляет его.
  test_clear_handles_missing_file      — файл отсутствует → clear() идемпотентен.
  test_clear_handler_still_callable    — _handle_clear_privacy_audit_log метод доступен
                                         для unit-тестов/migration-скриптов.
  test_clear_not_in_ipc_dispatch       — W957: clear_privacy_audit_log НЕ в IPC dispatch.

Примечание W957: test_clear_returns_ok (IPC handler через BackendService) удалён.
Вместо него test_clear_not_in_ipc_dispatch гарантирует, что метод НЕ регистрирован
в dispatch table (compliance audit trail нельзя уничтожать через неавторизованный IPC).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Настройка PYTHONPATH для standalone запуска
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "KrabEar"))


class TestPrivacyAuditClear(unittest.TestCase):
    """Тесты PrivacyAuditLogger.clear() — compliance trail protection."""

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
    # test_clear_handler_still_callable (for tests/migration scripts only)
    # ------------------------------------------------------------------
    def test_clear_handler_still_callable(self) -> None:
        """_handle_clear_privacy_audit_log существует и вызывается для тестов/migration-скриптов.

        Метод НАМЕРЕННО не зарегистрирован в IPC dispatch (W957). Этот тест проверяет,
        что метод не удалён из класса — он нужен migration-скриптам и unit-тестам.
        """
        log_path = Path(self._tmpdir) / "privacy_audit.log"
        log_path.write_text(
            '{"ts":"2026-05-26T00:00:00+00:00","category":"test","action":"dummy","details":{}}\n',
            encoding="utf-8",
        )
        self.assertTrue(log_path.exists())

        from backend.privacy_audit import PrivacyAuditLogger, get_privacy_audit_logger
        PrivacyAuditLogger.reset_instance()
        real_instance = get_privacy_audit_logger(log_path=log_path)

        # Создаём минимальный stub с подмененным get_privacy_audit_logger
        from backend.service import BackendService
        import unittest.mock as mock

        with mock.patch("backend.service.get_privacy_audit_logger", return_value=real_instance):
            result = BackendService._handle_clear_privacy_audit_log(
                MagicMock(spec=BackendService),
                params={},
            )

        self.assertIn("ok", result, "Ответ должен содержать ключ 'ok'")
        self.assertTrue(result["ok"], "ok должен быть True")
        # Файл должен быть удалён (handler вызвал audit.clear())
        self.assertFalse(log_path.exists(), "Файл лога должен быть удалён handler'ом")

    # ------------------------------------------------------------------
    # test_clear_not_in_ipc_dispatch  (W957 security gate)
    # ------------------------------------------------------------------
    def test_clear_not_in_ipc_dispatch(self) -> None:
        """W957: clear_privacy_audit_log НЕ зарегистрирован в IPC dispatch table.

        Compliance audit trail нельзя уничтожать через неавторизованный IPC
        (W952 CRITICAL finding F-1). Этот тест является security regression gate —
        провал = compliance audit trail снова уязвим.

        Метод проверяет, что строка `"clear_privacy_audit_log": self._handle_...`
        отсутствует в source-коде handle_request (шаблон dict-key registration).
        Комментарии с именем метода допустимы — проверяется именно dict-key pattern.
        """
        from backend.service import BackendService
        import re
        import inspect

        source = inspect.getsource(BackendService.handle_request)

        # Ищем паттерн dict-key registration: "clear_privacy_audit_log": <callable>
        # Комментарии не вызывают false positives — они не содержат ": self._handle" рядом
        match = re.search(
            r'"clear_privacy_audit_log"\s*:\s*\S',
            source,
        )

        self.assertIsNone(
            match,
            "SECURITY (W957): 'clear_privacy_audit_log' НЕ должен быть зарегистрирован "
            "в IPC dispatch table (dict-key pattern). "
            "Это compliance audit trail — удалять его через неавторизованный IPC запрещено. "
            "Если ты видишь этот fail — W952 CRITICAL finding F-1 снова открыт.",
        )


if __name__ == "__main__":
    unittest.main()
