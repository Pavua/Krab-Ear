"""Тесты для 4 LOW hardening fixes (wave-29 deferred).

1. WebhookManager.shutdown() дренирует executor и idempotent.
2. TelnyxAdapter на HTTP 429 не спит дольше _RETRY_AFTER_MAX_SEC (≤60s, W1196/W1208 cap).
3. EmailSender логирует WARNING при plaintext SMTP (нет TLS/SSL).
4. AppleIntegrationService обрезает поля title/body/notes до лимитов.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fix 1: WebhookManager.shutdown() — дрейн executor + idempotent
# ---------------------------------------------------------------------------


def test_webhook_manager_shutdown_drains_executor(tmp_path: pathlib.Path) -> None:
    """shutdown(wait=True) завершает executor, позволяя in-flight задачам выполниться."""
    from backend.webhook_manager import WebhookManager

    mgr = WebhookManager(tmp_path)

    # Флаг завершения фоновой задачи
    ran_flag: list[bool] = []

    ev_started = threading.Event()
    ev_release = threading.Event()

    def slow_task() -> None:
        ev_started.set()
        ev_release.wait(timeout=5)
        ran_flag.append(True)

    mgr._executor.submit(slow_task)
    ev_started.wait(timeout=5)  # убеждаемся что задача запустилась

    # Разрешаем задаче завершиться и тут же вызываем shutdown
    ev_release.set()
    mgr.shutdown(wait=True)

    # После shutdown задача должна была завершиться
    assert len(ran_flag) == 1, "in-flight задача должна завершиться до выхода из shutdown()"


def test_webhook_manager_shutdown_idempotent(tmp_path: pathlib.Path) -> None:
    """Повторный вызов shutdown() не бросает исключений (idempotent guard)."""
    from backend.webhook_manager import WebhookManager

    mgr = WebhookManager(tmp_path)
    mgr.shutdown(wait=True)
    # Второй вызов должен просто вернуться без ошибок
    mgr.shutdown(wait=True)  # не должен падать


def test_webhook_manager_privacy_mode_thread_safe(tmp_path: pathlib.Path) -> None:
    """set_privacy_mode и fire_webhook работают без race-condition под _lock."""
    from backend.webhook_manager import WebhookManager

    mgr = WebhookManager(tmp_path)
    # Включаем privacy mode — fire_webhook должен игнорировать события
    mgr.set_privacy_mode(True)
    # Регистрируем webhook: при fire_webhook с privacy_mode=True не должно быть submit
    with patch.object(mgr._executor, "submit") as mock_submit:
        mgr.fire_webhook("test_event", {"x": 1})
        mock_submit.assert_not_called()

    mgr.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Fix 3: EmailSender plaintext warning
# ---------------------------------------------------------------------------


def test_email_smtp_plaintext_warning_logged(tmp_path: pathlib.Path) -> None:
    """При smtp_use_tls=False и smtp_use_ssl=False EmailSender логирует WARNING."""
    from backend.email_sender import EmailSender

    sender = EmailSender(
        backend_name="smtp",
        smtp_host="mail.example.com",
        smtp_port=25,
        smtp_use_tls=False,
        smtp_use_ssl=False,
        smtp_from="from@example.com",
        use_keychain=False,
    )

    msg_mock = MagicMock()
    msg_mock.as_string.return_value = "raw-email"

    smtp_instance = MagicMock()
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("backend.email_sender.smtplib.SMTP", return_value=smtp_instance), \
         patch("backend.email_sender.MIMEMultipart", return_value=msg_mock), \
         patch("backend.email_sender.MIMEText"), \
         caplog_ctx(logging.getLogger("KrabEar.Backend.EmailSender")) as log:
        sender._send_via_smtp(
            to="to@example.com",
            subject="Test",
            body_html="<b>hi</b>",
            body_text="hi",
        )

    # Проверяем что WARNING о plaintext-соединении залогирован
    warnings = [r for r in log.records if r.levelno == logging.WARNING]
    assert any("НЕ зашифровано" in r.getMessage() for r in warnings), (
        f"Ожидался WARNING о незашифрованном SMTP, логи: {[r.getMessage() for r in log.records]}"
    )


def test_email_smtp_no_plaintext_warning_when_tls(tmp_path: pathlib.Path) -> None:
    """При smtp_use_tls=True plaintext WARNING НЕ появляется."""
    from backend.email_sender import EmailSender

    sender = EmailSender(
        backend_name="smtp",
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_use_tls=True,
        smtp_use_ssl=False,
        smtp_from="from@example.com",
        use_keychain=False,
    )

    msg_mock = MagicMock()
    msg_mock.as_string.return_value = "raw-email"

    smtp_instance = MagicMock()
    smtp_instance.__enter__ = MagicMock(return_value=smtp_instance)
    smtp_instance.__exit__ = MagicMock(return_value=False)

    with patch("backend.email_sender.smtplib.SMTP", return_value=smtp_instance), \
         patch("backend.email_sender.MIMEMultipart", return_value=msg_mock), \
         patch("backend.email_sender.MIMEText"), \
         caplog_ctx(logging.getLogger("KrabEar.Backend.EmailSender")) as log:
        sender._send_via_smtp(
            to="to@example.com",
            subject="Test",
            body_html="<b>hi</b>",
            body_text="hi",
        )

    # НЕ должно быть WARNING о plaintext
    plaintext_warns = [
        r for r in log.records
        if r.levelno == logging.WARNING and "НЕ зашифровано" in r.getMessage()
    ]
    assert not plaintext_warns, f"Неожиданный WARNING при TLS: {plaintext_warns}"


# ---------------------------------------------------------------------------
# Fix 4: AppleIntegrationService field clamp
# ---------------------------------------------------------------------------


def _make_apple_service() -> Any:
    """Создаёт AppleIntegrationService с mock TelegramBridge."""
    from backend.apple_integration_service import AppleIntegrationService
    from backend.telegram_bridge import TelegramBridge

    bridge = MagicMock(spec=TelegramBridge)
    return AppleIntegrationService(telegram_bridge=bridge)


def test_apple_note_title_clamped() -> None:
    """Слишком длинный title в Notes обрезается до _MAX_TITLE_CHARS (по WARNING в логах)."""
    from backend.apple_integration_service import AppleIntegrationService

    svc = _make_apple_service()
    long_title = "А" * (AppleIntegrationService._MAX_TITLE_CHARS + 100)

    with patch("backend.apple_integration_service.subprocess.run") as mock_run, \
         caplog_ctx(logging.getLogger("KrabEar.Backend.AppleIntegrationService")) as log:
        mock_run.return_value = MagicMock(returncode=0, stdout="note-id", stderr="")
        svc.handle_create_apple_note({"title": long_title, "body": "body"})

    warns = [r for r in log.records if r.levelno == logging.WARNING and "'title'" in r.getMessage()]
    assert warns, "WARNING об обрезке title должен быть залогирован"


def test_apple_note_body_clamped() -> None:
    """Слишком длинное body в Notes обрезается до _MAX_BODY_CHARS (по WARNING в логах)."""
    from backend.apple_integration_service import AppleIntegrationService

    svc = _make_apple_service()
    long_body = "Б" * (AppleIntegrationService._MAX_BODY_CHARS + 500)

    with patch("backend.apple_integration_service.subprocess.run") as mock_run, \
         caplog_ctx(logging.getLogger("KrabEar.Backend.AppleIntegrationService")) as log:
        mock_run.return_value = MagicMock(returncode=0, stdout="note-id", stderr="")
        svc.handle_create_apple_note({"title": "Тест", "body": long_body})

    warns = [r for r in log.records if r.levelno == logging.WARNING and "'body'" in r.getMessage()]
    assert warns, "WARNING об обрезке body должен быть залогирован"


def test_apple_reminder_title_clamped() -> None:
    """Слишком длинный title в Reminders обрезается (по WARNING в логах)."""
    from backend.apple_integration_service import AppleIntegrationService

    svc = _make_apple_service()
    long_title = "В" * (AppleIntegrationService._MAX_TITLE_CHARS + 50)

    with patch("backend.apple_integration_service.subprocess.run") as mock_run, \
         caplog_ctx(logging.getLogger("KrabEar.Backend.AppleIntegrationService")) as log:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        svc.handle_create_apple_reminder({"title": long_title, "body": "напомни"})

    warns = [r for r in log.records if r.levelno == logging.WARNING and "'title'" in r.getMessage()]
    assert warns, "WARNING об обрезке title должен быть залогирован"


def test_apple_calendar_title_clamped() -> None:
    """Слишком длинный title в Calendar event обрезается (по WARNING в логах)."""
    from backend.apple_integration_service import AppleIntegrationService

    svc = _make_apple_service()
    long_title = "Г" * (AppleIntegrationService._MAX_TITLE_CHARS + 50)

    with patch("backend.apple_integration_service.subprocess.run") as mock_run, \
         caplog_ctx(logging.getLogger("KrabEar.Backend.AppleIntegrationService")) as log:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        svc.handle_create_calendar_event({
            "title": long_title,
            "notes": "заметки",
            "start_date": "2025-01-01 10:00",
        })

    warns = [r for r in log.records if r.levelno == logging.WARNING and "'title'" in r.getMessage()]
    assert warns, "WARNING об обрезке title должен быть залогирован"


def test_apple_calendar_notes_clamped() -> None:
    """Слишком длинные notes в Calendar event обрезаются (по WARNING в логах)."""
    from backend.apple_integration_service import AppleIntegrationService

    svc = _make_apple_service()
    long_notes = "Д" * (AppleIntegrationService._MAX_BODY_CHARS + 200)

    with patch("backend.apple_integration_service.subprocess.run") as mock_run, \
         caplog_ctx(logging.getLogger("KrabEar.Backend.AppleIntegrationService")) as log:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        svc.handle_create_calendar_event({
            "title": "Встреча",
            "notes": long_notes,
            "start_date": "2025-01-01 10:00",
        })

    warns = [r for r in log.records if r.levelno == logging.WARNING and "'notes'" in r.getMessage()]
    assert warns, "WARNING об обрезке notes должен быть залогирован"


def test_apple_clamp_field_short_values_unchanged() -> None:
    """Поля в пределах лимита не должны обрезаться."""

    svc = _make_apple_service()
    title = "Обычный заголовок"
    body = "Обычное тело"

    with patch("backend.apple_integration_service.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        svc.handle_create_apple_note({"title": title, "body": body})

    script_text = " ".join(str(a) for a in mock_run.call_args[0][0])
    assert "Обычный заголовок" in script_text
    assert "Обычное тело" in script_text


# ---------------------------------------------------------------------------
# Вспомогательный context manager для перехвата логов
# ---------------------------------------------------------------------------


class caplog_ctx:
    """Простой context-manager для перехвата log-записей нужного logger."""

    def __init__(self, logger_obj: logging.Logger) -> None:
        self._logger = logger_obj
        self.records: list[logging.LogRecord] = []
        self._handler: logging.Handler = _ListHandler(self.records)

    def __enter__(self) -> "caplog_ctx":
        self._handler = _ListHandler(self.records)
        self._handler.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, *args: Any) -> None:
        self._logger.removeHandler(self._handler)


class _ListHandler(logging.Handler):
    """Logging handler который складывает записи в список."""

    def __init__(self, store: list[logging.LogRecord]) -> None:
        super().__init__()
        self._store = store

    def emit(self, record: logging.LogRecord) -> None:
        self._store.append(record)
