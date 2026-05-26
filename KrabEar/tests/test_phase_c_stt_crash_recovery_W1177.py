"""W1177 — unit tests for _stop_recording_phase_c STT crash recovery.

Covers:
  - test_phase_c_stt_crash_emits_error_event
  - test_phase_c_stt_crash_persists_audio_recovery
  - test_phase_c_stt_crash_returns_error_result_not_raise
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService
from backend.state_store import StateStore


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = True
    sample_rate = 16000

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        audio = (np.sin(2.0 * np.pi * 440.0 * np.linspace(0, 1, 16000, dtype=np.float32)) * 0.3)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _CrashingTranscriber:
    """Transcriber that always raises RuntimeError to simulate STT crash."""

    def transcribe(self, audio, **kwargs):
        raise RuntimeError("Simulated MLX GPU hang")


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text, status="skipped", source_lang="auto",
            target_lang="ru", mode="auto", engine="fake",
        )


class _FakeSettingsSvc:
    def cached_settings(self):
        return {}

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


class _FakeErrorBus:
    """Minimal ErrorBus stand-in that records pushed errors."""

    def __init__(self):
        self.pushed: list = []

    def push(self, error):
        self.pushed.append(error)


def _make_crashing_service(tmp_dir, error_bus=None):
    """Build a RecordingCoreService with a crashing transcriber."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None

    svc = RecordingCoreService(
        recorder=_FakeRecorder(),
        transcriber=_CrashingTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_FakeSettingsSvc(),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=session_tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )
    if error_bus is not None:
        svc._error_bus = error_bus
    return svc, store


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestPhaseCSTTCrashRecovery(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    # ------------------------------------------------------------------
    # Test 1: does NOT raise — returns error result instead
    # ------------------------------------------------------------------
    def test_phase_c_stt_crash_returns_error_result_not_raise(self):
        """handle_stop_recording must not propagate STT exceptions to caller."""
        svc, _ = _make_crashing_service(self._tmp)
        svc.handle_start_recording({})

        # Must not raise
        result = svc.handle_stop_recording({})

        self.assertIsInstance(result, dict, "result must be a dict")
        self.assertEqual(result.get("error"), "stt_failed",
                         "result['error'] must be 'stt_failed'")
        self.assertFalse(result.get("ok", True),
                         "result['ok'] must be False on STT crash")
        self.assertEqual(result.get("status"), "stt_failed",
                         "result['status'] must be 'stt_failed'")

    # ------------------------------------------------------------------
    # Test 2: audio recovery file is persisted to data_dir/failed_recordings/
    # ------------------------------------------------------------------
    def test_phase_c_stt_crash_persists_audio_recovery(self):
        """On STT crash, a WAV recovery file must be written under data_dir/failed_recordings/."""
        svc, store = _make_crashing_service(self._tmp)
        svc.handle_start_recording({})

        result = svc.handle_stop_recording({})

        # Check return value contains recovery path
        recovery_rel = result.get("audio_recovery_path")
        self.assertIsNotNone(recovery_rel, "audio_recovery_path must be set in result")

        # Check the file actually exists (relative to data_dir)
        recovery_abs = Path(store.data_dir) / recovery_rel
        self.assertTrue(recovery_abs.exists(),
                        f"Recovery WAV file not found at {recovery_abs}")
        self.assertTrue(recovery_abs.suffix == ".wav",
                        "Recovery file must be a .wav file")
        self.assertGreater(recovery_abs.stat().st_size, 0,
                           "Recovery WAV must not be empty")

        # Path must be under failed_recordings/
        self.assertIn("failed_recordings", str(recovery_rel),
                      "recovery path must be under failed_recordings/")

    # ------------------------------------------------------------------
    # Test 3: error event pushed to error_bus
    # ------------------------------------------------------------------
    def test_phase_c_stt_crash_emits_error_event(self):
        """On STT crash, KrabError with code='stt.transcribe_failed' must be pushed."""
        bus = _FakeErrorBus()
        svc, _ = _make_crashing_service(self._tmp, error_bus=bus)
        svc.handle_start_recording({})

        svc.handle_stop_recording({})

        self.assertEqual(len(bus.pushed), 1,
                         "Exactly one error must be pushed to the bus")
        err = bus.pushed[0]
        self.assertEqual(err.code, "stt.transcribe_failed",
                         "Error code must be 'stt.transcribe_failed'")
        self.assertEqual(err.severity, "error",
                         "Severity must be 'error'")
        self.assertEqual(err.component, "stt",
                         "Component must be 'stt'")
        self.assertIn("RuntimeError", err.message_debug,
                      "Debug message must contain the exception type")
        self.assertFalse(err.actionable)

    # ------------------------------------------------------------------
    # Test 4: no crash when error_bus is None (not wired)
    # ------------------------------------------------------------------
    def test_phase_c_stt_crash_without_error_bus_still_returns_error(self):
        """When _error_bus is None (not wired), the crash path must still work."""
        svc, _ = _make_crashing_service(self._tmp, error_bus=None)
        svc.handle_start_recording({})

        # Must not raise even without an error bus
        result = svc.handle_stop_recording({})
        self.assertEqual(result.get("error"), "stt_failed")

    # ------------------------------------------------------------------
    # Test 5: error code exists in ERROR_REGISTRY
    # ------------------------------------------------------------------
    def test_stt_transcribe_failed_in_error_registry(self):
        """stt.transcribe_failed must be present in ERROR_REGISTRY."""
        from backend.error_codes import ERROR_REGISTRY
        self.assertIn("stt.transcribe_failed", ERROR_REGISTRY,
                      "stt.transcribe_failed must be registered in ERROR_REGISTRY")
        entry = ERROR_REGISTRY["stt.transcribe_failed"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIn("user_msg_ru", entry)

    # ------------------------------------------------------------------
    # Test 6: happy path still works (non-crashing transcriber)
    # ------------------------------------------------------------------
    def test_phase_c_happy_path_unchanged(self):
        """Verify the normal (non-crash) path still returns transcription text."""
        from backend.state_store import StateStore

        class _GoodTranscriber:
            def transcribe(self, audio, **kwargs):
                return {"text": "привет мир", "confidence": 0.95, "engine": "fake"}

        store = StateStore(data_dir=Path(self._tmp))
        vocab = MagicMock()
        vocab.load.return_value = []
        session_tracker = MagicMock()
        session_tracker._active_session = None

        svc = RecordingCoreService(
            recorder=_FakeRecorder(),
            transcriber=_GoodTranscriber(),
            translator=_FakeTranslator(),
            store=store,
            vocabulary=vocab,
            settings_svc=_FakeSettingsSvc(),
            llm_rewriter=None,
            auto_glossary=None,
            semantic_searcher=_FakeSemanticSearcher(),
            context_memory=None,
            clipboard_history=[],
            auto_backup=MagicMock(),
            session_tracker=session_tracker,
            action_items_extractor=None,
            transcription_counter_ref=[0],
            last_stt_engine_ref=[None],
        )
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})

        # Should contain text (or status other than stt_failed)
        self.assertNotEqual(result.get("status"), "stt_failed",
                            "Normal path must not return stt_failed status")


if __name__ == "__main__":
    unittest.main()
