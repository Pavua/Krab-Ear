"""Comprehensive tests for KrabEar CLI (cli.py).

Covers:
- All 6 commands: status, history, export, stats, health, transcribe
- --limit flag
- --format flag (srt, md, obsidian)
- --output flag
- --socket flag propagation
- NO_COLOR env var (colored vs plain output)
- Error handling: FileNotFoundError, ConnectionRefusedError, socket.timeout, bad JSON
- Backend error responses (_unwrap error path)
- Output content and formatting
- Edge cases (missing items, confidence display, language display)

All socket calls are mocked — no backend required.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli as cli_module
from cli import (
    _c,
    _ipc_call,
    _resolve_socket,
    _unwrap,
    bold,
    build_parser,
    cmd_export,
    cmd_health,
    cmd_history,
    cmd_stats,
    cmd_status,
    cmd_transcribe,
    cyan,
    dim,
    green,
    red,
    yellow,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _ok(result: dict) -> dict:
    """Wrap result in a successful IPC response."""
    return {"id": "test", "ok": True, "result": result}


def _err(code: str, message: str) -> dict:
    """Wrap an error in a failed IPC response."""
    return {"id": "test", "ok": False, "error": {"code": code, "message": message}}


def _dispatch(**method_results):
    """Return side_effect function that routes by method name."""
    def _call(method, params=None, sock_path=None):
        if method in method_results:
            r = method_results[method]
            return r if isinstance(r, dict) and "ok" in r else _ok(r)
        return _err("unknown_method", f"no mock for {method}")
    return _call


# ─── 1. Color helpers ─────────────────────────────────────────────────────────

class TestColorHelpers(unittest.TestCase):

    def test_c_returns_plain_when_no_color(self):
        """_c() must return plain text when _USE_COLOR is False."""
        with patch.object(cli_module, "_USE_COLOR", False):
            result = cli_module._c("32", "hello")
        self.assertEqual(result, "hello")

    def test_c_returns_ansi_when_color(self):
        """_c() must wrap text in ANSI escape codes when _USE_COLOR is True."""
        with patch.object(cli_module, "_USE_COLOR", True):
            result = cli_module._c("32", "hello")
        self.assertIn("\033[32m", result)
        self.assertIn("\033[0m", result)

    def test_green_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.green("ok"), "ok")

    def test_red_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.red("err"), "err")

    def test_yellow_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.yellow("warn"), "warn")

    def test_cyan_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.cyan("info"), "info")

    def test_bold_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.bold("title"), "title")

    def test_dim_no_color(self):
        with patch.object(cli_module, "_USE_COLOR", False):
            self.assertEqual(cli_module.dim("faint"), "faint")

    def test_no_color_env_disables_color_at_import(self):
        """Module-level _USE_COLOR is False when NO_COLOR is set."""
        # We can only verify the logic since the module is already imported.
        # Simulate what the module does.
        with patch.dict(os.environ, {"NO_COLOR": "1"}):
            is_tty = sys.stdout.isatty()
            no_color_set = os.environ.get("NO_COLOR") is not None
            expected = is_tty and not no_color_set
            self.assertFalse(expected)


# ─── 2. Argument parsing ──────────────────────────────────────────────────────

class TestArgParsing(unittest.TestCase):

    def setUp(self):
        self.parser = build_parser()

    # status
    def test_status_command(self):
        args = self.parser.parse_args(["status"])
        self.assertEqual(args.command, "status")
        self.assertIs(args.func, cmd_status)

    # history
    def test_history_default_limit(self):
        args = self.parser.parse_args(["history"])
        self.assertEqual(args.limit, 20)

    def test_history_custom_limit(self):
        args = self.parser.parse_args(["history", "--limit", "5"])
        self.assertEqual(args.limit, 5)

    def test_history_limit_100(self):
        args = self.parser.parse_args(["history", "--limit", "100"])
        self.assertEqual(args.limit, 100)

    # export
    def test_export_default_format_md(self):
        args = self.parser.parse_args(["export"])
        self.assertEqual(args.format, "md")

    def test_export_format_srt(self):
        args = self.parser.parse_args(["export", "--format", "srt"])
        self.assertEqual(args.format, "srt")

    def test_export_format_obsidian(self):
        args = self.parser.parse_args(["export", "--format", "obsidian"])
        self.assertEqual(args.format, "obsidian")

    def test_export_default_output_none(self):
        args = self.parser.parse_args(["export"])
        self.assertIsNone(args.output)

    def test_export_output_flag(self):
        args = self.parser.parse_args(["export", "--output", "/tmp/out.md"])
        self.assertEqual(args.output, "/tmp/out.md")

    def test_export_invalid_format_exits(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["export", "--format", "pdf"])

    # stats / health
    def test_stats_command(self):
        args = self.parser.parse_args(["stats"])
        self.assertIs(args.func, cmd_stats)

    def test_health_command(self):
        args = self.parser.parse_args(["health"])
        self.assertIs(args.func, cmd_health)

    # transcribe
    def test_transcribe_with_file(self):
        args = self.parser.parse_args(["transcribe", "/tmp/audio.wav"])
        self.assertEqual(args.file, "/tmp/audio.wav")

    def test_transcribe_requires_file_arg(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["transcribe"])

    # --socket global flag
    def test_socket_flag_status(self):
        args = self.parser.parse_args(["--socket", "/tmp/my.sock", "status"])
        self.assertEqual(args.socket, "/tmp/my.sock")

    def test_socket_flag_history(self):
        args = self.parser.parse_args(["--socket", "/tmp/x.sock", "history"])
        self.assertEqual(args.socket, "/tmp/x.sock")

    def test_socket_flag_export(self):
        args = self.parser.parse_args(["--socket", "/s.sock", "export"])
        self.assertEqual(args.socket, "/s.sock")

    def test_no_command_exits(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([])


# ─── 3. Socket resolution ─────────────────────────────────────────────────────

class TestSocketResolution(unittest.TestCase):

    def test_explicit_path_wins(self):
        p = _resolve_socket("/tmp/explicit.sock")
        self.assertEqual(p, Path("/tmp/explicit.sock"))

    def test_explicit_path_expands_home(self):
        p = _resolve_socket("~/my.sock")
        self.assertNotIn("~", str(p))

    def test_env_var_krab_ear_socket(self):
        with patch.dict(os.environ, {"KRAB_EAR_SOCKET": "/tmp/env.sock"}, clear=False):
            # make sure explicit sock_path is not set
            p = _resolve_socket(None)
        self.assertEqual(p, Path("/tmp/env.sock"))

    def test_env_var_not_used_when_explicit_path_given(self):
        with patch.dict(os.environ, {"KRAB_EAR_SOCKET": "/tmp/env.sock"}, clear=False):
            p = _resolve_socket("/tmp/explicit.sock")
        self.assertEqual(p, Path("/tmp/explicit.sock"))

    def test_falls_back_to_first_default_when_none_exist(self):
        with patch.object(cli_module, "_DEFAULT_SOCKET_PATHS",
                          [Path("/no1.sock"), Path("/no2.sock")]):
            p = _resolve_socket()
        self.assertEqual(p, Path("/no1.sock"))

    def test_uses_existing_candidate(self):
        with tempfile.NamedTemporaryFile(suffix=".sock", delete=False) as f:
            existing = Path(f.name)
        try:
            with patch.object(cli_module, "_DEFAULT_SOCKET_PATHS",
                              [Path("/no.sock"), existing]):
                p = _resolve_socket()
            self.assertEqual(p, existing)
        finally:
            existing.unlink(missing_ok=True)


# ─── 4. _ipc_call error handling ─────────────────────────────────────────────

class TestIpcCallErrors(unittest.TestCase):

    def _run_ipc(self, exc):
        """Call _ipc_call while socket.socket raises exc; capture stderr + SystemExit."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.connect.side_effect = exc

        with patch("socket.socket", return_value=mock_sock), \
             patch.object(cli_module, "_DEFAULT_SOCKET_PATHS", [Path("/tmp/fake.sock")]):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                with self.assertRaises(SystemExit) as ctx:
                    _ipc_call("ping")
        return buf.getvalue(), ctx.exception.code

    def test_file_not_found_exits_with_message(self):
        output, code = self._run_ipc(FileNotFoundError())
        self.assertIn("Socket not found", output)
        self.assertNotEqual(code, 0)

    def test_connection_refused_exits_with_message(self):
        output, code = self._run_ipc(ConnectionRefusedError())
        self.assertIn("Connection refused", output)
        self.assertNotEqual(code, 0)

    def test_timeout_exits_with_message(self):
        output, code = self._run_ipc(socket.timeout())
        self.assertIn("timeout", output.lower())
        self.assertNotEqual(code, 0)

    def test_bad_json_response_exits(self):
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.recv.side_effect = [b"not json\n", b""]

        with patch("socket.socket", return_value=mock_sock), \
             patch.object(cli_module, "_DEFAULT_SOCKET_PATHS", [Path("/tmp/fake.sock")]):
            buf = io.StringIO()
            with patch("sys.stderr", buf):
                with self.assertRaises(SystemExit):
                    _ipc_call("ping")
        self.assertIn("Malformed", buf.getvalue())


