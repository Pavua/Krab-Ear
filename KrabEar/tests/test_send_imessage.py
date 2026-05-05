# -*- coding: utf-8 -*-
"""Тесты iMessage IPC handler — send_imessage.

Покрываем:
  - успешный вызов osascript (returncode=0) → ok=True
  - экранирование кавычек в recipient и body
  - параметр service="SMS" генерирует SMS service type
  - subprocess.TimeoutExpired → ok=False, error="osascript timeout"
  - returncode != 0 → ok=False с stderr в error
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


def _call_send_imessage(service: BackendService, params: dict) -> dict:
    """Вызывает handler напрямую через handle_request."""
    payload = {"id": "test-1", "method": "send_imessage", "params": params}
    response = service.handle_request(payload)
    return response.get("result", {})


def _make_completed_process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestSendImessage(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()

    def tearDown(self):
        self.service.close()

    # ------------------------------------------------------------------
    # test_send_imessage_calls_osascript
    # ------------------------------------------------------------------
    def test_send_imessage_calls_osascript(self):
        """osascript вызывается; при returncode=0 возвращает ok=True."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_send_imessage(
                self.service,
                {"recipient": "+79001234567", "body": "Привет!"},
            )

        self.assertTrue(result.get("ok"))
        self.assertIsNone(result.get("error"))

        mock_run.assert_called_once()
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")

        script = cmd[2]
        self.assertIn("+79001234567", script)
        self.assertIn("Привет!", script)
        self.assertIn("Messages", script)
        # Default service is iMessage
        self.assertIn("iMessage", script)

    # ------------------------------------------------------------------
    # test_send_imessage_escapes_quotes
    # ------------------------------------------------------------------
    def test_send_imessage_escapes_quotes(self):
        """Двойные кавычки в recipient и body экранируются — AppleScript не ломается."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_send_imessage(
                self.service,
                {"recipient": 'John "JJ" Doe', "body": 'He said "hello"'},
            )

        self.assertTrue(result.get("ok"))
        script = mock_run.call_args[0][0][2]
        self.assertIn('\\"JJ\\"', script)
        self.assertIn('\\"hello\\"', script)
        # Verify the raw un-escaped quote is not present (would break AppleScript)
        self.assertNotIn('buddy "John "JJ"', script)

    # ------------------------------------------------------------------
    # test_send_imessage_uses_sms_service
    # ------------------------------------------------------------------
    def test_send_imessage_uses_sms_service(self):
        """Параметр service='SMS' генерирует service type = SMS в скрипте."""
        proc = _make_completed_process(returncode=0)
        with patch("subprocess.run", return_value=proc) as mock_run:
            result = _call_send_imessage(
                self.service,
                {"recipient": "+79001234567", "body": "SMS test", "service": "SMS"},
            )

        self.assertTrue(result.get("ok"))
        script = mock_run.call_args[0][0][2]
        self.assertIn("SMS", script)

    # ------------------------------------------------------------------
    # test_send_imessage_handles_timeout
    # ------------------------------------------------------------------
    def test_send_imessage_handles_timeout(self):
        """TimeoutExpired → ok=False, error='osascript timeout'."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=10)):
            result = _call_send_imessage(
                self.service,
                {"recipient": "+79001234567", "body": "Hello"},
            )

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "osascript timeout")

    # ------------------------------------------------------------------
    # test_send_imessage_handles_error_returncode
    # ------------------------------------------------------------------
    def test_send_imessage_handles_error_returncode(self):
        """returncode != 0 → ok=False и stderr передаётся в error."""
        proc = _make_completed_process(returncode=1, stderr="Messages not running")
        with patch("subprocess.run", return_value=proc):
            result = _call_send_imessage(
                self.service,
                {"recipient": "+79001234567", "body": "Hello"},
            )

        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error"), "Messages not running")


if __name__ == "__main__":
    unittest.main()
