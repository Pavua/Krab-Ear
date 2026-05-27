"""Tests for W1352: AuditLogger wired into BackendService (W1351 F1 CRITICAL fix).

Three targeted tests:
  1. audit_logger is instantiated on BackendService (was dead module before this fix)
  2. handle_request calls audit_logger.log_request on every dispatch
  3. Exception in audit_logger.log_request does NOT break dispatch
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audit_logger import AuditLogger
from backend.service import BackendService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Minimal stubs so BackendService.__init__ can run without real hardware
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.is_recording = False
        return None

    def snapshot_audio(self):
        return None

    def on_audio_level(self, rms):
        pass


class _FakeTranscriber:
    engine = MagicMock()

    def transcribe(self, *a, **kw):
        return MagicMock(text="ok", language="en", confidence=0.9)


class _FakeTranslator:
    def translate(self, *a, **kw):
        from backend.translator import TranslationResult
        return TranslationResult(original="x", translated="y", mode="off")

    def cached_settings(self):
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(tmp: str) -> BackendService:
    store = StateStore(Path(tmp) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAuditLoggerInstantiatedInBackend(unittest.TestCase):
    """W1351 F1: BackendService must have a live _audit_logger after __init__."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(self._tmp.name)

    def test_audit_logger_attribute_exists(self):
        """_audit_logger attribute must be present on BackendService instance."""
        self.assertTrue(
            hasattr(self.service, "_audit_logger"),
            "_audit_logger not found on BackendService — module still dead!",
        )

    def test_audit_logger_is_audit_logger_instance(self):
        """_audit_logger must be an AuditLogger (not None, not a stub)."""
        audit = self.service._audit_logger
        self.assertIsNotNone(audit, "_audit_logger is None — not instantiated")
        self.assertIsInstance(
            audit,
            AuditLogger,
            f"Expected AuditLogger, got {type(audit).__name__}",
        )

    def test_audit_logger_data_dir_matches_store(self):
        """AuditLogger data_dir must match service's store.data_dir."""
        audit_dir = Path(self.service._audit_logger._data_dir)
        store_dir = Path(self.service.store.data_dir)
        self.assertEqual(
            audit_dir,
            store_dir,
            f"AuditLogger data_dir {audit_dir!r} != store.data_dir {store_dir!r}",
        )


class TestHandleRequestCallsAuditLog(unittest.TestCase):
    """handle_request must call _audit_logger.log_request after every successful dispatch."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(self._tmp.name)
        # Replace the real audit logger with a mock to spy on calls
        self.mock_audit = MagicMock(spec=AuditLogger)
        self.service._audit_logger = self.mock_audit

    def test_log_request_called_on_successful_dispatch(self):
        """audit_logger.log_request must be called once for a successful method call."""
        resp = self.service.handle_request({"id": "1", "method": "ping", "params": {}})
        self.assertEqual(resp.get("ok"), True, f"ping should succeed, got: {resp}")
        self.mock_audit.log_request.assert_called_once()
        call_kwargs = self.mock_audit.log_request.call_args
        # Check the keyword arguments match expected signature
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else {}
        # log_request may be called positionally or with kwargs
        all_args = list(call_kwargs.args) + list(kwargs.values())
        # method name should be 'ping'
        self.assertIn("ping", all_args)

    def test_log_request_called_with_correct_method(self):
        """audit_logger.log_request should receive the dispatched method name."""
        self.service.handle_request({"id": "1", "method": "get_settings", "params": {}})
        call_args = self.mock_audit.log_request.call_args
        # Called as log_request(method=..., params=..., result=..., duration_ms=...)
        kwargs = call_args.kwargs
        self.assertEqual(kwargs.get("method"), "get_settings")

    def test_log_request_called_on_unknown_method(self):
        """audit_logger.log_request is NOT expected to be called for unknown methods
        (handle_request returns error before reaching dispatch)."""
        resp = self.service.handle_request({"id": "1", "method": "nonexistent_xyz", "params": {}})
        # Unknown method: error returned before handler table lookup completes
        self.assertFalse(resp.get("ok"), "Unknown method should return ok=False")
        # The audit call should NOT fire before the handler is resolved
        self.mock_audit.log_request.assert_not_called()

    def test_duration_ms_positive(self):
        """duration_ms passed to log_request must be a non-negative float."""
        self.service.handle_request({"id": "1", "method": "ping", "params": {}})
        call_kwargs = self.mock_audit.log_request.call_args.kwargs
        duration = call_kwargs.get("duration_ms", -1)
        self.assertGreaterEqual(duration, 0, "duration_ms must be >= 0")


class TestAuditLoggerExceptionDoesNotBreakDispatch(unittest.TestCase):
    """If audit_logger.log_request raises, the response must still be returned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.service = _make_service(self._tmp.name)
        # Make audit_logger raise on every call
        broken_audit = MagicMock(spec=AuditLogger)
        broken_audit.log_request.side_effect = RuntimeError("simulated audit failure")
        self.service._audit_logger = broken_audit

    def test_dispatch_succeeds_despite_audit_exception(self):
        """handle_request must return ok=True for ping even if audit raises."""
        resp = self.service.handle_request({"id": "1", "method": "ping", "params": {}})
        self.assertEqual(
            resp.get("ok"),
            True,
            f"Dispatch must survive audit exception — got: {resp}",
        )

    def test_dispatch_returns_correct_result_despite_audit_exception(self):
        """Result payload must be intact when audit logger raises."""
        resp = self.service.handle_request({"id": "1", "method": "get_settings", "params": {}})
        self.assertEqual(resp.get("ok"), True, f"get_settings must succeed — got: {resp}")
        self.assertIn("result", resp, "Response must contain 'result' key")

    def test_audit_none_does_not_crash(self):
        """Setting _audit_logger=None must not crash handle_request (None guard)."""
        self.service._audit_logger = None
        resp = self.service.handle_request({"id": "1", "method": "ping", "params": {}})
        self.assertEqual(resp.get("ok"), True, f"ping with None audit_logger — got: {resp}")


if __name__ == "__main__":
    unittest.main()