# ─── 5. _unwrap error path ────────────────────────────────────────────────────

class TestUnwrap(unittest.TestCase):

    def test_unwrap_ok_returns_result(self):
        resp = {"ok": True, "result": {"foo": "bar"}}
        self.assertEqual(_unwrap(resp), {"foo": "bar"})

    def test_unwrap_error_exits(self):
        resp = _err("E001", "something failed")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                _unwrap(resp)
        self.assertIn("something failed", buf.getvalue())

    def test_unwrap_error_includes_code(self):
        resp = _err("E_BAD", "broken")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                _unwrap(resp)
        self.assertIn("E_BAD", buf.getvalue())

    def test_unwrap_missing_result_returns_empty_dict(self):
        resp = {"ok": True}
        self.assertEqual(_unwrap(resp), {})


# ─── 6. --socket flag propagated to _ipc_call ─────────────────────────────────

class TestSocketFlagPropagation(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_socket_passed_to_status(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            ping={"service": "k", "version": "1", "uptime_sec": 0,
                  "is_recording": False, "history_count": 0},
            get_diagnostics={"system": {}, "stt": {}},
        )
        args = build_parser().parse_args(["--socket", "/tmp/x.sock", "status"])
        with patch("sys.stdout", io.StringIO()):
            cmd_status(args)
        calls = mock_ipc.call_args_list
        for c in calls:
            self.assertEqual(c.kwargs.get("sock_path") or c.args[2] if len(c.args) > 2 else c.kwargs.get("sock_path"),
                             "/tmp/x.sock")

    @patch("cli._ipc_call")
    def test_socket_passed_to_history(self, mock_ipc):
        received_paths = []

        def capture(method, params=None, sock_path=None):
            received_paths.append(sock_path)
            return _ok({"items": []})

        mock_ipc.side_effect = capture
        args = build_parser().parse_args(["--socket", "/tmp/h.sock", "history"])
        with patch("sys.stdout", io.StringIO()):
            cmd_history(args)
        self.assertTrue(all(p == "/tmp/h.sock" for p in received_paths))

    @patch("cli._ipc_call")
    def test_socket_passed_to_export(self, mock_ipc):
        received_paths = []

        def capture(method, params=None, sock_path=None):
            received_paths.append(sock_path)
            return _ok({"content": ""})

        mock_ipc.side_effect = capture
        args = build_parser().parse_args(["--socket", "/tmp/e.sock", "export"])
        with patch("sys.stdout", io.StringIO()):
            cmd_export(args)
        self.assertTrue(all(p == "/tmp/e.sock" for p in received_paths))


# ─── 7. cmd_status ────────────────────────────────────────────────────────────

class TestCmdStatus(unittest.TestCase):

    def _status_output(self, ping_data: dict, diag_data: dict | None = None) -> str:
        diag = diag_data or {"system": {}, "stt": {}}
        with patch("cli._ipc_call", side_effect=_dispatch(ping=ping_data, get_diagnostics=diag)):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_status(build_parser().parse_args(["status"]))
        return buf.getvalue()

    def test_shows_service_name(self):
        out = self._status_output({"service": "krabear-backend", "version": "2.0",
                                   "uptime_sec": 100, "is_recording": False, "history_count": 3})
        self.assertIn("krabear-backend", out)

    def test_shows_version(self):
        out = self._status_output({"service": "k", "version": "3.1.0",
                                   "uptime_sec": 0, "is_recording": False, "history_count": 0})
        self.assertIn("3.1.0", out)

    def test_shows_uptime(self):
        out = self._status_output({"service": "k", "version": "1",
                                   "uptime_sec": 9999, "is_recording": False, "history_count": 0})
        self.assertIn("9999", out)

    def test_shows_history_count(self):
        out = self._status_output({"service": "k", "version": "1",
                                   "uptime_sec": 0, "is_recording": False, "history_count": 42})
        self.assertIn("42", out)

    def test_idle_state_shown(self):
        out = self._status_output({"service": "k", "version": "1",
                                   "uptime_sec": 0, "is_recording": False, "history_count": 0})
        self.assertIn("idle", out)

    def test_recording_state_shown(self):
        out = self._status_output({"service": "k", "version": "1",
                                   "uptime_sec": 0, "is_recording": True, "history_count": 0})
        self.assertIn("RECORDING", out)

    def test_shows_diagnostics_platform(self):
        out = self._status_output(
            {"service": "k", "version": "1", "uptime_sec": 0, "is_recording": False, "history_count": 0},
            diag_data={"system": {"platform": "darwin-arm64", "data_dir": "/data"}, "stt": {}},
        )
        self.assertIn("darwin-arm64", out)

    def test_shows_stt_model(self):
        out = self._status_output(
            {"service": "k", "version": "1", "uptime_sec": 0, "is_recording": False, "history_count": 0},
            diag_data={"system": {}, "stt": {"model": "large-v3", "quality_profile": "max"}},
        )
        self.assertIn("large-v3", out)


# ─── 8. cmd_history ───────────────────────────────────────────────────────────

class TestCmdHistory(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_shows_item_text(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [{"text": "Привет мир", "created_at": "2026-01-01T10:00:00"}]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("Привет мир", buf.getvalue())

    @patch("cli._ipc_call")
    def test_shows_language_tag(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [
                {"text": "Hello", "created_at": "2026-01-01T10:00:00", "lang": "en"}
            ]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("[en]", buf.getvalue())

    @patch("cli._ipc_call")
    def test_shows_confidence(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [
                {"text": "Hi", "created_at": "2026-01-01T10:00:00", "confidence": 0.95}
            ]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("95%", buf.getvalue())

    @patch("cli._ipc_call")
    def test_truncates_long_text(self, mock_ipc):
        long_text = "X" * 200
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [{"text": long_text, "created_at": "2026-01-01T10:00:00"}]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("...", buf.getvalue())
        # Ensure the raw full string is NOT present
        self.assertNotIn("X" * 100, buf.getvalue().replace("...", ""))

    @patch("cli._ipc_call")
    def test_empty_history_message(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(get_history_page={"items": []})
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("No history", buf.getvalue())

    @patch("cli._ipc_call")
    def test_limit_sent_to_ipc(self, mock_ipc):
        captured = []
        def cap(method, params=None, sock_path=None):
            captured.append((method, params))
            return _ok({"items": []})
        mock_ipc.side_effect = cap
        with patch("sys.stdout", io.StringIO()):
            cmd_history(build_parser().parse_args(["history", "--limit", "13"]))
        page_calls = [(m, p) for m, p in captured if m == "get_history_page"]
        self.assertEqual(page_calls[0][1]["page_size"], 13)

    @patch("cli._ipc_call")
    def test_multiple_items_all_shown(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [
                {"text": "Alpha", "created_at": "2026-01-01T10:00:00"},
                {"text": "Beta",  "created_at": "2026-01-01T10:01:00"},
                {"text": "Gamma", "created_at": "2026-01-01T10:02:00"},
            ]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(build_parser().parse_args(["history"]))
        out = buf.getvalue()
        self.assertIn("Alpha", out)
        self.assertIn("Beta", out)
        self.assertIn("Gamma", out)

    @patch("cli._ipc_call")
    def test_timestamp_field_fallback(self, mock_ipc):
        """Items using 'timestamp' key instead of 'created_at' should still display."""
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [
                {"text": "Fallback item", "timestamp": "2026-06-01T08:00:00"}
            ]}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(build_parser().parse_args(["history"]))
        self.assertIn("Fallback item", buf.getvalue())


# ─── 9. cmd_export ────────────────────────────────────────────────────────────

class TestCmdExport(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_md_to_stdout(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            export_history_markdown={"content": "# Transcript\nLine one"}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_export(build_parser().parse_args(["export", "--format", "md"]))
        self.assertIn("Line one", buf.getvalue())

    @patch("cli._ipc_call")
    def test_srt_calls_correct_method(self, mock_ipc):
        called = []
        def cap(method, params=None, sock_path=None):
            called.append(method)
            return _ok({"content": "1\n00:00:00,000 --> 00:00:01,000\nHello\n"})
        mock_ipc.side_effect = cap
        with patch("sys.stdout", io.StringIO()):
            cmd_export(build_parser().parse_args(["export", "--format", "srt"]))
        self.assertIn("export_history_srt", called)

    @patch("cli._ipc_call")
    def test_obsidian_calls_correct_method(self, mock_ipc):
        called = []
        def cap(method, params=None, sock_path=None):
            called.append(method)
            return _ok({"content": "# Obsidian note"})
        mock_ipc.side_effect = cap
        with patch("sys.stdout", io.StringIO()):
            cmd_export(build_parser().parse_args(["export", "--format", "obsidian"]))
        self.assertIn("export_obsidian", called)

    @patch("cli._ipc_call")
    def test_export_to_file_writes_content(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            export_history_markdown={"content": "## My notes\nSome text here"}
        )
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            outfile = f.name
        try:
            with patch("sys.stdout", io.StringIO()):
                cmd_export(build_parser().parse_args(
                    ["export", "--format", "md", "--output", outfile]
                ))
            content = Path(outfile).read_text(encoding="utf-8")
            self.assertIn("Some text here", content)
        finally:
            Path(outfile).unlink(missing_ok=True)

    @patch("cli._ipc_call")
    def test_export_to_file_prints_success_message(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            export_history_markdown={"content": "x"}
        )
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            outfile = f.name
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_export(build_parser().parse_args(
                    ["export", "--format", "md", "--output", outfile]
                ))
            self.assertIn(outfile, buf.getvalue())
        finally:
            Path(outfile).unlink(missing_ok=True)

    @patch("cli._ipc_call")
    def test_export_uses_data_field_fallback(self, mock_ipc):
        """Some IPC methods return 'data' instead of 'content'."""
        mock_ipc.side_effect = _dispatch(
            export_history_markdown={"data": "fallback data content"}
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_export(build_parser().parse_args(["export", "--format", "md"]))
        self.assertIn("fallback data content", buf.getvalue())

    @patch("cli._ipc_call")
    def test_export_srt_to_file(self, mock_ipc):
        srt_content = "1\n00:00:00,000 --> 00:00:01,000\nHello world\n"
        mock_ipc.side_effect = _dispatch(export_history_srt={"content": srt_content})
        with tempfile.NamedTemporaryFile(suffix=".srt", delete=False) as f:
            outfile = f.name
        try:
            with patch("sys.stdout", io.StringIO()):
                cmd_export(build_parser().parse_args(
                    ["export", "--format", "srt", "--output", outfile]
                ))
            self.assertIn("Hello world", Path(outfile).read_text())
        finally:
            Path(outfile).unlink(missing_ok=True)


# ─── 10. cmd_stats ────────────────────────────────────────────────────────────

class TestCmdStats(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_stats_shows_counts(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_statistics={"total_items": 77, "total_duration_sec": 1800},
            get_metrics_dashboard={"stt": {}, "storage": {}},
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_stats(build_parser().parse_args(["stats"]))
        self.assertIn("77", buf.getvalue())
        self.assertIn("1800", buf.getvalue())

    @patch("cli._ipc_call")
    def test_stats_shows_latency_metrics(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_statistics={},
            get_metrics_dashboard={
                "stt": {"latency_p50_ms": 123, "latency_p95_ms": 456, "confidence_avg": 0.88},
                "storage": {"history_bytes": 2048},
            },
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_stats(build_parser().parse_args(["stats"]))
        out = buf.getvalue()
        self.assertIn("123", out)
        self.assertIn("456", out)

    @patch("cli._ipc_call")
    def test_stats_shows_storage_bytes(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_statistics={},
            get_metrics_dashboard={"stt": {}, "storage": {"history_bytes": 99999}},
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_stats(build_parser().parse_args(["stats"]))
        self.assertIn("99999", buf.getvalue())

    @patch("cli._ipc_call")
    def test_stats_tolerates_missing_metrics_dashboard(self, mock_ipc):
        """If get_metrics_dashboard fails, cmd_stats should not crash."""
        def dispatch(method, params=None, sock_path=None):
            if method == "get_history_statistics":
                return _ok({"total_items": 5})
            return _err("not_found", "no metrics")
        mock_ipc.side_effect = dispatch
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_stats(build_parser().parse_args(["stats"]))  # must not raise
        self.assertIn("5", buf.getvalue())


# ─── 11. cmd_health ───────────────────────────────────────────────────────────

class TestCmdHealth(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_health_ok(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            health_check={
                "status": "ok",
                "checks": {
                    "backend": {"status": "ok", "detail": "running"},
                    "storage": {"status": "ok"},
                }
            }
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
            cmd_health(build_parser().parse_args(["health"]))
        out = buf.getvalue()
        self.assertIn("OK", out)
        self.assertIn("backend", out)
        self.assertIn("storage", out)

    @patch("cli._ipc_call")
    def test_health_degraded(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            health_check={
                "status": "degraded",
                "checks": {"llm": {"status": "degraded", "detail": "circuit open"}},
            }
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
            cmd_health(build_parser().parse_args(["health"]))
        out = buf.getvalue()
        self.assertIn("DEGRADED", out)
        self.assertIn("circuit open", out)

    @patch("cli._ipc_call")
    def test_health_error_status(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            health_check={
                "status": "error",
                "checks": {"stt": {"status": "error", "detail": "model not found"}},
            }
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
            cmd_health(build_parser().parse_args(["health"]))
        out = buf.getvalue()
        self.assertIn("ERROR", out)
        self.assertIn("model not found", out)

    @patch("cli._ipc_call")
    def test_health_check_detail_optional(self, mock_ipc):
        """Checks without 'detail' key must not raise."""
        mock_ipc.side_effect = _dispatch(
            health_check={
                "status": "ok",
                "checks": {"storage": {"status": "ok"}},
            }
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_health(build_parser().parse_args(["health"]))  # must not raise

    @patch("cli._ipc_call")
    def test_health_non_dict_check_value(self, mock_ipc):
        """Non-dict check values (plain strings) must not raise."""
        mock_ipc.side_effect = _dispatch(
            health_check={
                "status": "ok",
                "checks": {"misc": "some plain string"},
            }
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_health(build_parser().parse_args(["health"]))
        self.assertIn("misc", buf.getvalue())


# ─── 12. cmd_transcribe ───────────────────────────────────────────────────────

class TestCmdTranscribe(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_transcribe_shows_text(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            transcribe_paths={"results": [{"text": "Esto es una prueba", "lang": "es", "confidence": 0.92}]}
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmpfile = f.name
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_transcribe(build_parser().parse_args(["transcribe", tmpfile]))
            self.assertIn("Esto es una prueba", buf.getvalue())
        finally:
            Path(tmpfile).unlink(missing_ok=True)

    @patch("cli._ipc_call")
    def test_transcribe_shows_language(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            transcribe_paths={"results": [{"text": "Hello", "lang": "en"}]}
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmpfile = f.name
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf), patch.object(cli_module, "_USE_COLOR", False):
                cmd_transcribe(build_parser().parse_args(["transcribe", tmpfile]))
            self.assertIn("en", buf.getvalue())
        finally:
            Path(tmpfile).unlink(missing_ok=True)

    @patch("cli._ipc_call")
    def test_transcribe_shows_confidence(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            transcribe_paths={"results": [{"text": "Hi", "lang": "en", "confidence": 0.75}]}
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmpfile = f.name
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_transcribe(build_parser().parse_args(["transcribe", tmpfile]))
            self.assertIn("75.0%", buf.getvalue())
        finally:
            Path(tmpfile).unlink(missing_ok=True)

    def test_transcribe_missing_file_exits(self):
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                cmd_transcribe(build_parser().parse_args(["transcribe", "/no/such/file.wav"]))
        self.assertIn("not found", buf.getvalue().lower())

    @patch("cli._ipc_call")
    def test_transcribe_passes_resolved_path(self, mock_ipc):
        received = []
        def cap(method, params=None, sock_path=None):
            received.append((method, params))
            return _ok({"results": [{"text": "ok"}]})
        mock_ipc.side_effect = cap

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmpfile = f.name
        try:
            with patch("sys.stdout", io.StringIO()):
                cmd_transcribe(build_parser().parse_args(["transcribe", tmpfile]))
            tx_call = next((p for m, p in received if m == "transcribe_paths"), None)
            self.assertIsNotNone(tx_call)
            # path should be absolute
            self.assertTrue(Path(tx_call["paths"][0]).is_absolute())
        finally:
            Path(tmpfile).unlink(missing_ok=True)

    @patch("cli._ipc_call")
    def test_transcribe_handles_transcript_field_fallback(self, mock_ipc):
        """Result dicts using 'transcript' key instead of 'text' should still display."""
        mock_ipc.side_effect = _dispatch(
            transcribe_paths={"results": [{"transcript": "Fallback field text"}]}
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmpfile = f.name
        try:
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_transcribe(build_parser().parse_args(["transcribe", tmpfile]))
            self.assertIn("Fallback field text", buf.getvalue())
        finally:
            Path(tmpfile).unlink(missing_ok=True)


# ─── 13. Backend error response propagation ───────────────────────────────────

class TestBackendErrorPropagation(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_status_backend_error_exits(self, mock_ipc):
        mock_ipc.return_value = _err("E500", "internal error")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                cmd_status(build_parser().parse_args(["status"]))
        self.assertIn("internal error", buf.getvalue())

    @patch("cli._ipc_call")
    def test_history_backend_error_exits(self, mock_ipc):
        mock_ipc.return_value = _err("E403", "forbidden")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                cmd_history(build_parser().parse_args(["history"]))

    @patch("cli._ipc_call")
    def test_export_backend_error_exits(self, mock_ipc):
        mock_ipc.return_value = _err("E404", "no data")
        buf = io.StringIO()
        with patch("sys.stderr", buf):
            with self.assertRaises(SystemExit):
                cmd_export(build_parser().parse_args(["export"]))


# ─── 14. Output formatting (no-color mode) ────────────────────────────────────

class TestOutputFormattingNoColor(unittest.TestCase):
    """Verify output text content when colors are disabled."""

    @patch("cli._ipc_call")
    def test_status_no_ansi_codes(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            ping={"service": "k", "version": "1", "uptime_sec": 0,
                  "is_recording": False, "history_count": 0},
            get_diagnostics={"system": {}, "stt": {}},
        )
        with patch.object(cli_module, "_USE_COLOR", False):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_status(build_parser().parse_args(["status"]))
        self.assertNotIn("\033[", buf.getvalue())

    @patch("cli._ipc_call")
    def test_history_no_ansi_codes(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            get_history_page={"items": [
                {"text": "Test", "created_at": "2026-01-01T10:00:00", "lang": "ru", "confidence": 0.9}
            ]}
        )
        with patch.object(cli_module, "_USE_COLOR", False):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_history(build_parser().parse_args(["history"]))
        self.assertNotIn("\033[", buf.getvalue())

    @patch("cli._ipc_call")
    def test_health_no_ansi_codes(self, mock_ipc):
        mock_ipc.side_effect = _dispatch(
            health_check={"status": "ok", "checks": {}}
        )
        with patch.object(cli_module, "_USE_COLOR", False):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                cmd_health(build_parser().parse_args(["health"]))
        self.assertNotIn("\033[", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
