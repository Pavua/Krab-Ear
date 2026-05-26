# -*- coding: utf-8 -*-
"""Security regression tests: AppleScript newline injection (W942 HIGH-1).

Attack vector: a newline (\\n, \\r) or NUL (\\x00) in user-supplied params to any
osascript-using handler breaks out of the AppleScript double-quoted string literal,
allowing arbitrary AppleScript commands to execute.

Example payload (calendar event title):
    x"\\nsay "PWNED"\\nset y to "

These tests verify that:
  1. No literal newlines appear in the generated osascript script after sanitisation.
  2. The helper _escape_as_str strips \\r, \\n, \\x00 and escapes backslash+quote.
  3. All four vulnerable handlers are covered:
       - _handle_create_calendar_event
       - _handle_create_apple_note
       - _handle_create_apple_reminder
       - _handle_send_imessage
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

from backend.service import BackendService, _escape_as_str
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service() -> BackendService:
    """Minimal BackendService via __new__ + stub collaborators (avoids full init)."""
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    store = StateStore(data_dir=tmp)

    service = BackendService.__new__(BackendService)
    service.store = store
    service.transcriber = MagicMock()
    service.recorder = MagicMock()
    service.translator = MagicMock()
    service.llm_rewriter = MagicMock()
    service.metrics = MagicMock()
    service.event_bus = MagicMock()
    service._call_assist = MagicMock()
    service._history_svc = MagicMock()
    service._translation_svc = MagicMock()
    service._settings_svc = MagicMock()
    service._settings_svc.get_settings.return_value = {}
    return service


def _ok_proc() -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""
    return proc


# ---------------------------------------------------------------------------
# Unit tests for _escape_as_str helper
# ---------------------------------------------------------------------------

class TestEscapeAsStrHelper(unittest.TestCase):
    """Unit tests for the module-level _escape_as_str sanitisation helper."""

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
        self.assertNotIn('"hello"', result)  # raw unescaped must be gone

    def test_escapes_backslash_before_quote(self):
        # Backslash must be doubled before the quote is escaped,
        # otherwise \\" would become \\\\" (over-escaped).
        result = _escape_as_str('path\\file"x"')
        # backslash → \\\\ and quote → \\"
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
        # A string with no special chars must pass through unchanged
        self.assertEqual(_escape_as_str("Meeting at 9am"), "Meeting at 9am")


# ---------------------------------------------------------------------------
# Integration tests: calendar event handler
# ---------------------------------------------------------------------------

INJECTION_PAYLOADS = [
    # Classic newline break-out
    'x"\nsay "PWNED"\nset y to "',
    # CR break-out
    'x"\rsay "PWNED"\rset y to "',
    # NUL injection
    'x"\x00say "PWNED"\x00set y to "',
    # Combined
    "title\nwith\nnewlines",
]


class TestCalendarEventInjection(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()



    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        """Newline injection in title must be sanitised — no executable say statement."""
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "x\"\nsay \"PWNED\"\nset y to \"",
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        # The injection payload must NOT produce a bare say "PWNED" AppleScript command.
        # After sanitisation newlines become spaces, so the payload becomes a
        # harmless escaped string value, not an executable statement.
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_notes_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "Legit title",
            "notes": "Notes\nsay \"PWNED\"",
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_start_date_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "T",
            "start_date": "05/26/2026 10:00:00\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_calendar_name_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "T",
            "start_date": "05/26/2026 10:00:00",
            "calendar_name": "Work\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "x\"\rsay \"PWNED\"\rset y to \"",
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_nul_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_calendar_event({
            "title": "x\"\x00say \"PWNED\"\x00set y to \"",
            "start_date": "05/26/2026 10:00:00",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn("\x00", script)


# ---------------------------------------------------------------------------
# Integration tests: apple note handler
# ---------------------------------------------------------------------------

class TestAppleNoteInjection(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()



    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_note({
            "title": "x\"\nsay \"PWNED\"\nset y to \"",
            "body": "normal body",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_note({
            "title": "Normal",
            "body": "body\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_folder_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_note({
            "title": "T",
            "body": "B",
            "folder": "MyFolder\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)


# ---------------------------------------------------------------------------
# Integration tests: apple reminder handler
# ---------------------------------------------------------------------------

class TestAppleReminderInjection(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()



    @patch("subprocess.run")
    def test_newline_in_title_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_reminder({
            "title": "x\"\nsay \"PWNED\"\nset y to \"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_reminder({
            "title": "Normal",
            "body": "body\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_due_date_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_reminder({
            "title": "T",
            "due_date": "05/26/2026\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_list_name_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_create_apple_reminder({
            "title": "T",
            "list_name": "Reminders\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)


# ---------------------------------------------------------------------------
# Integration tests: send iMessage handler
# ---------------------------------------------------------------------------

class TestSendIMessageInjection(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()



    @patch("subprocess.run")
    def test_newline_in_recipient_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_send_imessage({
            "recipient": "+1234567890\nsay \"PWNED\"",
            "body": "hello",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_newline_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_send_imessage({
            "recipient": "+1234567890",
            "body": "hi\nsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_cr_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_send_imessage({
            "recipient": "+1234567890",
            "body": "hi\rsay \"PWNED\"",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn('say "PWNED"', script)

    @patch("subprocess.run")
    def test_nul_in_body_not_in_script(self, mock_run):
        mock_run.return_value = _ok_proc()
        self.service._handle_send_imessage({
            "recipient": "+1234567890",
            "body": "hi\x00there",
        })
        script = mock_run.call_args[0][0][2]
        self.assertNotIn("\x00", script)


if __name__ == "__main__":
    unittest.main()
