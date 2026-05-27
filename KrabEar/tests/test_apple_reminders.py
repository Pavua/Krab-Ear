# -*- coding: utf-8 -*-
"""Тесты Apple Reminders IPC handler — create_apple_reminder.

Покрываем:
  - успешный вызов osascript (returncode=0) возвращает ok=True
  - экранирование кавычек в title/body (no AppleScript injection)
  - list_name param: генерирует tell list ... block
  - due_date param: добавляет due date: clause
  - subprocess.TimeoutExpired → ok=False, error="osascript timeout"
"""

from __future__ import annotations

import sys
import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService
from backend.state_store import StateStore


def _make_service() -> BackendService:
    """Минимальный BackendService для тестирования handle_request."""
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp / "data")
    service = BackendService(store=store)
    return service


def _call_create_apple_reminder(service: BackendService, params: dict) -> dict:
    """Вызывает handler напрямую через handle_request."""
    payload = {"id": "test-1", "method": "create_apple_reminder", "params": params}
    response = service.handle_request(payload)
    return response.get("result", {})


def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestCreateAppleReminder(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()

    def tearDown(self):
        self.service.close()

    # ------------------------------------------------------------------
    # test_create_reminder_calls_osascript
    # ------------------------------------------------------------------
    def test_create_reminder_calls_osascript(self):
        """osascript вызывается; при returncode=0 возвращает ok=True."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_reminder(
                self.service,
                {"title": "Hello", "body": "World"},
            )

        self.assertTrue(result.get("ok"))
        self.assertIsNone(result.get("error"))

        # Убеждаемся что был вызван именно osascript
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        # Скрипт содержит переданные title и body
        script = cmd[2]
        self.assertIn("Hello", script)
        self.assertIn("World", script)

    # ------------------------------------------------------------------
    # test_create_reminder_escapes_quotes
    # ------------------------------------------------------------------
    def test_create_reminder_escapes_quotes(self):
        """Двойные кавычки в title/body экранируются — AppleScript не ломается."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_reminder(
                self.service,
                {"title": 'Say "hello"', "body": 'He said "bye"'},
            )

        self.assertTrue(result.get("ok"))
        call_args = mock_run.call_args
        script = call_args[0][0][2]
        # Кавычки должны быть экранированы обратным слешем
        self.assertIn('\\"hello\\"', script)
        self.assertIn('\\"bye\\"', script)
        # Проверяем что name:"Say \"hello\"" встречается
        self.assertIn('name:"Say \\"hello\\""', script)

    # ------------------------------------------------------------------
    # test_create_reminder_with_list_name
    # ------------------------------------------------------------------
    def test_create_reminder_with_list_name(self):
        """Параметр list_name оборачивает вызов в tell list ... end tell."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_reminder(
                self.service,
                {"title": "Title", "body": "Body", "list_name": "Krab Ear"},
            )

        self.assertTrue(result.get("ok"))
        script = mock_run.call_args[0][0][2]
        self.assertIn('list "Krab Ear"', script)
        self.assertIn("tell list", script)

    # ------------------------------------------------------------------
    # test_create_reminder_with_due_date
    # ------------------------------------------------------------------
    def test_create_reminder_with_due_date(self):
        """Параметр due_date добавляет due date: clause в properties."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_reminder(
                self.service,
                {"title": "Title", "body": "Body", "due_date": "2026-06-01T10:00:00"},
            )

        self.assertTrue(result.get("ok"))
        script = mock_run.call_args[0][0][2]
        self.assertIn("due date", script)
        self.assertIn("2026-06-01T10:00:00", script)

    # ------------------------------------------------------------------
    # test_create_reminder_handles_timeout
    # ------------------------------------------------------------------
    def test_create_reminder_handles_timeout(self):
        """TimeoutExpired → ok=False, error='osascript timeout'."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=10)):
            result = _call_create_apple_reminder(
                self.service,
                {"title": "Title", "body": "Body"},
            )

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "osascript timeout")


    # ------------------------------------------------------------------
    # test_apple_reminder_backslash_in_title_safe  (W1052 regression)
    # ------------------------------------------------------------------
    def test_apple_reminder_backslash_in_title_safe(self):
        """Backslash in title must be doubled; naïve replace misses this.

        Without _escape_as_str(), a title like 'C:\\path' stays as 'C:\\path' in
        the script — the backslash then escapes the next char in AppleScript.
        _escape_as_str() first doubles backslashes so the script literal is safe.
        """
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_reminder(
                self.service,
                {"title": 'C:\\path', "body": 'line\\nbreak'},
            )

        self.assertTrue(result.get("ok"), result.get("error"))
        script = mock_run.call_args[0][0][2]
        self.assertIn("C:\\\\path", script)
        self.assertIn("line\\\\nbreak", script)


if __name__ == "__main__":
    unittest.main()
