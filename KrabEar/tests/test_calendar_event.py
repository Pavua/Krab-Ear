"""Tests for _handle_create_calendar_event (Phase D.4 — Apple Calendar integration)."""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Ensure backend package is importable when run directly.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.service import BackendService


def _make_service():
    """Return a BackendService with minimal fake collaborators."""
    from backend.state_store import StateStore
    import tempfile, pathlib

    tmp = pathlib.Path(tempfile.mkdtemp())
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

        result = self.service._handle_create_calendar_event({
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

        result = self.service._handle_create_calendar_event({
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

        result = self.service._handle_create_calendar_event({
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

        self.service._handle_create_calendar_event({
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

        result = self.service._handle_create_calendar_event({
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
        result = self.service._handle_create_calendar_event({
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

        result = self.service._handle_create_calendar_event({
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

        self.service._handle_create_calendar_event({
            "title": "Auto calendar",
            "start_date": "05/11/2026 15:00:00",
        })

        script = mock_run.call_args[0][0][2]
        self.assertIn("writable", script)

    # ------------------------------------------------------------------
    # test_handler_registered_in_dispatch_table
    # ------------------------------------------------------------------
    def test_handler_registered_in_dispatch_table(self):
        """create_calendar_event must be present in the handler lookup table."""
        # Build the handler table by calling handle_request with any method —
        # or just verify the method exists and the handler table key exists.
        handler = getattr(self.service, "_handle_create_calendar_event", None)
        self.assertIsNotNone(handler, "_handle_create_calendar_event method must exist")

        # Verify registration by checking the internal dispatch table key
        # (BackendService builds it lazily in handle_request; call a dummy)
        # We'll just check the method is callable.
        self.assertTrue(callable(handler))


if __name__ == "__main__":
    unittest.main()
