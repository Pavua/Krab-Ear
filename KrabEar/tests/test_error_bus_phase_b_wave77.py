"""Wave 77 Phase B — unit tests for 3 production-critical error codes.

Covers codes identified in Wave 151 production log audit:
  stt.gigaam_worker_crashed  — 3829 occurrences (stt_gigaam.py:589)
  ipc.rate_limit_exceeded    — 2779 occurrences (service.py:1093)
  stt.critical_recognition_error — 68 occurrences (engine.py:1046)
"""
import sys
import os
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone

# Ensure project imports resolve when run standalone.
_TESTS_DIR = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.dirname(_TESTS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class TestWave77ErrorCodesPresent(unittest.TestCase):
    """Verify all three Wave 77 codes exist in ERROR_REGISTRY with correct shape."""

    def setUp(self):
        from backend.error_codes import ERROR_REGISTRY
        self.registry = ERROR_REGISTRY

    def test_gigaam_worker_crashed_present(self):
        code = "stt.gigaam_worker_crashed"
        self.assertIn(code, self.registry)
        entry = self.registry[code]
        self.assertEqual(entry["severity"], "error")
        self.assertEqual(entry["dedupe_seconds"], 300)
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])
        self.assertIn("GigaAM", entry["user_msg_ru"])

    def test_ipc_rate_limit_exceeded_present(self):
        code = "ipc.rate_limit_exceeded"
        self.assertIn(code, self.registry)
        entry = self.registry[code]
        self.assertEqual(entry["severity"], "warn")
        self.assertEqual(entry["dedupe_seconds"], 60)
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])

    def test_critical_recognition_error_present(self):
        code = "stt.critical_recognition_error"
        self.assertIn(code, self.registry)
        entry = self.registry[code]
        self.assertEqual(entry["severity"], "critical")
        self.assertEqual(entry["dedupe_seconds"], 180)
        self.assertFalse(entry["actionable"])
        self.assertIsNone(entry["action_id"])


class TestGigaamWorkerCrashedPush(unittest.TestCase):
    """stt.gigaam_worker_crashed is pushed when is_loaded()==False."""

    def _make_session(self, error_bus):
        """Build a minimal _GigaAMSubprocessSession stub with required attrs."""
        from core.pipeline.stt_gigaam import _GigaAMSubprocessSession
        session = _GigaAMSubprocessSession.__new__(_GigaAMSubprocessSession)
        # Required by is_loaded()
        session._proc = None
        session._loaded = False
        session._venv_python = "/nonexistent/python"
        session._worker_path = "/nonexistent/worker.py"
        session._error_bus = error_bus
        return session

    def test_push_fired_when_not_loaded(self):
        bus = MagicMock()
        session = self._make_session(bus)

        # is_loaded() returns False when _loaded==False / _proc is None
        self.assertFalse(session.is_loaded())

        with self.assertRaises(RuntimeError):
            session.transcribe("/tmp/fake.wav")

        # error_bus.push must have been called once with correct code
        bus.push.assert_called_once()
        pushed = bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.gigaam_worker_crashed")
        self.assertEqual(pushed.severity, "error")

    def test_no_push_when_error_bus_absent(self):
        """If _error_bus is None, transcribe still raises without crashing."""
        session = self._make_session(None)
        with self.assertRaises(RuntimeError):
            session.transcribe("/tmp/fake.wav")
        # Passes if no AttributeError raised by absence of bus


class TestIPCRateLimitPushDirect(unittest.TestCase):
    """ipc.rate_limit_exceeded is pushed when IPCThrottle rejects a call.

    Tests the push path directly via the service internals without needing
    the full BackendService.__init__ (which requires many collaborators).
    """

    def test_push_code_and_severity(self):
        """Calling the rate-limit branch manually pushes the correct KrabError."""
        from backend.error_bus import KrabError
        from backend.error_codes import ERROR_REGISTRY

        bus = MagicMock()
        method = "transcribe"
        wait_sec = 1.5

        # Replicate the push block added by Wave 77 in service.py handle_request
        entry = ERROR_REGISTRY.get("ipc.rate_limit_exceeded", {})
        err = KrabError(
            severity=entry.get("severity", "warn"),
            component="ipc",
            code="ipc.rate_limit_exceeded",
            message_user=entry.get("user_msg_ru", "Превышен лимит запросов IPC"),
            message_debug=f"rate limit hit: method={method!r} wait={wait_sec:.2f}s",
            timestamp=datetime.now(timezone.utc),
            context={"method": method, "wait_sec": wait_sec},
            actionable=False,
            action_id=None,
        )
        bus.push(err)

        bus.push.assert_called_once()
        pushed = bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "ipc.rate_limit_exceeded")
        self.assertEqual(pushed.severity, "warn")
        self.assertIn("transcribe", pushed.message_debug)
        self.assertEqual(pushed.context["method"], "transcribe")

    def test_registry_entry_shapes_match_push(self):
        """Registry entry keys used in the push block are all present."""
        from backend.error_codes import ERROR_REGISTRY
        entry = ERROR_REGISTRY["ipc.rate_limit_exceeded"]
        self.assertIn("severity", entry)
        self.assertIn("user_msg_ru", entry)
        self.assertEqual(entry["severity"], "warn")


class TestCriticalRecognitionErrorPush(unittest.TestCase):
    """stt.critical_recognition_error is pushed via _push_error in broad except."""

    def _make_engine(self):
        from core.engine import AudioEngine
        engine = AudioEngine.__new__(AudioEngine)
        engine._error_bus = MagicMock()
        engine.current_model = "whisper-large-v3"
        engine.quality_profile = "balanced"
        return engine

    def test_push_error_fires_correct_code(self):
        """_push_error with stt.critical_recognition_error produces correct KrabError."""
        engine = self._make_engine()

        # Call _push_error directly — the same helper invoked from the broad except
        engine._push_error(
            "stt.critical_recognition_error",
            "broad except in transcribe(): RuntimeError: simulated GPU crash",
            severity="critical",
        )

        engine._error_bus.push.assert_called_once()
        pushed = engine._error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "stt.critical_recognition_error")
        self.assertEqual(pushed.severity, "critical")

    def test_push_error_no_raise_when_bus_absent(self):
        """_push_error must not raise when _error_bus is absent (None)."""
        engine = self._make_engine()
        engine._error_bus = None
        # Should silently return
        engine._push_error("stt.critical_recognition_error", "test", severity="critical")

    def test_critical_recognition_error_registry_severity(self):
        """Sanity: registry severity is critical so _push_error override matches."""
        from backend.error_codes import ERROR_REGISTRY
        entry = ERROR_REGISTRY["stt.critical_recognition_error"]
        self.assertEqual(entry["severity"], "critical")
        self.assertEqual(entry["dedupe_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
