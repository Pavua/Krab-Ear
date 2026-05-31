"""Tests for W1193 F2+F4 fixes in backend/observability.py.

Covers:
  - _sentry_before_send redacts /Users/<name>/... → ~/...
  - _sentry_before_send drops KrabEar/transcripts/... paths
  - _sentry_before_send passes through unrelated events unchanged
  - init_sentry wires include_local_variables=False + before_send
"""

import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or tests/ directory.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.dirname(_HERE)  # KrabEar/
_REPO_ROOT = os.path.dirname(_BACKEND_ROOT)  # repo root

for _p in (_BACKEND_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Stub out heavy optional deps before importing observability.
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    """Insert lightweight stubs for imports used by observability.py.

    Wave 1744 test-isolation fix: import real modules first; stub only when
    the real import fails — prevents bare ModuleType leaks across xdist workers.
    """
    import importlib

    # plistlib is stdlib — no stub needed.
    # subprocess / signal / re / os are stdlib — no stub needed.

    # sentry_sdk is a genuinely optional external package.
    if "sentry_sdk" not in sys.modules:
        try:
            importlib.import_module("sentry_sdk")
        except Exception:
            stub = types.ModuleType("sentry_sdk")
            stub.init = MagicMock()  # type: ignore[attr-defined]
            stub.push_scope = MagicMock()  # type: ignore[attr-defined]
            stub.capture_exception = MagicMock()  # type: ignore[attr-defined]
            stub.capture_message = MagicMock()  # type: ignore[attr-defined]
            stub.add_breadcrumb = MagicMock()  # type: ignore[attr-defined]
            stub.flush = MagicMock()  # type: ignore[attr-defined]
            sys.modules["sentry_sdk"] = stub

    # backend.privacy_audit — import real module; set missing attr if needed.
    if "backend.privacy_audit" not in sys.modules:
        try:
            importlib.import_module("backend.privacy_audit")
        except Exception:
            pa_stub = types.ModuleType("backend.privacy_audit")
            pa_stub.get_privacy_audit_logger = MagicMock(  # type: ignore[attr-defined]
                return_value=MagicMock(log_event=MagicMock())
            )
            sys.modules["backend.privacy_audit"] = pa_stub

    _pa = sys.modules["backend.privacy_audit"]
    if not hasattr(_pa, "get_privacy_audit_logger"):
        _pa.get_privacy_audit_logger = MagicMock(  # type: ignore[attr-defined]
            return_value=MagicMock(log_event=MagicMock())
        )


_install_stubs()

from backend.observability import (  # noqa: E402
    _sentry_before_send,
    _TRANSCRIPT_REDACTED,
    init_sentry,
)


class TestSentryBeforeSendRedactsHomePath(unittest.TestCase):
    """_sentry_before_send replaces /Users/<name>/... with ~/..."""

    def test_redacts_abs_path_in_frame_filename(self) -> None:
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {"filename": "/Users/alice/projects/krab/main.py"},
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)
        frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(frame["filename"], "~/projects/krab/main.py")

    def test_redacts_abs_path_in_frame_abs_path(self) -> None:
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "service.py",
                                    "abs_path": "/Users/pablito/Antigravity_AGENTS/Krab Ear/KrabEar/backend/service.py",
                                },
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertTrue(
            frame["abs_path"].startswith("~/"),
            f"Expected ~/... but got {frame['abs_path']!r}",
        )
        self.assertNotIn("/Users/", frame["abs_path"])

    def test_redacts_path_inside_vars(self) -> None:
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "service.py",
                                    "vars": {
                                        "data_dir": "/Users/pablito/.krab_ear_data",
                                        "count": 42,
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        frame_vars = result["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        self.assertNotIn("/Users/", frame_vars["data_dir"])
        self.assertTrue(frame_vars["data_dir"].startswith("~/"))
        self.assertEqual(frame_vars["count"], 42)  # non-string values untouched

    def test_redacts_path_in_message(self) -> None:
        event = {"message": "Error reading /Users/bob/Library/Logs/KrabEar/backend.log"}
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("/Users/bob", result["message"])
        self.assertIn("~/Library", result["message"])

    def test_redacts_path_in_extra(self) -> None:
        event = {"extra": {"log_file": "/Users/carol/Library/Logs/KrabEar/sentry.log"}}
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("/Users/carol", result["extra"]["log_file"])


class TestSentryBeforeSendDropsTranscriptPath(unittest.TestCase):
    """_sentry_before_send replaces transcript paths with redacted marker."""

    def test_drops_transcript_path_in_frame_filename(self) -> None:
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": (
                                        "/Users/pablito/Library/Application Support/"
                                        "KrabEar/transcripts/2026-05-26T12-00-00.md"
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        frame = result["exception"]["values"][0]["stacktrace"]["frames"][0]
        self.assertEqual(frame["filename"], _TRANSCRIPT_REDACTED)

    def test_drops_transcript_path_in_extra(self) -> None:
        event = {
            "extra": {
                "path": (
                    "/Users/alice/.krab_ear_data/"
                    "KrabEar/transcripts/recording_42.md"
                )
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertEqual(result["extra"]["path"], _TRANSCRIPT_REDACTED)

    def test_drops_transcript_path_in_vars(self) -> None:
        event = {
            "exception": {
                "values": [
                    {
                        "stacktrace": {
                            "frames": [
                                {
                                    "filename": "transcriber.py",
                                    "vars": {
                                        "output_path": (
                                            "/Users/x/Library/Application Support/"
                                            "KrabEar/transcripts/foo.md"
                                        ),
                                        "duration": 3.5,
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        frame_vars = result["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        self.assertEqual(frame_vars["output_path"], _TRANSCRIPT_REDACTED)
        self.assertEqual(frame_vars["duration"], 3.5)

    def test_drops_transcript_path_in_message(self) -> None:
        transcript_path = (
            "/Users/pablito/.krab_ear_data/KrabEar/transcripts/2026-05-26T10-00.md"
        )
        event = {"message": f"Failed to write {transcript_path}"}
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("transcripts", result["message"])
        self.assertIn(_TRANSCRIPT_REDACTED, result["message"])


class TestSentryBeforeSendPassesUnrelatedEvents(unittest.TestCase):
    """_sentry_before_send returns unrelated events unchanged."""

    def test_passes_event_without_paths(self) -> None:
        event = {
            "message": "Unexpected STT timeout",
            "extra": {"method": "transcribe_audio", "duration_ms": 5000},
        }
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)
        self.assertEqual(result["message"], "Unexpected STT timeout")
        self.assertEqual(result["extra"]["method"], "transcribe_audio")

    def test_returns_event_not_none(self) -> None:
        event = {"exception": {"values": []}}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)

    def test_empty_event_passes(self) -> None:
        event: dict = {}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)

    def test_non_path_string_values_unchanged(self) -> None:
        event = {
            "message": "hello world",
            "extra": {"language": "ru", "confidence": 0.92},
        }
        result = _sentry_before_send(event, hint=None)
        self.assertEqual(result["extra"]["language"], "ru")
        self.assertAlmostEqual(result["extra"]["confidence"], 0.92)

    def test_numeric_extra_values_unchanged(self) -> None:
        event = {"extra": {"retries": 3, "ok": True}}
        result = _sentry_before_send(event, hint=None)
        self.assertEqual(result["extra"]["retries"], 3)
        self.assertTrue(result["extra"]["ok"])


class TestSentryInitUsesIncludeLocalVariablesFalse(unittest.TestCase):
    """init_sentry passes include_local_variables=False and before_send to sentry_sdk.init."""

    def test_include_local_variables_false_passed(self) -> None:
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
            # Patch _sentry_initialized so init_sentry proceeds.
            import backend.observability as obs

            obs._sentry_initialized = False
            with patch.object(obs, "release_from_git", return_value="krab-ear@test"):
                obs.init_sentry("https://fake@sentry.io/1")

        call_kwargs = mock_sdk.init.call_args[1]
        self.assertFalse(
            call_kwargs.get("include_local_variables", True),
            "include_local_variables should be False",
        )

    def test_before_send_callback_passed(self) -> None:
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
            import backend.observability as obs

            obs._sentry_initialized = False
            with patch.object(obs, "release_from_git", return_value="krab-ear@test"):
                obs.init_sentry("https://fake@sentry.io/1")

        call_kwargs = mock_sdk.init.call_args[1]
        self.assertIn("before_send", call_kwargs)
        self.assertIs(call_kwargs["before_send"], obs._sentry_before_send)

    def test_send_default_pii_false_still_set(self) -> None:
        mock_sdk = MagicMock()
        with patch.dict(sys.modules, {"sentry_sdk": mock_sdk}):
            import backend.observability as obs

            obs._sentry_initialized = False
            with patch.object(obs, "release_from_git", return_value="krab-ear@test"):
                obs.init_sentry("https://fake@sentry.io/1")

        call_kwargs = mock_sdk.init.call_args[1]
        self.assertFalse(
            call_kwargs.get("send_default_pii", True),
            "send_default_pii should remain False",
        )


if __name__ == "__main__":
    unittest.main()
