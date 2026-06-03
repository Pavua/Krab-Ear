"""Tests for create_calendar_event (Phase D.4 — Apple Calendar integration).

W797 follow-up (#47): the in-class BackendService._handle_create_calendar_event
duplicate was deleted — production dispatch routes straight to
self._apple_integration_svc.handle_create_calendar_event. These tests now invoke
the LIVE extracted AppleIntegrationService handler. The _escape_as_str static
helper is retained on BackendService and is still exercised directly below.
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure backend package is importable when run directly.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService
from backend.apple_integration_service import AppleIntegrationService


def _make_service():
    """Return the LIVE AppleIntegrationService that production dispatch routes to."""
    return AppleIntegrationService(telegram_bridge=MagicMock())


class TestCreateCalendarEvent(unittest.TestCase):

    def setUp(self):
        self.service = _make_service()

    # ------------------------------------------------------------------
    # test_create_event_calls_osascript
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_calls_osascript(self, mock_run):
        """Handler must call osascript and return ok=True on success."""
        mock_run.return_value = MagicMock(returncode=0, stdout="event id 42\n", stderr="")

        result = self.service.handle_create_calendar_event({
            "title": "Meeting",
            "start_date": "05/05/2026 10:00:00",
        })

        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        # First positional arg is the command list
        cmd = args[0]
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[1], "-e")
        script = cmd[2]
        self.assertIn("Calendar", script)
        self.assertIn("Meeting", script)
        self.assertIn("05/05/2026 10:00:00", script)

    # ------------------------------------------------------------------
    # test_create_event_escapes_quotes
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_escapes_quotes(self, mock_run):
        """Double-quotes in title/notes must be escaped so osascript doesn't break."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.service.handle_create_calendar_event({
            "title": 'He said "hello"',
            "notes": 'Notes with "quotes"',
            "start_date": "05/05/2026 14:00:00",
        })

        self.assertTrue(result["ok"])
        _, kwargs = mock_run.call_args
        script = mock_run.call_args[0][0][2]
        # Escaped form should appear in script, raw unescaped should not
        self.assertIn('\\"hello\\"', script)
        self.assertIn('\\"quotes\\"', script)
        # Unbalanced raw double-quote must not be present (after title/notes)
        # We only check that escaped versions are there — that's the contract.

    # ------------------------------------------------------------------
    # test_create_event_with_calendar_name
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_with_calendar_name(self, mock_run):
        """When calendar_name is set, the tell calendar block must use it."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = self.service.handle_create_calendar_event({
            "title": "Sprint Review",
            "start_date": "05/06/2026 09:00:00",
            "calendar_name": "Work",
        })

        self.assertTrue(result["ok"])
        script = mock_run.call_args[0][0][2]
        self.assertIn('tell calendar "Work"', script)

    # ------------------------------------------------------------------
    # test_create_event_default_duration
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_default_duration(self, mock_run):
        """When duration_minutes is omitted, it defaults to 30."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.service.handle_create_calendar_event({
            "title": "Quick call",
            "start_date": "05/07/2026 11:00:00",
        })

        script = mock_run.call_args[0][0][2]
        self.assertIn("30 * minutes", script)

    # ------------------------------------------------------------------
    # test_create_event_handles_timeout
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_handles_timeout(self, mock_run):
        """TimeoutExpired must be caught and returned as ok=False with error message."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["osascript"], timeout=15)

        result = self.service.handle_create_calendar_event({
            "title": "Slow event",
            "start_date": "05/08/2026 12:00:00",
        })

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "osascript timeout")

    # ------------------------------------------------------------------
    # test_create_event_missing_title_returns_error
    # ------------------------------------------------------------------
    def test_create_event_missing_title_returns_error(self):
        """Empty title must return ok=False immediately without calling osascript."""
        result = self.service.handle_create_calendar_event({
            "title": "",
            "start_date": "05/09/2026 08:00:00",
        })
        self.assertFalse(result["ok"])
        self.assertIn("title", result.get("error", ""))

    # ------------------------------------------------------------------
    # test_create_event_osascript_error_propagated
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_osascript_error_propagated(self, mock_run):
        """Non-zero osascript exit must return ok=False with stderr as error."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Calendar got an error: access denied"
        )

        result = self.service.handle_create_calendar_event({
            "title": "Bad event",
            "start_date": "05/10/2026 09:00:00",
        })

        self.assertFalse(result["ok"])
        self.assertIn("access denied", result["error"])

    # ------------------------------------------------------------------
    # test_create_event_no_calendar_name_uses_writable
    # ------------------------------------------------------------------
    @patch("subprocess.run")
    def test_create_event_no_calendar_name_uses_writable(self, mock_run):
        """When calendar_name is None, script must target first writable calendar."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.service.handle_create_calendar_event({
            "title": "Auto calendar",
            "start_date": "05/11/2026 15:00:00",
        })

        script = mock_run.call_args[0][0][2]
        self.assertIn("writable", script)

    # ------------------------------------------------------------------
    # test_handler_registered_in_dispatch_table
    # ------------------------------------------------------------------
    def test_handler_registered_in_dispatch_table(self):
        """create_calendar_event must route to the live extracted handler."""
        # The live handler lives on AppleIntegrationService (production dispatch
        # routes "create_calendar_event" → self._apple_integration_svc.handle_*).
        handler = getattr(self.service, "handle_create_calendar_event", None)
        self.assertIsNotNone(handler, "handle_create_calendar_event method must exist")
        self.assertTrue(callable(handler))

        # Verify the dispatch table in service.py wires the IPC key to the
        # extracted service rather than any deleted in-class duplicate.
        import os
        service_src = os.path.join(PROJECT_ROOT, "backend", "service.py")
        with open(service_src, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn(
            '"create_calendar_event": self._apple_integration_svc.handle_create_calendar_event',
            src,
            "create_calendar_event must dispatch to the extracted AppleIntegrationService",
        )

    # ------------------------------------------------------------------
    # W1028-F5: backslash-before-quote AppleScript injection regression
    # ------------------------------------------------------------------

    def test_escape_as_str_backslash_first_ordering(self):
        """_escape_as_str must escape backslash BEFORE quote.

        Input 'Stand\\"' (backslash + quote) would produce 'Stand\\\\"' with
        the wrong order (quote first), leaving a literal \\ that cancels the
        escaping of the following quote — allowing AppleScript injection.
        With correct (backslash-first) ordering the result is 'Stand\\\\\\"':
        the backslash is doubled first, then the quote is escaped separately.
        """
        result = BackendService._escape_as_str('Stand\\"')
        # Backslash must be doubled: \ → \\
        # Quote must be escaped:     " → \"
        # Combined input \\" → \\\\\" (four chars: \\, \, ")
        self.assertIn('\\\\', result, "Backslash must be doubled")
        self.assertIn('\\"', result, "Quote must be backslash-escaped")
        # The dangerous sequence (unescaped backslash cancelling quote escape)
        # must NOT appear: a single \ followed immediately by "
        # After escaping, there must be no odd-count backslash run before "
        # Simplest check: the raw literal \" (one backslash, one quote) only
        # appears as part of a larger \\" (two backslashes + quote) sequence.
        self.assertNotIn('\\"', result.replace('\\\\"', ''))

    def test_escape_as_str_plain_string_unchanged(self):
        """Strings without special chars pass through _escape_as_str untouched."""
        s = "Hello World 2026"
        self.assertEqual(BackendService._escape_as_str(s), s)

    def test_escape_as_str_strips_control_chars(self):
        """Newlines and NUL bytes in title must be replaced with spaces."""
        s = "line1\nline2\rline3\x00end"
        result = BackendService._escape_as_str(s)
        self.assertNotIn('\n', result)
        self.assertNotIn('\r', result)
        self.assertNotIn('\x00', result)

    @patch("subprocess.run")
    def test_backslash_in_title_does_not_escape_quote(self, mock_run):
        """Injection attempt 'foo\\"; do evil' must not produce executable AS.

        W1028-F5: input 'foo\\"; do evil' with naive quote-first escaping
        produces 'foo\\\\"; do evil' — the \\\\ is interpreted as two
        literal backslashes and the " closes the AS string, breaking out.
        With backslash-first escaping it produces 'foo\\\\\\\\"; do evil' —
        safe because the backslashes are fully doubled before the quote is escaped.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        injection_title = 'foo\\"; do evil'
        self.service.handle_create_calendar_event({
            "title": injection_title,
            "start_date": "05/26/2026 10:00:00",
        })

        script = mock_run.call_args[0][0][2]
        # The raw injection sequence must not appear literally in the script
        self.assertNotIn('foo\\"; do evil', script,
                         "Injection sequence must be neutralised by escaping")
        # The backslash must be doubled (safe)
        self.assertIn('\\\\', script, "Backslash must be doubled in script")

    @patch("subprocess.run")
    def test_backslash_in_notes_does_not_escape_quote(self, mock_run):
        """Same backslash-injection test for notes field."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.service.handle_create_calendar_event({
            "title": "Safe title",
            "notes": 'bad\\"; inject',
            "start_date": "05/26/2026 11:00:00",
        })

        script = mock_run.call_args[0][0][2]
        self.assertNotIn('bad\\"; inject', script)

    @patch("subprocess.run")
    def test_backslash_in_calendar_name_does_not_escape_quote(self, mock_run):
        """Same backslash-injection test for calendar_name field."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.service.handle_create_calendar_event({
            "title": "Safe title",
            "start_date": "05/26/2026 12:00:00",
            "calendar_name": 'Work\\"; malicious',
        })

        script = mock_run.call_args[0][0][2]
        self.assertNotIn('Work\\"; malicious', script)


if __name__ == "__main__":
    unittest.main()
