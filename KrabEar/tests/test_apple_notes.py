# -*- coding: utf-8 -*-
"""Тесты Apple Notes IPC handler — create_apple_note.

Покрываем:
  - успешный вызов osascript (returncode=0) возвращает ok=True
  - экранирование кавычек в title/body (no AppleScript injection)
  - folder param: оборачивает вызов в tell folder ... block
  - subprocess.TimeoutExpired → ok=False, error="osascript timeout"
  - ненулевой returncode → ok=False, error=stderr
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


def _call_create_apple_note(service: BackendService, params: dict) -> dict:
    """Вызывает handler напрямую через handle_request."""
    payload = {"id": "test-1", "method": "create_apple_note", "params": params}
    response = service.handle_request(payload)
    return response.get("result", {})


def _make_completed_process(returncode: int = 0, stdout: str = "note-id-123", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestCreateAppleNote(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()

    def tearDown(self):
        self.service.close()

    # ------------------------------------------------------------------
    # test_create_note_calls_osascript
    # ------------------------------------------------------------------
    def test_create_note_calls_osascript(self):
        """osascript вызывается; при returncode=0 возвращает ok=True и note_id."""
        proc = _make_completed_process(returncode=0, stdout="note-id-abc")
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_note(
                self.service,
                {"title": "Hello", "body": "World"},
            )

        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("note_id"), "note-id-abc")
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
    # test_create_note_escapes_quotes
    # ------------------------------------------------------------------
    def test_create_note_escapes_quotes(self):
        """Двойные кавычки в title/body экранируются — AppleScript не ломается."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_note(
                self.service,
                {"title": 'Say "hello"', "body": 'He said "bye"'},
            )

        self.assertTrue(result.get("ok"))
        call_args = mock_run.call_args
        script = call_args[0][0][2]
        # Кавычки должны быть экранированы обратным слешем
        self.assertIn('\\"hello\\"', script)
        self.assertIn('\\"bye\\"', script)
        # Сырые неэкранированные кавычки (кроме обрамляющих) не должны присутствовать
        # Проверяем что name:"Say \"hello\"" встречается
        self.assertIn('name:"Say \\"hello\\""', script)

    # ------------------------------------------------------------------
    # test_create_note_with_folder
    # ------------------------------------------------------------------
    def test_create_note_with_folder(self):
        """Параметр folder оборачивает вызов в tell folder ... end tell."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_note(
                self.service,
                {"title": "Title", "body": "Body", "folder": "Krab Ear"},
            )

        self.assertTrue(result.get("ok"))
        script = mock_run.call_args[0][0][2]
        self.assertIn('folder "Krab Ear"', script)
        self.assertIn("targetFolder", script)
        self.assertIn("tell account", script)

    # ------------------------------------------------------------------
    # test_create_note_handles_timeout
    # ------------------------------------------------------------------
    def test_create_note_handles_timeout(self):
        """TimeoutExpired → ok=False, error='osascript timeout'."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=10)):
            result = _call_create_apple_note(
                self.service,
                {"title": "Title", "body": "Body"},
            )

        self.assertFalse(result.get("ok"))
        self.assertIsNone(result.get("note_id"))
        self.assertEqual(result.get("error"), "osascript timeout")

    # ------------------------------------------------------------------
    # test_create_note_non_zero_returncode_returns_error
    # ------------------------------------------------------------------
    def test_create_note_non_zero_returncode_returns_error(self):
        """Ненулевой returncode → ok=False, error=stderr."""
        proc = _make_completed_process(returncode=1, stdout="", stderr="Notes не открылись")
        with patch("subprocess.run", return_value=proc):
            result = _call_create_apple_note(
                self.service,
                {"title": "Title", "body": "Body"},
            )

        self.assertFalse(result.get("ok"))
        self.assertIsNone(result.get("note_id"))
        self.assertEqual(result.get("error"), "Notes не открылись")


    def test_apple_note_backslash_in_title_safe(self):  # W1052 regression
        """Backslash in title must be doubled before quote-escaping.

        Naïve .replace('"', '\\"') skips backslash escaping, so a title like
        'path\\note' would become 'path\\note' unchanged — which in AppleScript
        means the backslash escapes the next char. With _escape_as_str() the
        backslash itself is first doubled to '\\\\', making the resulting script
        literal safe.
        """
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_create_apple_note(
                self.service,
                {"title": 'path\\note', "body": 'line1\\nline2'},
            )

        self.assertTrue(result.get("ok"), result.get("error"))
        script = mock_run.call_args[0][0][2]
        # Backslash must be doubled in the emitted AppleScript
        self.assertIn("path\\\\note", script)
        self.assertIn("line1\\\\nline2", script)


if __name__ == "__main__":
    unittest.main()
