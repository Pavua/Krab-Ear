"""Tests for the interactive REPL mode of KrabEar CLI.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_cli_interactive.py -v
"""

from __future__ import annotations

import argparse
import sys
import types
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap (mirrors other test files in this suite)
# ---------------------------------------------------------------------------
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import cli  # noqa: E402  (KrabEar/cli.py is on sys.path via PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs) -> argparse.Namespace:
    ns = argparse.Namespace(socket=None)
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# 1. _dispatch_repl_line: unknown command prints warning
# ---------------------------------------------------------------------------

class TestDispatchUnknownCommand(unittest.TestCase):
    def test_unknown_command_prints_warning(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("foobar", sock_path=None)
        self.assertTrue(result, "Unknown command should keep the loop alive")
        output = buf.getvalue()
        self.assertIn("Unknown command", output)
        self.assertIn("foobar", output)


# ---------------------------------------------------------------------------
# 2. _dispatch_repl_line: quit / exit return False
# ---------------------------------------------------------------------------

class TestDispatchQuitExit(unittest.TestCase):
    def test_quit_returns_false(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("quit", sock_path=None)
        self.assertFalse(result)

    def test_exit_returns_false(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("exit", sock_path=None)
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# 3. _dispatch_repl_line: help prints command list
# ---------------------------------------------------------------------------

class TestDispatchHelp(unittest.TestCase):
    def test_help_lists_commands(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("help", sock_path=None)
        self.assertTrue(result)
        output = buf.getvalue()
        for cmd in ("status", "history", "export", "stats", "health", "transcribe", "search", "last", "clear", "quit"):
            self.assertIn(cmd, output, f"help output missing '{cmd}'")


# ---------------------------------------------------------------------------
# 4. _dispatch_repl_line: clear writes ANSI escape or handles gracefully
# ---------------------------------------------------------------------------

class TestDispatchClear(unittest.TestCase):
    def test_clear_returns_true_and_outputs(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("clear", sock_path=None)
        self.assertTrue(result)
        # We just verify it doesn't crash and returns True; actual ANSI codes
        # depend on terminal detection which is irrelevant in a test context.


# ---------------------------------------------------------------------------
# 5. _dispatch_repl_line: empty line keeps loop alive
# ---------------------------------------------------------------------------

class TestDispatchEmptyLine(unittest.TestCase):
    def test_empty_line_keeps_running(self):
        result = cli._dispatch_repl_line("   ", sock_path=None)
        self.assertTrue(result)

    def test_blank_line_keeps_running(self):
        result = cli._dispatch_repl_line("", sock_path=None)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# 6. _dispatch_repl_line: status delegates to cmd_status
# ---------------------------------------------------------------------------

class TestDispatchStatus(unittest.TestCase):
    def test_status_calls_cmd_status(self):
        with patch.object(cli, "cmd_status") as mock_status:
            result = cli._dispatch_repl_line("status", sock_path=None)
        self.assertTrue(result)
        mock_status.assert_called_once()
        ns_passed = mock_status.call_args[0][0]
        self.assertIsNone(ns_passed.socket)

    def test_status_propagates_socket_path(self):
        with patch.object(cli, "cmd_status") as mock_status:
            cli._dispatch_repl_line("status", sock_path="/tmp/test.sock")
        ns_passed = mock_status.call_args[0][0]
        self.assertEqual(ns_passed.socket, "/tmp/test.sock")


# ---------------------------------------------------------------------------
# 7. _dispatch_repl_line: history parses --limit
# ---------------------------------------------------------------------------

class TestDispatchHistory(unittest.TestCase):
    def test_history_default_limit(self):
        with patch.object(cli, "cmd_history") as mock_hist:
            cli._dispatch_repl_line("history", sock_path=None)
        ns = mock_hist.call_args[0][0]
        self.assertEqual(ns.limit, 20)

    def test_history_custom_limit(self):
        with patch.object(cli, "cmd_history") as mock_hist:
            cli._dispatch_repl_line("history --limit 5", sock_path=None)
        ns = mock_hist.call_args[0][0]
        self.assertEqual(ns.limit, 5)

    def test_history_invalid_limit_uses_default(self):
        with patch.object(cli, "cmd_history") as mock_hist:
            cli._dispatch_repl_line("history --limit notanumber", sock_path=None)
        ns = mock_hist.call_args[0][0]
        self.assertEqual(ns.limit, 20)


# ---------------------------------------------------------------------------
# 8. _dispatch_repl_line: export parses --format and --output
# ---------------------------------------------------------------------------

class TestDispatchExport(unittest.TestCase):
    def test_export_default_format(self):
        with patch.object(cli, "cmd_export") as mock_exp:
            cli._dispatch_repl_line("export", sock_path=None)
        ns = mock_exp.call_args[0][0]
        self.assertEqual(ns.format, "md")
        self.assertIsNone(ns.output)

    def test_export_srt_format(self):
        with patch.object(cli, "cmd_export") as mock_exp:
            cli._dispatch_repl_line("export --format srt", sock_path=None)
        ns = mock_exp.call_args[0][0]
        self.assertEqual(ns.format, "srt")

    def test_export_with_output(self):
        with patch.object(cli, "cmd_export") as mock_exp:
            cli._dispatch_repl_line("export --format md --output /tmp/out.md", sock_path=None)
        ns = mock_exp.call_args[0][0]
        self.assertEqual(ns.format, "md")
        self.assertEqual(ns.output, "/tmp/out.md")


# ---------------------------------------------------------------------------
# 9. _dispatch_repl_line: stats and health delegate correctly
# ---------------------------------------------------------------------------

class TestDispatchStatsHealth(unittest.TestCase):
    def test_stats_calls_cmd_stats(self):
        with patch.object(cli, "cmd_stats") as mock_stats:
            result = cli._dispatch_repl_line("stats", sock_path=None)
        self.assertTrue(result)
        mock_stats.assert_called_once()

    def test_health_calls_cmd_health(self):
        with patch.object(cli, "cmd_health") as mock_health:
            result = cli._dispatch_repl_line("health", sock_path=None)
        self.assertTrue(result)
        mock_health.assert_called_once()


# ---------------------------------------------------------------------------
# 10. _dispatch_repl_line: transcribe without file prints usage
# ---------------------------------------------------------------------------

class TestDispatchTranscribe(unittest.TestCase):
    def test_transcribe_no_file_prints_usage(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            result = cli._dispatch_repl_line("transcribe", sock_path=None)
        self.assertTrue(result)
        self.assertIn("Usage", buf.getvalue())

    def test_transcribe_with_file_delegates(self):
        with patch.object(cli, "cmd_transcribe") as mock_tx:
            result = cli._dispatch_repl_line("transcribe /tmp/audio.wav", sock_path=None)
        self.assertTrue(result)
        ns = mock_tx.call_args[0][0]
        self.assertEqual(ns.file, "/tmp/audio.wav")


# ---------------------------------------------------------------------------
# 11. _repl_search: no query prints usage
# ---------------------------------------------------------------------------

class TestReplSearch(unittest.TestCase):
    def test_empty_query_prints_usage(self):
        buf = StringIO()
        with patch("sys.stdout", buf):
            cli._repl_search("", sock_path=None)
        self.assertIn("Usage", buf.getvalue())

    def test_search_dispatches_ipc_and_filters(self):
        mock_resp = {
            "ok": True,
            "result": {
                "items": [
                    {"text": "Hello world", "created_at": "2026-01-01T10:00:00"},
                    {"text": "Unrelated text", "created_at": "2026-01-01T10:01:00"},
                ]
            },
        }
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_search("hello", sock_path=None)
        output = buf.getvalue()
        self.assertIn("Hello world", output)
        self.assertNotIn("Unrelated text", output)

    def test_search_no_results_says_so(self):
        mock_resp = {
            "ok": True,
            "result": {"items": [{"text": "Something else", "created_at": "2026-01-01T10:00:00"}]},
        }
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_search("zzznomatch", sock_path=None)
        self.assertIn("No results", buf.getvalue())

    def test_search_ipc_failure_shows_warning(self):
        mock_resp = {"ok": False, "error": {"code": 500, "message": "backend error"}}
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_search("test", sock_path=None)
        self.assertIn("Could not retrieve", buf.getvalue())


# ---------------------------------------------------------------------------
# 12. _repl_last: shows last item
# ---------------------------------------------------------------------------

class TestReplLast(unittest.TestCase):
    def test_last_shows_most_recent(self):
        mock_resp = {
            "ok": True,
            "result": {
                "items": [
                    {
                        "text": "Latest transcription text",
                        "created_at": "2026-04-12T12:00:00",
                        "lang": "ru",
                        "confidence": 0.95,
                    }
                ]
            },
        }
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_last(sock_path=None)
        output = buf.getvalue()
        self.assertIn("Latest transcription text", output)
        self.assertIn("ru", output)

    def test_last_no_history_says_so(self):
        mock_resp = {"ok": True, "result": {"items": []}}
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_last(sock_path=None)
        self.assertIn("No transcriptions", buf.getvalue())

    def test_last_ipc_failure_shows_warning(self):
        mock_resp = {"ok": False, "error": {"code": 500, "message": "err"}}
        buf = StringIO()
        with patch.object(cli, "_ipc_call", return_value=mock_resp):
            with patch("sys.stdout", buf):
                cli._repl_last(sock_path=None)
        self.assertIn("Could not retrieve", buf.getvalue())


# ---------------------------------------------------------------------------
# 13. cmd_interactive: Ctrl+D (EOFError) exits cleanly
# ---------------------------------------------------------------------------

class TestCmdInteractiveEOF(unittest.TestCase):
    def test_eof_exits_gracefully(self):
        args = _make_args()
        buf = StringIO()
        with patch("builtins.input", side_effect=EOFError):
            with patch("sys.stdout", buf):
                cli.cmd_interactive(args)
        # Must reach here without raising
        output = buf.getvalue()
        self.assertIn("Goodbye", output)


# ---------------------------------------------------------------------------
# 14. cmd_interactive: processes a sequence then quits
# ---------------------------------------------------------------------------

class TestCmdInteractiveSequence(unittest.TestCase):
    def test_sequence_help_then_quit(self):
        inputs = iter(["help", "quit"])
        args = _make_args()
        buf = StringIO()
        with patch("builtins.input", side_effect=inputs):
            with patch("sys.stdout", buf):
                cli.cmd_interactive(args)
        output = buf.getvalue()
        self.assertIn("status", output)   # from help
        self.assertIn("Goodbye", output)  # from quit


# ---------------------------------------------------------------------------
# 15. build_parser: interactive subcommand is registered
# ---------------------------------------------------------------------------

class TestBuildParserInteractive(unittest.TestCase):
    def test_interactive_subcommand_registered(self):
        parser = cli.build_parser()
        # parse_args should succeed and assign func=cmd_interactive
        args = parser.parse_args(["interactive"])
        self.assertEqual(args.func, cli.cmd_interactive)
        self.assertEqual(args.command, "interactive")

    def test_interactive_inherits_socket_option(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--socket", "/tmp/test.sock", "interactive"])
        self.assertEqual(args.socket, "/tmp/test.sock")
        self.assertEqual(args.func, cli.cmd_interactive)


if __name__ == "__main__":
    unittest.main()
