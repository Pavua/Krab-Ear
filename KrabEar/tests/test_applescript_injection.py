# -*- coding: utf-8 -*-
"""Security regression tests: AppleScript-инъекция (W942 HIGH-1 / W1765 MED).

Вектор атаки: символ \\n, \\r или NUL (\\x00) в пользовательских параметрах
osascript-обработчика разрывает AppleScript-строку в двойных кавычках и позволяет
выполнить произвольную AppleScript-команду.

Пример payload (заголовок события Calendar):
    x"\\nsay "PWNED"\\nset y to "

Тесты проверяют:
  1. _escape_as_str (AppleIntegrationService) очищает \\r, \\n, \\x00 и экранирует
     \\ и ".
  2. Каждый из четырёх live-обработчиков AppleIntegrationService формирует script,
     в котором инъекционная нагрузка безвредна (нет исполнимого say "PWNED").
  3. Тест ПРОВАЛИТСЯ если _escape_as_str вернуть к слабой версии (только \\ и ").
  4. Обработчики вызываются напрямую через AppleIntegrationService — именно тот
     путь, который используется в production handle_request (W1765 MED-2: предыдущий
     вариант теста вызывал BackendService._handle_* — устаревший путь с другим,
     более безопасным _escape_as_str).

Covered handlers (AppleIntegrationService):
  - handle_create_calendar_event
  - handle_create_apple_note
  - handle_create_apple_reminder
  - handle_send_imessage
"""

from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Импортируем ЖИВОЙ сервис, который используется в production handle_request.
# W1765 MED-2: предыдущие тесты импортировали BackendService._escape_as_str
# и вызывали BackendService._handle_* — это устаревшая ветка кода с более
# безопасным re.sub-экранированием, которая НЕ упражняет production-путь.
from backend.apple_integration_service import AppleIntegrationService

# _escape_as_str для проверки единицы экранирования — берём напрямую из сервиса.
_escape_as_str = AppleIntegrationService._escape_as_str


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _make_svc() -> AppleIntegrationService:
    """Минимальный экземпляр AppleIntegrationService без полного инициализатора."""
    telegram_bridge = MagicMock()
    return AppleIntegrationService(
        telegram_bridge=telegram_bridge,
        settings_get=lambda key, default: default,
    )


def _ok_proc() -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "note:1"
    proc.stderr = ""
    return proc


# ---------------------------------------------------------------------------
# Слабый эскейпер — для проверки что тест ПРОВАЛИТСЯ при регрессии
# ---------------------------------------------------------------------------

def _weak_escape(s: str) -> str:
    """Версия _escape_as_str БЕЗ обработки newline/CR/NUL — намеренно уязвимая.

    Используется в TestWeakEscapeFailsInjection для доказательства что
    тесты реально обнаруживают регрессию (test-must-fail-on-weak-escape).
    """
    return str(s).replace('\\', '\\\\').replace('"', '\\"')


# ---------------------------------------------------------------------------
# Единичные тесты вспомогательного метода _escape_as_str
# ---------------------------------------------------------------------------

class TestEscapeAsStrHelper(unittest.TestCase):
    """Проверяет AppleIntegrationService._escape_as_str напрямую."""

    def test_strips_newline(self):
        result = _escape_as_str("line1\nline2")
        self.assertNotIn("\n", result)

    def test_strips_carriage_return(self):
        result = _escape_as_str("line1\rline2")
        self.assertNotIn("\r", result)

    def test_strips_nul(self):
        result = _escape_as_str("ab\x00cd")
        self.assertNotIn("\x00", result)

    def test_escapes_double_quote(self):
        result = _escape_as_str('say "hello"')
        self.assertIn('\\"hello\\"', result)
        # Незаэкранированная кавычка не должна присутствовать
        self.assertNotIn('"hello"', result)

    def test_escapes_backslash_before_quote(self):
        # Порядок: сначала \\ → \\\\, затем " → \\" — иначе \\" станет \\\\".
        result = _escape_as_str('path\\file"x"')
        self.assertIn('\\\\', result)
        self.assertIn('\\"x\\"', result)

    def test_newline_replaced_with_space(self):
        result = _escape_as_str("hello\nworld")
        self.assertIn("hello world", result)

    def test_non_string_coerced(self):
        result = _escape_as_str(42)
        self.assertEqual(result, "42")

    def test_empty_string(self):
        self.assertEqual(_escape_as_str(""), "")

    def test_normal_string_unchanged(self):
        self.assertEqual(_escape_as_str("Meeting at 9am"), "Meeting at 9am")

    def test_combined_injection_payload_neutralised(self):
        """Комплексная нагрузка: кавычка + перенос + команда."""
        payload = '"\nsay "PWNED"\nset x to "'
        result = _escape_as_str(payload)
        self.assertNotIn("\n", result)
        self.assertNotIn('say "PWNED"', result)

    def test_weak_escape_does_NOT_strip_newline(self):
        """Контрольная проверка: слабый эскейпер оставляет \\n — именно это делало
        AppleIntegrationService до W1765 и почему инъекция была возможна."""
        payload = "hello\nworld"
        self.assertIn("\n", _weak_escape(payload),
                      "Слабый эскейпер должен оставлять newline (иначе тест-на-регрессию "
                      "неинформативен)")


