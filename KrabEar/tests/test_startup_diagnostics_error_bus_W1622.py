"""W1622 — tests verifying StartupDiagnostics._error_bus is injected in BackendService.

W1615 F1 HIGH: BackendService.__init__ constructed StartupDiagnostics but never
assigned self._error_bus into it, causing _push_stt_cache_miss_error to silently
return on every invocation.  This file verifies the fix.
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

from backend.state_store import StateStore
from backend.service import BackendService


# ---------------------------------------------------------------------------
# Minimal stubs (same as BackendServiceInitTestCase pattern)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16_000

    def start(self) -> bool:
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        return None

    def snapshot_audio(self) -> bytes:
        return b""


class _FakeTranscriber:
    def transcribe(self, audio, language=None, initial_prompt=None):
        return "test", 0.9, []

    def get_profile(self):
        return "balanced"

    def set_profile(self, profile: str) -> None:
        pass

    def get_vocabulary(self):
        return []

    def set_vocabulary(self, words) -> None:
        pass


class _FakeTranslator:
    def translate(self, text, source_language=None, target_language=None):
        from backend.translator import TranslationResult
        return TranslationResult(translated_text=text, source_language="ru", target_language="es")


def _make_service(tmp_dir: str) -> BackendService:
    store = StateStore(Path(tmp_dir) / "data")
    return BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )


# ===========================================================================
# Tests
# ===========================================================================

class TestStartupDiagnosticsErrorBusInjectedInBackendService(unittest.TestCase):
    """W1622: BackendService must wire _error_bus into _startup_diagnostics."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = _make_service(self.tmp.name)

    def test_startup_diagnostics_error_bus_injected_in_backend_service(self):
        """After __init__, _startup_diagnostics._error_bus must not be None."""
        svc = self.service
        self.assertTrue(
            hasattr(svc, "_startup_diagnostics"),
            "_startup_diagnostics attribute must exist on BackendService",
        )
        self.assertTrue(
            hasattr(svc, "_error_bus"),
            "_error_bus attribute must exist on BackendService",
        )
        self.assertIsNotNone(
            svc._startup_diagnostics._error_bus,
            "W1622: _startup_diagnostics._error_bus must be wired — was None, "
            "meaning _push_stt_cache_miss_error silently returned on every call",
        )

    def test_startup_diagnostics_error_bus_is_same_instance(self):
        """_startup_diagnostics._error_bus must be the exact same ErrorBus instance."""
        svc = self.service
        self.assertIs(
            svc._startup_diagnostics._error_bus,
            svc._error_bus,
            "W1622: _startup_diagnostics._error_bus must be the same object as "
            "BackendService._error_bus — not a copy or a different bus",
        )


class TestSttCacheMissErrorActuallyPushesToBusAfterWiring(unittest.TestCase):
    """W1622: after injection, _push_stt_cache_miss_error must call error_bus.push()."""

    def test_stt_cache_miss_error_actually_pushes_to_bus_after_wiring(self):
        """Verify push() is called when error_bus is properly injected."""
        from backend.startup_diagnostics import StartupDiagnostics

        with tempfile.TemporaryDirectory() as tmp:
            diag = StartupDiagnostics(data_dir=tmp)

            # Before injection: push must NOT be called
            mock_bus_pre = MagicMock()
            # _error_bus is None by default — confirm early-return still present
            # (we don't assign, just call directly)
            diag._push_stt_cache_miss_error("whisper-large-v3")
            # No error should happen — it should silently return

            # After injection: push MUST be called
            mock_bus = MagicMock()
            diag._error_bus = mock_bus
            diag._push_stt_cache_miss_error("whisper-large-v3")
            mock_bus.push.assert_called_once()
            pushed_err = mock_bus.push.call_args[0][0]
            # The pushed error should reference the model name
            self.assertIn(
                "whisper-large-v3",
                str(pushed_err),
                "Pushed KrabError should reference the model name",
            )


if __name__ == "__main__":
    unittest.main()
