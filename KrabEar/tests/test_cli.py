"""Tests for KrabEar CLI (cli.py).

Tests cover argument parsing, output formatting, and IPC call delegation.
All network/socket calls are mocked — no backend required.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cli import (
    build_parser,
    cmd_export,
    cmd_health,
    cmd_history,
    cmd_stats,
    cmd_status,
    cmd_transcribe,
    _resolve_socket,
    _USE_COLOR,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

def _ok(result: dict) -> dict:
    """Wrap result in a successful IPC response."""
    return {"id": "test", "ok": True, "result": result}


def _ipc(method_results: dict):
    """Return a side_effect function that dispatches to method_results by method name."""
    def _call(method, params=None, sock_path=None):
        if method in method_results:
            return _ok(method_results[method])
        return {"id": "test", "ok": False, "error": {"code": "unknown_method", "message": f"no mock for {method}"}}
    return _call


# ─── Argument parsing tests ───────────────────────────────────────────────────

class TestArgParsing(unittest.TestCase):

    def setUp(self):
        self.parser = build_parser()

    def test_status_command_parsed(self):
        args = self.parser.parse_args(["status"])
        self.assertEqual(args.command, "status")
        self.assertIs(args.func, cmd_status)

    def test_history_default_limit(self):
        args = self.parser.parse_args(["history"])
        self.assertEqual(args.command, "history")
        self.assertEqual(args.limit, 20)

    def test_history_custom_limit(self):
        args = self.parser.parse_args(["history", "--limit", "50"])
        self.assertEqual(args.limit, 50)

    def test_export_default_format(self):
        args = self.parser.parse_args(["export"])
        self.assertEqual(args.format, "md")
        self.assertIsNone(args.output)

    def test_export_srt_with_output(self):
        args = self.parser.parse_args(["export", "--format", "srt", "--output", "/tmp/out.srt"])
        self.assertEqual(args.format, "srt")
        self.assertEqual(args.output, "/tmp/out.srt")

    def test_export_obsidian_format(self):
        args = self.parser.parse_args(["export", "--format", "obsidian"])
        self.assertEqual(args.format, "obsidian")

    def test_stats_command(self):
        args = self.parser.parse_args(["stats"])
        self.assertIs(args.func, cmd_stats)

    def test_health_command(self):
        args = self.parser.parse_args(["health"])
        self.assertIs(args.func, cmd_health)

    def test_transcribe_requires_file(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["transcribe"])

    def test_transcribe_with_file(self):
        args = self.parser.parse_args(["transcribe", "/tmp/audio.wav"])
        self.assertEqual(args.file, "/tmp/audio.wav")

    def test_global_socket_flag(self):
        args = self.parser.parse_args(["--socket", "/tmp/test.sock", "status"])
        self.assertEqual(args.socket, "/tmp/test.sock")

    def test_invalid_export_format_rejected(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["export", "--format", "xml"])

    def test_no_command_exits(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([])


# ─── Socket resolution tests ─────────────────────────────────────────────────

class TestSocketResolution(unittest.TestCase):

    def test_explicit_path_used(self):
        p = _resolve_socket("/tmp/my.sock")
        self.assertEqual(p, Path("/tmp/my.sock"))

    def test_env_var_respected(self):
        import os
        with patch.dict(os.environ, {"KRAB_EAR_SOCKET": "/tmp/env.sock"}):
            p = _resolve_socket()
        self.assertEqual(p, Path("/tmp/env.sock"))

    def test_returns_first_default_when_none_exist(self):
        with patch("cli._DEFAULT_SOCKET_PATHS", [Path("/nonexistent1.sock"), Path("/nonexistent2.sock")]):
            p = _resolve_socket()
        self.assertEqual(p, Path("/nonexistent1.sock"))


# ─── cmd_status tests ─────────────────────────────────────────────────────────

class TestCmdStatus(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_status_prints_service_info(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "ping": {
                "status": "ok",
                "service": "krabear-backend",
                "version": "1.0.0",
                "uptime_sec": 42.0,
                "is_recording": False,
                "history_count": 7,
            },
            "get_diagnostics": {
                "system": {"platform": "darwin", "data_dir": "/tmp"},
                "stt": {"model": "tiny", "quality_profile": "balanced"},
            },
        })
        args = build_parser().parse_args(["status"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        output = buf.getvalue()
        self.assertIn("krabear-backend", output)
        self.assertIn("1.0.0", output)
        self.assertIn("42.0", output)
        self.assertIn("7", output)

    @patch("cli._ipc_call")
    def test_status_shows_recording_state(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "ping": {
                "service": "krabear-backend",
                "version": "1.0.0",
                "uptime_sec": 5.0,
                "is_recording": True,
                "history_count": 0,
            },
            "get_diagnostics": {"system": {}, "stt": {}},
        })
        args = build_parser().parse_args(["status"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_status(args)
        output = buf.getvalue()
        # Should contain recording indicator
        self.assertIn("RECORDING", output)


# ─── cmd_history tests ────────────────────────────────────────────────────────

class TestCmdHistory(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_history_lists_items(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "get_history_page": {
                "items": [
                    {"text": "Hello world", "created_at": "2026-01-01T12:00:00", "lang": "en", "confidence": 0.95},
                    {"text": "Привет мир", "created_at": "2026-01-01T12:01:00", "lang": "ru", "confidence": 0.87},
                ]
            }
        })
        args = build_parser().parse_args(["history", "--limit", "5"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(args)
        output = buf.getvalue()
        self.assertIn("Hello world", output)
        self.assertIn("Привет мир", output)

    @patch("cli._ipc_call")
    def test_history_empty_message(self, mock_ipc):
        mock_ipc.side_effect = _ipc({"get_history_page": {"items": []}})
        args = build_parser().parse_args(["history"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(args)
        output = buf.getvalue()
        self.assertIn("No history", output)

    @patch("cli._ipc_call")
    def test_history_truncates_long_text(self, mock_ipc):
        long_text = "A" * 200
        mock_ipc.side_effect = _ipc({
            "get_history_page": {
                "items": [{"text": long_text, "created_at": "2026-01-01T12:00:00"}]
            }
        })
        args = build_parser().parse_args(["history"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_history(args)
        output = buf.getvalue()
        self.assertIn("...", output)
        # Line should be significantly shorter than 200 chars
        lines = [l for l in output.splitlines() if "A" in l]
        self.assertTrue(any(len(l) < 150 for l in lines))

    @patch("cli._ipc_call")
    def test_history_passes_limit_to_ipc(self, mock_ipc):
        call_args_list = []
        def capturing_ipc(method, params=None, sock_path=None):
            call_args_list.append((method, params))
            return _ok({"items": []})
        mock_ipc.side_effect = capturing_ipc
        args = build_parser().parse_args(["history", "--limit", "7"])
        with patch("sys.stdout", io.StringIO()):
            cmd_history(args)
        history_calls = [(m, p) for m, p in call_args_list if m == "get_history_page"]
        self.assertEqual(len(history_calls), 1)
        self.assertEqual(history_calls[0][1]["page_size"], 7)


# ─── cmd_export tests ─────────────────────────────────────────────────────────

class TestCmdExport(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_export_md_to_stdout(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "export_history_markdown": {"content": "# My Transcript\nHello world"}
        })
        args = build_parser().parse_args(["export", "--format", "md"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_export(args)
        self.assertIn("Hello world", buf.getvalue())

    @patch("cli._ipc_call")
    def test_export_srt_calls_correct_method(self, mock_ipc):
        called_with = []
        def side_effect(method, params=None, sock_path=None):
            called_with.append(method)
            return _ok({"content": "1\n00:00:00,000 --> 00:00:01,000\nHello\n"})
        mock_ipc.side_effect = side_effect
        args = build_parser().parse_args(["export", "--format", "srt"])
        with patch("sys.stdout", io.StringIO()):
            cmd_export(args)
        self.assertIn("export_history_srt", called_with)

    @patch("cli._ipc_call")
    def test_export_to_file(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "export_history_markdown": {"content": "# Transcript\nSome content"}
        })
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
            outfile = f.name
        args = build_parser().parse_args(["export", "--format", "md", "--output", outfile])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_export(args)
        written = Path(outfile).read_text()
        self.assertIn("Some content", written)
        self.assertIn(outfile, buf.getvalue())  # success message mentions path


# ─── cmd_health tests ─────────────────────────────────────────────────────────

class TestCmdHealth(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_health_ok_output(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "health_check": {
                "status": "ok",
                "checks": {
                    "backend": {"status": "ok", "detail": "running"},
                    "storage": {"status": "ok"},
                }
            }
        })
        args = build_parser().parse_args(["health"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_health(args)
        output = buf.getvalue()
        self.assertIn("OK", output)
        self.assertIn("backend", output)
        self.assertIn("storage", output)

    @patch("cli._ipc_call")
    def test_health_degraded_output(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "health_check": {
                "status": "degraded",
                "checks": {
                    "llm": {"status": "degraded", "detail": "circuit open"},
                }
            }
        })
        args = build_parser().parse_args(["health"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_health(args)
        output = buf.getvalue()
        self.assertIn("DEGRADED", output)


# ─── cmd_stats tests ──────────────────────────────────────────────────────────

class TestCmdStats(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_stats_output(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "get_history_statistics": {
                "total_items": 42,
                "total_duration_sec": 3600,
                "languages": {"ru": 30, "es": 12},
            },
            "get_metrics_dashboard": {
                "stt": {"latency_p50_ms": 250, "latency_p95_ms": 800, "confidence_avg": 0.91},
                "storage": {"history_bytes": 102400},
            },
        })
        args = build_parser().parse_args(["stats"])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_stats(args)
        output = buf.getvalue()
        self.assertIn("42", output)
        self.assertIn("3600", output)


# ─── cmd_transcribe tests ─────────────────────────────────────────────────────

class TestCmdTranscribe(unittest.TestCase):

    @patch("cli._ipc_call")
    def test_transcribe_existing_file(self, mock_ipc):
        mock_ipc.side_effect = _ipc({
            "transcribe_paths": {
                "results": [{"text": "Test transcription result", "lang": "en", "confidence": 0.93}]
            }
        })
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmpfile = f.name
            f.write(b"RIFF" + b"\x00" * 40)  # fake wav header

        args = build_parser().parse_args(["transcribe", tmpfile])
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cmd_transcribe(args)
        output = buf.getvalue()
        self.assertIn("Test transcription result", output)
        self.assertIn("en", output)

    def test_transcribe_missing_file_exits(self):
        args = build_parser().parse_args(["transcribe", "/nonexistent/file.wav"])
        with self.assertRaises(SystemExit):
            cmd_transcribe(args)

    @patch("cli._ipc_call")
    def test_transcribe_calls_correct_ipc_method(self, mock_ipc):
        called = []
        def side_effect(method, params=None, sock_path=None):
            called.append((method, params))
            return _ok({"results": [{"text": "hello"}]})
        mock_ipc.side_effect = side_effect

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmpfile = f.name

        args = build_parser().parse_args(["transcribe", tmpfile])
        with patch("sys.stdout", io.StringIO()):
            cmd_transcribe(args)

        self.assertTrue(any(m == "transcribe_paths" for m, _ in called))
        tx_call = next((p for m, p in called if m == "transcribe_paths"), None)
        self.assertIn(tmpfile, tx_call["paths"][0])


if __name__ == "__main__":
    unittest.main()
