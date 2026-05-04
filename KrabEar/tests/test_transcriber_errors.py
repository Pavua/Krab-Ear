"""Tests for paste/diarization error pushes (Phase B.1 Task 9)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.service import BackendService
from backend.translator import TranslationResult


# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_error_bus_integration.py)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    def __init__(self) -> None:
        self.is_recording = False
        self.sample_rate = 16000

    def start(self) -> bool:
        self.is_recording = True
        return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0):
        self.is_recording = False
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_audio(self, max_duration_sec: float = 12.0):
        return np.ones(16000, dtype=np.float32), 1.0


class _FakeTranscriber:
    def __init__(self) -> None:
        self.counter = 0

    def transcribe(self, audio_data, quality_profile: str = "balanced",
                   cleanup_profile: str = "soft", domain: str = "casual",
                   extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None) -> str:
        self.counter += 1
        return f"test #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile: str = "balanced") -> str:
        return "preview"


class _FakeTranslator:
    def translate(self, text: str, mode: str, network_mode: str,
                  translation_style: str = "neutral",
                  glossary: dict | None = None) -> TranslationResult:
        return TranslationResult(
            text="", status="not_requested",
            source_lang="", target_lang="", mode="off", engine="fake",
        )


# ---------------------------------------------------------------------------
# Test cases: report_paste_failure IPC handler
# ---------------------------------------------------------------------------

class ReportPasteFailureTests(unittest.TestCase):
    """IPC handler report_paste_failure pushes KrabError to error_bus."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = BackendService(
            store=StateStore(Path(self.tmp.name) / "data"),
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            translator=_FakeTranslator(),
        )
        self.addCleanup(self.service.close)

    def _call(self, method: str, params: dict | None = None) -> dict:
        return self.service.handle_request(
            {"id": "t", "method": method, "params": params or {}}
        )

    def test_ax_denied_pushes_paste_ax_denied(self) -> None:
        resp = self._call("report_paste_failure", {"reason": "ax_denied", "app_bundle": "com.test"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["code"], "paste.ax_denied")
        # Verify the error appears in recent errors ring buffer
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("paste.ax_denied", codes)

    def test_app_unsupported_pushes_paste_app_unsupported(self) -> None:
        resp = self._call("report_paste_failure", {"reason": "app_unsupported", "app_bundle": "com.x"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["code"], "paste.app_unsupported")
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        codes = [e["code"] for e in errors]
        self.assertIn("paste.app_unsupported", codes)

    def test_unknown_reason_returns_error(self) -> None:
        resp = self._call("report_paste_failure", {"reason": "weird_reason"})
        # Handler returns {"ok": True, "result": {"ok": False, "reason": "unknown_paste_reason"}}
        text = str(resp)
        self.assertIn("unknown", text.lower())

    def test_error_has_correct_severity(self) -> None:
        self._call("report_paste_failure", {"reason": "ax_denied", "app_bundle": "com.example"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        ax_errors = [e for e in errors if e["code"] == "paste.ax_denied"]
        self.assertTrue(ax_errors, "paste.ax_denied should be in ring buffer")
        self.assertEqual(ax_errors[0]["severity"], "error")

    def test_app_bundle_stored_in_context(self) -> None:
        self._call("report_paste_failure", {"reason": "ax_denied", "app_bundle": "com.apple.Notes"})
        errors = self._call("list_recent_errors", {})["result"]["errors"]
        ax_errors = [e for e in errors if e["code"] == "paste.ax_denied"]
        self.assertTrue(ax_errors)
        self.assertEqual(ax_errors[0]["context"].get("app_bundle"), "com.apple.Notes")


# ---------------------------------------------------------------------------
# Test cases: Transcriber._push_diarization_no_token_if_needed helper
# ---------------------------------------------------------------------------

class DiarizationNoTokenTests(unittest.TestCase):
    """Direct tests of Transcriber._push_diarization_no_token_if_needed."""

    def _make_transcriber(self) -> object:
        from backend.transcriber import Transcriber
        t = Transcriber.__new__(Transcriber)  # bypass __init__ (no AudioEngine required)
        t._error_bus = MagicMock()
        return t

    def test_diarization_off_returns_true_no_push(self) -> None:
        t = self._make_transcriber()
        result = t._push_diarization_no_token_if_needed({"diarization_enabled": False})
        self.assertTrue(result)
        t._error_bus.push.assert_not_called()

    def test_diarization_off_missing_key_returns_true(self) -> None:
        """Default for diarization_enabled is False, so missing key = off."""
        t = self._make_transcriber()
        result = t._push_diarization_no_token_if_needed({})
        self.assertTrue(result)
        t._error_bus.push.assert_not_called()

    def test_token_present_hf_token_returns_true(self) -> None:
        t = self._make_transcriber()
        os.environ["HF_TOKEN"] = "hf_test_token_abc"
        try:
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
            self.assertTrue(result)
            t._error_bus.push.assert_not_called()
        finally:
            os.environ.pop("HF_TOKEN", None)

    def test_token_present_krab_ear_hf_token_returns_true(self) -> None:
        t = self._make_transcriber()
        os.environ.pop("HF_TOKEN", None)
        os.environ["KRAB_EAR_HF_TOKEN"] = "hf_krab_ear_token"
        try:
            result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
            self.assertTrue(result)
            t._error_bus.push.assert_not_called()
        finally:
            os.environ.pop("KRAB_EAR_HF_TOKEN", None)

    def test_no_token_pushes_diarization_no_token(self) -> None:
        t = self._make_transcriber()
        for k in ("HF_TOKEN", "KRAB_EAR_HF_TOKEN"):
            os.environ.pop(k, None)
        result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertFalse(result)
        self.assertEqual(t._error_bus.push.call_count, 1)
        pushed_err = t._error_bus.push.call_args[0][0]
        self.assertEqual(pushed_err.code, "diarization.no_token")
        self.assertEqual(pushed_err.component, "diarization")
        self.assertEqual(pushed_err.severity, "warn")

    def test_no_token_no_error_bus_returns_false_gracefully(self) -> None:
        """When _error_bus is not injected yet, still returns False without crash."""
        from backend.transcriber import Transcriber
        t = Transcriber.__new__(Transcriber)
        # Deliberately do NOT set _error_bus
        for k in ("HF_TOKEN", "KRAB_EAR_HF_TOKEN"):
            os.environ.pop(k, None)
        result = t._push_diarization_no_token_if_needed({"diarization_enabled": True})
        self.assertFalse(result)

    def test_error_bus_injected_by_backend_service(self) -> None:
        """BackendService.__init__ wires _error_bus onto the Transcriber instance."""
        tmp = tempfile.mkdtemp()
        fake_transcriber = _FakeTranscriber()
        service = BackendService(
            store=StateStore(Path(tmp) / "data"),
            recorder=_FakeRecorder(),
            transcriber=fake_transcriber,
            translator=_FakeTranslator(),
        )
        try:
            # The fake transcriber should have _error_bus wired
            self.assertTrue(
                hasattr(fake_transcriber, "_error_bus"),
                "_error_bus should be injected by BackendService.__init__",
            )
            self.assertIs(fake_transcriber._error_bus, service._error_bus)
        finally:
            service.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