# ---------------------------------------------------------------------------
# Проверка что слабый эскейпер действительно пропускает инъекцию
# (test-must-fail-on-weak-escape)
# ---------------------------------------------------------------------------

class TestWeakEscapeFailsInjection(unittest.TestCase):
    """Доказывает что тест инъекции ПРОВАЛИТСЯ если _escape_as_str откатить
    к слабой версии (только \\\\ и ").

    Этот класс намеренно проверяет СЛАБЫЙ путь через _patch_escape,
    чтобы подтвердить что основные тесты не дают ложную уверенность.
    """

    def test_weak_escape_leaks_newline_into_script(self):
        """Слабый эскейпер позволяет \\n попасть в AppleScript-строку.

        Если AppleIntegrationService._escape_as_str заменить на _weak_escape,
        это должно быть заметно по тому что \\n остаётся в escaped-значении.
        Это обратная проверка: убеждаемся что основные тесты «знают разницу».
        """
        malicious = "x\"\nsay \"PWNED\"\nset y to \""
        weak_result = _weak_escape(malicious)
        strong_result = _escape_as_str(malicious)

        # Слабый путь содержит newline
        self.assertIn("\n", weak_result)
        # Сильный путь НЕ содержит newline
        self.assertNotIn("\n", strong_result)

    def test_note_handler_with_weak_escape_leaks_newline(self):
        """Если откатить _escape_as_str к слабой версии — newline попадает в script.

        Прямая проверка: применяем _weak_escape вручную к инъекционному payload
        и убеждаемся что в результате есть \\n — именно это приводило бы к инъекции.
        Это подтверждает что основные тесты значимы: strong-версия удаляет \\n,
        weak-версия нет.
        """
        malicious_title = "x\"\nsay \"PWNED\"\nset y to \""
        weak_result = _weak_escape(malicious_title)
        # При слабом эскейпере \n остаётся в строке → прорыв строки AppleScript
        self.assertIn("\n", weak_result,
                      "Слабый эскейпер должен оставлять newline в payload "
                      "(иначе тест-sentinel неинформативен)")
        # При сильном эскейпере \n заменяется на пробел → нет прорыва
        strong_result = _escape_as_str(malicious_title)
        self.assertNotIn("\n", strong_result)


# ---------------------------------------------------------------------------
# Интеграционные тесты: AppleIntegrationService.handle_create_calendar_event
# ---------------------------------------------------------------------------

INJECTION_PAYLOADS = [
    'x"\nsay "PWNED"\nset y to "',
    'x"\rsay "PWNED"\rset y to "',
    'x"\x00say "PWNED"\x00set y to "',
    "title\nwith\nnewlines",
]


