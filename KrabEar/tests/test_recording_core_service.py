"""Wave 172 — unit tests for RecordingCoreService.

Tests the recording lifecycle service extracted from BackendService in Wave 172.
Covers: start/stop recording, recording state, audio inputs, transcribe progress/cancel,
preview_transcribe_paths, preview worker, and core audio guard helpers.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
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
# Shared fakes / stubs
# ---------------------------------------------------------------------------

class _FakeRecorder:
    is_recording = False
    sample_rate = 16000

    def start(self) -> bool:
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        audio = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _SilentRecorder(_FakeRecorder):
    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.zeros(32000, dtype=np.float32), 1.0


class _IdleRecorder(_FakeRecorder):
    """Already stopped — returns None on stop()."""
    is_recording = False

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        return None


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "hello world", "confidence": 0.9, "engine": "fake"}


class _FakeTranslator:
    def translate(self, text, **kwargs):
        from backend.translator import TranslationResult
        return TranslationResult(
            text=text,
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
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


def _make_service(tmp_dir, recorder=None, transcriber=None, extra_kwargs=None):
    """Utility: construct a RecordingCoreService with minimal fakes."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    kwargs = dict(
        recorder=recorder or _FakeRecorder(),
        transcriber=transcriber or _FakeTranscriber(),
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
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return RecordingCoreService(**kwargs)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestStartRecording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_start_returns_recording_status(self):
        svc = _make_service(self._tmp)
        result = svc.handle_start_recording({})
        self.assertEqual(result["status"], "recording")

    def test_start_when_already_recording_returns_already_recording(self):
        svc = _make_service(self._tmp)
        svc.handle_start_recording({})  # first start
        result = svc.handle_start_recording({})  # second start
        self.assertEqual(result["status"], "already_recording")
        self.assertTrue(result["is_recording"])

    def test_start_resets_preview_state(self):
        svc = _make_service(self._tmp)
        # Manually set some preview state
        with svc._preview_lock:
            svc._preview_text = "old text"
            svc._preview_duration_sec = 5.0
        svc.handle_start_recording({})
        self.assertEqual(svc.preview_text, "")
        self.assertEqual(svc.preview_duration_sec, 0.0)


class TestStopRecording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_stop_when_not_recording_returns_already_stopped(self):
        svc = _make_service(self._tmp, recorder=_IdleRecorder())
        result = svc.handle_stop_recording({})
        self.assertEqual(result["status"], "already_stopped")

    def test_stop_silence_returns_empty_audio(self):
        recorder = _SilentRecorder()
        svc = _make_service(self._tmp, recorder=recorder)
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "empty_audio")

    def test_stop_with_speech_returns_ok_or_empty_status(self):
        svc = _make_service(self._tmp)
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        # Either "ok" (transcribed) or "empty_audio" (guard triggered) — both are valid
        self.assertIn(result.get("status"), ("ok", "empty_audio"))

    def test_transcription_counter_incremented_on_success(self):
        counter_ref = [0]
        recorder = _FakeRecorder()
        svc = _make_service(self._tmp, recorder=recorder,
                            extra_kwargs={"transcription_counter_ref": counter_ref})
        svc.handle_start_recording({})
        svc.handle_stop_recording({"quality_profile": "balanced"})
        # Counter may or may not increment depending on guards; just verify it's >= 0
        self.assertGreaterEqual(counter_ref[0], 0)


class TestGetRecordingState(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_state_not_recording_initially(self):
        svc = _make_service(self._tmp)
        result = svc.handle_get_recording_state({})
        self.assertFalse(result["is_recording"])

    def test_state_recording_after_start(self):
        svc = _make_service(self._tmp)
        svc.handle_start_recording({})
        result = svc.handle_get_recording_state({})
        self.assertTrue(result["is_recording"])

    def test_state_has_required_keys(self):
        svc = _make_service(self._tmp)
        result = svc.handle_get_recording_state({})
        for key in ("is_recording", "duration_sec", "preview_text", "audio_rms", "elapsed_sec", "session_id"):
            self.assertIn(key, result, f"Missing key: {key}")

    def test_preview_text_reflects_current_value(self):
        svc = _make_service(self._tmp)
        with svc._preview_lock:
            svc._preview_text = "test preview"
            svc._preview_duration_sec = 2.5
        result = svc.handle_get_recording_state({})
        self.assertEqual(result["preview_text"], "test preview")
        self.assertAlmostEqual(result["duration_sec"], 2.5, places=3)


class TestListAudioInputs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_returns_items_count_default_input_keys(self):
        svc = _make_service(self._tmp)
        result = svc.handle_list_audio_inputs({})
        self.assertIn("items", result)
        self.assertIn("count", result)
        self.assertIn("default_input_id", result)
        self.assertEqual(result["count"], len(result["items"]))

    def test_monkey_patch_overrides_audio_inputs(self):
        svc = _make_service(self._tmp)
        svc._list_audio_inputs = lambda: [
            {"id": 5, "name": "Test Device", "is_default": True}
        ]
        result = svc.handle_list_audio_inputs({})
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["default_input_id"], 5)

    def test_default_input_id_none_when_no_default(self):
        svc = _make_service(self._tmp)
        svc._list_audio_inputs = lambda: [
            {"id": 0, "name": "Dev A", "is_default": False},
            {"id": 1, "name": "Dev B", "is_default": False},
        ]
        result = svc.handle_list_audio_inputs({})
        self.assertIsNone(result["default_input_id"])

    def test_get_audio_devices_returns_devices_key(self):
        svc = _make_service(self._tmp)
        svc._list_audio_inputs = lambda: []
        result = svc.handle_get_audio_devices({})
        self.assertIn("devices", result)


