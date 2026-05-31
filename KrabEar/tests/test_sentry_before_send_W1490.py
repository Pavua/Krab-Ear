"""Tests for W1483 F1+F2 fixes in backend/observability.py.

Covers:
  - _sentry_before_send redacts breadcrumb data dicts (F1)
  - _sentry_before_send redacts breadcrumb message strings (F1)
  - _sentry_before_send redacts logentry.message and logentry.params (F2)
  - _sentry_before_send redacts request.data, request.query_string, request.cookies (F2)
"""

import sys
import os
import types
import unittest
from unittest.mock import MagicMock

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
)

# ---------------------------------------------------------------------------
# Shared test paths for assertions
# ---------------------------------------------------------------------------
_ABS_HOME_PATH = "/Users/pablito/Library/Logs/KrabEar/backend.log"
_TRANSCRIPT_PATH = "/Users/pablito/Library/Application Support/KrabEar/transcripts/2026-05-26.md"


class TestBeforeSendRedactsBreadcrumbDataPaths(unittest.TestCase):
    """W1483 F1: _sentry_before_send walks breadcrumbs[].data for path redaction."""

    def test_before_send_redacts_breadcrumb_data_paths(self) -> None:
        """Absolute /Users/... path in breadcrumb data.path is collapsed to ~/..."""
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "type": "default",
                        "category": "ipc",
                        "message": "start_recording",
                        "data": {
                            "path": _ABS_HOME_PATH,
                            "duration_ms": 1234,
                        },
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        crumb = result["breadcrumbs"]["values"][0]
        self.assertNotIn("/Users/pablito", crumb["data"]["path"])
        self.assertTrue(
            crumb["data"]["path"].startswith("~/"),
            f"Expected ~/... but got {crumb['data']['path']!r}",
        )
        # Non-string values must pass through unchanged.
        self.assertEqual(crumb["data"]["duration_ms"], 1234)

    def test_before_send_redacts_transcript_path_in_breadcrumb_data(self) -> None:
        """KrabEar/transcripts/... path in breadcrumb data is replaced with redacted marker."""
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "category": "file",
                        "data": {"output_path": _TRANSCRIPT_PATH},
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        crumb_data = result["breadcrumbs"]["values"][0]["data"]
        self.assertEqual(crumb_data["output_path"], _TRANSCRIPT_REDACTED)

    def test_before_send_breadcrumb_data_nested_list_redacted(self) -> None:
        """Nested list of paths inside breadcrumb data is also walked."""
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "data": {
                            "paths": [
                                "/Users/alice/project/file.py",
                                "safe_value",
                            ]
                        }
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        paths = result["breadcrumbs"]["values"][0]["data"]["paths"]
        self.assertNotIn("/Users/alice", paths[0])
        self.assertTrue(paths[0].startswith("~/"))
        self.assertEqual(paths[1], "safe_value")

    def test_before_send_multiple_breadcrumbs_all_redacted(self) -> None:
        """All crumbs in the values list are walked, not just the first."""
        event = {
            "breadcrumbs": {
                "values": [
                    {"data": {"p": "/Users/a/file.py"}},
                    {"data": {"p": "/Users/b/file.py"}},
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        for crumb in result["breadcrumbs"]["values"]:
            self.assertNotIn("/Users/", crumb["data"]["p"])
            self.assertTrue(crumb["data"]["p"].startswith("~/"))

    def test_before_send_missing_breadcrumbs_no_crash(self) -> None:
        """Event with no breadcrumbs key must not crash."""
        event = {"message": "hello"}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)

    def test_before_send_empty_breadcrumb_values_no_crash(self) -> None:
        """Event with empty breadcrumbs.values list must not crash."""
        event = {"breadcrumbs": {"values": []}}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)


class TestBeforeSendRedactsBreadcrumbMessage(unittest.TestCase):
    """W1483 F1: _sentry_before_send walks breadcrumbs[].message string."""

    def test_before_send_redacts_breadcrumb_message(self) -> None:
        """Absolute path in breadcrumb message is collapsed to ~/..."""
        event = {
            "breadcrumbs": {
                "values": [
                    {
                        "message": f"Writing file to {_ABS_HOME_PATH}",
                        "data": {},
                    }
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        crumb_msg = result["breadcrumbs"]["values"][0]["message"]
        self.assertNotIn("/Users/pablito", crumb_msg)
        self.assertIn("~/Library", crumb_msg)

    def test_before_send_redacts_transcript_path_in_breadcrumb_message(self) -> None:
        """Transcript path in breadcrumb message is replaced with redacted marker."""
        event = {
            "breadcrumbs": {
                "values": [
                    {"message": f"Failed writing {_TRANSCRIPT_PATH}"}
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        crumb_msg = result["breadcrumbs"]["values"][0]["message"]
        self.assertNotIn("transcripts", crumb_msg)
        self.assertIn(_TRANSCRIPT_REDACTED, crumb_msg)

    def test_before_send_breadcrumb_message_without_path_unchanged(self) -> None:
        """Non-path breadcrumb message strings are preserved as-is."""
        event = {
            "breadcrumbs": {
                "values": [
                    {"message": "start_recording"}
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertEqual(result["breadcrumbs"]["values"][0]["message"], "start_recording")

    def test_before_send_breadcrumb_without_message_key_no_crash(self) -> None:
        """Crumb without a 'message' key must not crash."""
        event = {
            "breadcrumbs": {
                "values": [
                    {"category": "ipc", "data": {"ok": True}}
                ]
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)


class TestBeforeSendRedactsLogentryMessage(unittest.TestCase):
    """W1483 F2: _sentry_before_send walks logentry.message and logentry.params."""

    def test_before_send_redacts_logentry_message(self) -> None:
        """Absolute path in logentry.message is collapsed to ~/..."""
        event = {
            "logentry": {
                "message": f"Error reading {_ABS_HOME_PATH}",
                "params": [],
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("/Users/pablito", result["logentry"]["message"])
        self.assertIn("~/Library", result["logentry"]["message"])

    def test_before_send_redacts_logentry_params(self) -> None:
        """Absolute path in logentry.params list is collapsed to ~/..."""
        event = {
            "logentry": {
                "message": "Error at %s",
                "params": [_ABS_HOME_PATH, "extra_info"],
            }
        }
        result = _sentry_before_send(event, hint=None)
        params = result["logentry"]["params"]
        self.assertNotIn("/Users/pablito", params[0])
        self.assertTrue(params[0].startswith("~/"))
        self.assertEqual(params[1], "extra_info")

    def test_before_send_redacts_transcript_path_in_logentry(self) -> None:
        """Transcript path in logentry.message is replaced with redacted marker."""
        event = {
            "logentry": {
                "message": f"Wrote transcript to {_TRANSCRIPT_PATH}",
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("transcripts", result["logentry"]["message"])
        self.assertIn(_TRANSCRIPT_REDACTED, result["logentry"]["message"])

    def test_before_send_missing_logentry_no_crash(self) -> None:
        """Event without logentry key must not crash."""
        event = {"message": "no logentry"}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)

    def test_before_send_logentry_without_params_no_crash(self) -> None:
        """logentry without 'params' key must not crash."""
        event = {"logentry": {"message": "plain log message"}}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)
        self.assertEqual(result["logentry"]["message"], "plain log message")

    def test_before_send_logentry_params_dict_redacted(self) -> None:
        """logentry.params as a dict (not a list) is also walked recursively."""
        event = {
            "logentry": {
                "message": "msg",
                "params": {"file": _ABS_HOME_PATH, "count": 5},
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertNotIn("/Users/pablito", result["logentry"]["params"]["file"])
        self.assertEqual(result["logentry"]["params"]["count"], 5)


class TestBeforeSendRedactsRequestData(unittest.TestCase):
    """W1483 F2: _sentry_before_send walks request.data, query_string, cookies."""

    def test_before_send_redacts_request_data(self) -> None:
        """Absolute path in request.data dict is collapsed to ~/..."""
        event = {
            "request": {
                "method": "POST",
                "url": "http://localhost:5005/transcribe",
                "data": {"audio_path": _ABS_HOME_PATH, "language": "ru"},
            }
        }
        result = _sentry_before_send(event, hint=None)
        req_data = result["request"]["data"]
        self.assertNotIn("/Users/pablito", req_data["audio_path"])
        self.assertTrue(req_data["audio_path"].startswith("~/"))
        self.assertEqual(req_data["language"], "ru")

    def test_before_send_redacts_request_query_string(self) -> None:
        """Absolute path in request.query_string string is collapsed to ~/..."""
        event = {
            "request": {
                "query_string": f"path={_ABS_HOME_PATH}&fmt=json",
            }
        }
        result = _sentry_before_send(event, hint=None)
        qs = result["request"]["query_string"]
        self.assertNotIn("/Users/pablito", qs)
        self.assertIn("~/Library", qs)

    def test_before_send_redacts_request_cookies(self) -> None:
        """Absolute path in request.cookies dict is collapsed to ~/..."""
        event = {
            "request": {
                "cookies": {"session_path": _ABS_HOME_PATH, "sid": "abc123"},
            }
        }
        result = _sentry_before_send(event, hint=None)
        cookies = result["request"]["cookies"]
        self.assertNotIn("/Users/pablito", cookies["session_path"])
        self.assertTrue(cookies["session_path"].startswith("~/"))
        self.assertEqual(cookies["sid"], "abc123")

    def test_before_send_redacts_transcript_path_in_request_data(self) -> None:
        """Transcript path in request.data is replaced with redacted marker."""
        event = {
            "request": {
                "data": {"output": _TRANSCRIPT_PATH},
            }
        }
        result = _sentry_before_send(event, hint=None)
        self.assertEqual(result["request"]["data"]["output"], _TRANSCRIPT_REDACTED)

    def test_before_send_missing_request_no_crash(self) -> None:
        """Event without request key must not crash."""
        event = {"message": "no request"}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)

    def test_before_send_request_safe_values_unchanged(self) -> None:
        """Non-path request fields pass through unchanged."""
        event = {
            "request": {
                "method": "GET",
                "url": "http://localhost:5005/health",
                "data": {},
                "query_string": "fmt=json",
                "cookies": {},
            }
        }
        result = _sentry_before_send(event, hint=None)
        req = result["request"]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["url"], "http://localhost:5005/health")
        self.assertEqual(req["query_string"], "fmt=json")

    def test_before_send_request_without_data_no_crash(self) -> None:
        """request with only method/url (no data/query_string/cookies) must not crash."""
        event = {"request": {"method": "GET", "url": "http://localhost:5005/"}}
        result = _sentry_before_send(event, hint=None)
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