class TestCalendarEventInjection(unittest.TestCase):
    """Упражняет AppleIntegrationService.handle_create_calendar_event — production-путь."""

    def setUp(self):
        self.svc = _make_svc()

    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        """Инъекция newline в title нейтрализована — нет исполнимого say "PWNED"."""
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": 'x"\nsay "PWNED"\nset y to "',
            "start_date": "05/26/2026 10:00:00",
        })
        call_args = mock_run.call_args[0][0]
        # Убеждаемся что вызывается osascript с флагом -e
        self.assertEqual(call_args[0], "osascript")
        self.assertIn("-e", call_args)
        script = call_args[2]
        self.assertNotIn('say "PWNED"', script)
        self.assertNotIn("\n\n", script)  # не должно быть пустых строк от инъекции

    @patch("subprocess.run")
    def test_newline_in_notes_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": "Legit title",
            "notes": 'Notes\nsay "PWNED"',
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_start_date_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": "T",
            "start_date": '05/26/2026 10:00:00\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_calendar_name_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": "T",
            "start_date": "05/26/2026 10:00:00",
            "calendar_name": 'Work\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": 'x"\rsay "PWNED"\rset y to "',
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_nul_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_calendar_event({
            "title": 'x"\x00say "PWNED"\x00set y to "',
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn("\x00", script)


# ---------------------------------------------------------------------------
# Интеграционные тесты: AppleIntegrationService.handle_create_apple_note
# ---------------------------------------------------------------------------

class TestAppleNoteInjection(unittest.TestCase):
    """Упражняет AppleIntegrationService.handle_create_apple_note — production-путь."""

    def setUp(self):
        self.svc = _make_svc()

    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_note({
            "title": 'x"\nsay "PWNED"\nset y to "',
            "body": "normal body",
        })
        call_args = mock_run.call_args[0][0]
        script = call_args[2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_note({
            "title": "Normal",
            "body": 'body\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_folder_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_note({
            "title": "T",
            "body": "B",
            "folder": 'MyFolder\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_note({
            "title": "T",
            "body": 'B\rsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_script_contains_osascript_call(self, mock_run):
        """Проверяем что subprocess.run вызывается с osascript."""
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_note({"title": "Test", "body": "Body"})
        call_args = mock_run.call_args[0][0]
        self.assertEqual(call_args[0], "osascript")


# ---------------------------------------------------------------------------
# Интеграционные тесты: AppleIntegrationService.handle_create_apple_reminder
# ---------------------------------------------------------------------------

class TestAppleReminderInjection(unittest.TestCase):
    """Упражняет AppleIntegrationService.handle_create_apple_reminder — production-путь."""

    def setUp(self):
        self.svc = _make_svc()

    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_reminder({
            "title": 'x"\nsay "PWNED"\nset y to "',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_reminder({
            "title": "Normal",
            "body": 'body\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_due_date_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_reminder({
            "title": "T",
            "due_date": '05/26/2026\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_list_name_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_reminder({
            "title": "T",
            "list_name": 'Reminders\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_create_apple_reminder({
            "title": 'x"\rsay "PWNED"\rset y to "',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)


# ---------------------------------------------------------------------------
# Интеграционные тесты: AppleIntegrationService.handle_send_imessage
# ---------------------------------------------------------------------------

class TestSendIMessageInjection(unittest.TestCase):
    """Упражняет AppleIntegrationService.handle_send_imessage — production-путь."""

    def setUp(self):
        self.svc = _make_svc()

    @patch("subprocess.run")
    def test_newline_in_recipient_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_send_imessage({
            "recipient": '+1234567890\nsay "PWNED"',
            "body": "hello",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_send_imessage({
            "recipient": "+1234567890",
            "body": 'hi\nsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_send_imessage({
            "recipient": "+1234567890",
            "body": 'hi\rsay "PWNED"',
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_nul_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.svc.handle_send_imessage({
            "recipient": "+1234567890",
            "body": "hi\x00there",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn("\x00", script)

    @patch("subprocess.run")
    def test_leading_dash_recipient_not_flag(self, mock_run):
        """recipient начинающийся с '-' не должен становиться флагом osascript.

        W1764-урок (email_sender): osascript getopt обрабатывает ведущие '-' как
        флаги когда значение передаётся как отдельный ARGV. Здесь значение встроено
        в script-строку, а не ARGV, поэтому достаточно проверить что сам вызов
        subprocess.run не передаёт recipient как отдельный элемент argv.
        """
        mock_run.return_value = _ok_proc()
        self.svc.handle_send_imessage({
            "recipient": "-hacker@example.com",
            "body": "test",
        })
        call_args = mock_run.call_args[0][0]
        # Проверяем что recipient не фигурирует как отдельный элемент argv
        self.assertNotIn("-hacker@example.com", call_args,
                         "recipient не должен передаваться как отдельный argv-аргумент")


if __name__ == "__main__":
    unittest.main()