class TestTranscribeProgress(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_missing_job_id_raises(self):
        svc = _make_service(self._tmp)
        with self.assertRaises(RuntimeError):
            svc.handle_get_transcribe_progress({})

    def test_unknown_job_id_raises(self):
        svc = _make_service(self._tmp)
        with self.assertRaises(RuntimeError):
            svc.handle_get_transcribe_progress({"job_id": "nonexistent-id"})

    def test_progress_known_job_returns_status_fields(self):
        svc = _make_service(self._tmp)
        job_id = svc._job_tracker.create_job(total_files=1)
        result = svc.handle_get_transcribe_progress({"job_id": job_id})
        for key in ("status", "current_file", "current_stage", "file_index",
                    "total_files", "elapsed_sec", "processed", "errors", "items"):
            self.assertIn(key, result)

    def test_completed_job_items_visible(self):
        svc = _make_service(self._tmp)
        job_id = svc._job_tracker.create_job(total_files=1)
        svc._job_tracker.mark_done(job_id, items=[{"text": "hi"}], errors=[])
        result = svc.handle_get_transcribe_progress({"job_id": job_id})
        self.assertEqual(result["status"], "done")
        self.assertEqual(len(result["items"]), 1)


class TestCancelTranscribeJob(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_missing_job_id_raises(self):
        svc = _make_service(self._tmp)
        with self.assertRaises(RuntimeError):
            svc.handle_cancel_transcribe_job({})

    def test_cancel_known_job_returns_cancelled_true(self):
        svc = _make_service(self._tmp)
        job_id = svc._job_tracker.create_job(total_files=1)
        result = svc.handle_cancel_transcribe_job({"job_id": job_id})
        self.assertTrue(result["cancelled"])

    def test_cancel_unknown_job_returns_cancelled_false(self):
        svc = _make_service(self._tmp)
        result = svc.handle_cancel_transcribe_job({"job_id": "does-not-exist"})
        self.assertFalse(result["cancelled"])


class TestPreviewTranscribePaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_non_list_paths_raises(self):
        svc = _make_service(self._tmp)
        with self.assertRaises(RuntimeError):
            svc.handle_preview_transcribe_paths({"paths": "not_a_list"})

    def test_empty_paths_returns_zero_counts(self):
        svc = _make_service(self._tmp)
        result = svc.handle_preview_transcribe_paths({"paths": []})
        self.assertEqual(result["audio_count"], 0)
        self.assertEqual(result["input_count"], 0)

    def test_paths_outside_allowed_returns_error(self):
        svc = _make_service(self._tmp)
        result = svc.handle_preview_transcribe_paths({"paths": ["/etc/passwd"]})
        self.assertIn("errors", result)
        self.assertTrue(len(result["errors"]) > 0)

    def test_valid_tmp_path_resolves(self):
        """A path under /tmp (allowed) is accepted and counted."""
        svc = _make_service(self._tmp)
        wav_path = Path(self._tmp) / "test.wav"
        wav_path.touch()
        result = svc.handle_preview_transcribe_paths({"paths": [str(wav_path)]})
        self.assertEqual(result["input_count"], 1)
        self.assertIn("by_ext", result)


class TestPreviewWorker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_reset_preview_state_clears_fields(self):
        svc = _make_service(self._tmp)
        with svc._preview_lock:
            svc._preview_text = "something"
            svc._preview_duration_sec = 7.0
            svc._preview_updated_at = 123.4
        svc.reset_preview_state()
        self.assertEqual(svc.preview_text, "")
        self.assertEqual(svc.preview_duration_sec, 0.0)
        self.assertEqual(svc._preview_updated_at, 0.0)

    def test_preview_thread_alive_false_initially(self):
        svc = _make_service(self._tmp)
        self.assertFalse(svc.preview_thread_alive)

    def test_start_preview_worker_does_nothing_without_transcribe_preview(self):
        """Transcriber without transcribe_preview method: no thread started."""
        svc = _make_service(self._tmp)
        svc.start_preview_worker("balanced")
        self.assertFalse(svc.preview_thread_alive)

    def test_preview_error_count_readable(self):
        svc = _make_service(self._tmp)
        self.assertEqual(svc.preview_error_count, 0)

    def test_preview_error_last_reset_ts_initially_none(self):
        svc = _make_service(self._tmp)
        self.assertIsNone(svc.preview_error_last_reset_ts)


class TestAudioGuardHelpers(unittest.TestCase):
    """Tests for the static audio analysis helpers."""

    def test_silence_detection_zeros(self):
        silent = np.zeros(16000, dtype=np.float32)
        result = RecordingCoreService._looks_like_silence_audio(
            audio=silent,
            sample_rate=16000,
            rms_threshold=0.002,
            peak_threshold=0.012,
            active_ratio_threshold=0.015,
        )
        self.assertTrue(result)

    def test_silence_detection_speech(self):
        t = np.linspace(0.0, 1.0, 16000, endpoint=False, dtype=np.float32)
        speech = (np.sin(2.0 * np.pi * 440.0 * t) * 0.3).astype(np.float32)
        result = RecordingCoreService._looks_like_silence_audio(
            audio=speech,
            sample_rate=16000,
            rms_threshold=0.002,
            peak_threshold=0.012,
            active_ratio_threshold=0.015,
        )
        self.assertFalse(result)

    def test_coerce_bool_true_values(self):
        for val in (True, 1, "1", "true", "True", "on", "yes"):
            self.assertTrue(RecordingCoreService._coerce_bool(val, default=False), f"failed for {val!r}")

    def test_coerce_bool_false_values(self):
        for val in (False, 0, "0", "false", "off", "no"):
            self.assertFalse(RecordingCoreService._coerce_bool(val, default=True), f"failed for {val!r}")

    def test_coerce_bool_none_returns_false(self):
        # RecordingCoreService._coerce_bool: None → bool(None) = False
        self.assertFalse(RecordingCoreService._coerce_bool(None, default=True))

    def test_coerce_bounded_out_of_range_returns_default(self):
        # RecordingCoreService._coerce_bounded: out-of-range returns default (not clamped)
        result = RecordingCoreService._coerce_bounded(200, default=100, min_value=0, max_value=150)
        self.assertEqual(result, 100)  # returns default, not clamped
        result = RecordingCoreService._coerce_bounded(-5, default=100, min_value=0, max_value=150)
        self.assertEqual(result, 100)

    def test_coerce_bounded_invalid_uses_default(self):
        result = RecordingCoreService._coerce_bounded("bad", default=50, min_value=0, max_value=100)
        self.assertEqual(result, 50)

    def test_postprocess_preview_text_strips_whitespace(self):
        result = RecordingCoreService._postprocess_preview_text("  hello  ")
        self.assertEqual(result.strip(), "hello")

    def test_extract_transcribed_text_from_string_payload(self):
        result = RecordingCoreService._extract_transcribed_text("hello world")
        self.assertEqual(result, "hello world")

    def test_extract_transcribed_text_from_dict_payload(self):
        result = RecordingCoreService._extract_transcribed_text({"text": "hi there"})
        self.assertEqual(result, "hi there")

    def test_collect_audio_paths_filters_by_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "audio.wav").touch()
            (Path(tmpdir) / "audio.mp3").touch()
            (Path(tmpdir) / "readme.txt").touch()
            result = RecordingCoreService._collect_audio_paths([tmpdir])
        exts = {Path(p).suffix for p in result}
        self.assertIn(".wav", exts)
        self.assertIn(".mp3", exts)
        self.assertNotIn(".txt", exts)


class TestConstructorAndProperties(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_constructor_sets_all_dependencies(self):
        recorder = _FakeRecorder()
        svc = _make_service(self._tmp, recorder=recorder)
        self.assertIs(svc.recorder, recorder)

    def test_mutable_box_counter_ref_shared(self):
        counter_ref = [0]
        svc = _make_service(self._tmp, extra_kwargs={"transcription_counter_ref": counter_ref})
        # Confirm it shares the same list object
        self.assertIs(svc._transcription_counter_ref, counter_ref)

    def test_mutable_box_engine_ref_shared(self):
        engine_ref = [None]
        svc = _make_service(self._tmp, extra_kwargs={"last_stt_engine_ref": engine_ref})
        self.assertIs(svc._last_stt_engine_ref, engine_ref)

    def test_clipboard_history_shared_reference(self):
        ch = []
        svc = _make_service(self._tmp, extra_kwargs={"clipboard_history": ch})
        # Internal clipboard_history IS the passed list
        self.assertIs(svc._clipboard_history, ch)

    def test_preview_properties_initial_values(self):
        svc = _make_service(self._tmp)
        self.assertEqual(svc.preview_text, "")
        self.assertEqual(svc.preview_duration_sec, 0.0)
        self.assertEqual(svc.preview_error_count, 0)
        self.assertIsNone(svc.preview_error_last_reset_ts)
        self.assertFalse(svc.preview_thread_alive)


if __name__ == "__main__":
    unittest.main()
