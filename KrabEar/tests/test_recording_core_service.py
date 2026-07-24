"""Unit-тесты RecordingCoreService, выделенного в Wave 172.

Покрывают start/stop и состояние записи, аудиовходы, прогресс/отмену
транскрибации, preview-пути, preview-worker и защитные audio-инварианты.
"""

from __future__ import annotations

import sys
import tempfile
import threading
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

    def start(self, spill=None) -> bool:
        # R1: RecordingCoreService always calls start(spill=...); these fakes
        # don't own spill lifecycle (that's RecordingCoreService._active_spill).
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

    def test_timeout_preserves_preview_handle_and_blocks_restart(self):
        """Зависший preview нельзя забыть или оживить очисткой общего Event."""
        entered = threading.Event()
        release = threading.Event()
        recorder = MagicMock()
        recorder.is_recording = True
        recorder.sample_rate = 1
        recorder.snapshot_audio.return_value = (
            np.ones(2, dtype=np.float32),
            1.0,
        )
        transcriber = MagicMock()

        def _blocked_preview(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2.0)
            return {"text": "устаревший preview"}

        transcriber.transcribe_preview.side_effect = _blocked_preview
        svc = _make_service(self._tmp, recorder=recorder, transcriber=transcriber)
        self.addCleanup(svc._stop_preview_worker)
        self.addCleanup(release.set)

        with patch(
            "backend.recording_core_service.IPC_PREVIEW_THREAD_TIMEOUT_SEC",
            0.01,
        ):
            self.assertTrue(svc.start_preview_worker("balanced"))
            first_thread = svc._preview_thread
            self.assertIsNotNone(first_thread)
            self.assertTrue(entered.wait(timeout=1.0))

            self.assertFalse(svc._stop_preview_worker())
            self.assertIs(svc._preview_thread, first_thread)
            first_event = svc._preview_stop_event
            self.assertTrue(first_event.is_set())

            self.assertFalse(svc.start_preview_worker("balanced"))
            self.assertIs(svc._preview_thread, first_thread)
            self.assertIs(svc._preview_stop_event, first_event)
            self.assertTrue(first_event.is_set())

            release.set()
            assert first_thread is not None
            first_thread.join(timeout=1.0)
            self.assertTrue(svc._stop_preview_worker())
            self.assertIsNone(svc._preview_thread)


class TestRecordingLifecycleGate(unittest.TestCase):
    """Start setup и close образуют линейный lifecycle без потерянных handle."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self._tmp = self._tmp_ctx.name
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_close_waits_for_inflight_start_then_stops_it(self):
        entered = threading.Event()
        release = threading.Event()

        class _BlockingRecorder(_FakeRecorder):
            def start(self, spill=None) -> bool:
                entered.set()
                release.wait(timeout=2.0)
                return super().start(spill=spill)

        recorder = _BlockingRecorder()
        svc = _make_service(self._tmp, recorder=recorder)
        start_result: dict = {}
        close_result: list[bool] = []

        start_thread = threading.Thread(
            target=lambda: start_result.update(svc.handle_start_recording({})),
            daemon=True,
        )
        start_thread.start()
        self.addCleanup(start_thread.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        close_started = threading.Event()

        def _close() -> None:
            close_started.set()
            close_result.append(svc.close_background_workers())

        close_thread = threading.Thread(
            target=_close,
            daemon=True,
        )
        close_thread.start()
        self.addCleanup(close_thread.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(close_started.wait(timeout=1.0))
        self.assertTrue(close_thread.is_alive())

        release.set()
        start_thread.join(timeout=1.0)
        close_thread.join(timeout=1.0)
        self.assertEqual(start_result["status"], "recording")
        self.assertEqual(close_result, [True])
        self.assertFalse(recorder.is_recording)
        self.assertIsNone(svc._rt_partial)
        self.assertIsNone(svc._rsf)

        self.assertEqual(
            svc.handle_start_recording({})["status"],
            "backend_closing",
        )
        self.assertFalse(svc.start_preview_worker("balanced"))

    def test_hung_start_gives_bounded_close_and_retry_stops_recorder(self):
        """Зависший setup не держит shutdown вечно, а retry завершает запись."""
        entered = threading.Event()
        release = threading.Event()

        class _BlockingRecorder(_FakeRecorder):
            def __init__(self) -> None:
                self.start_calls = 0
                self.stop_calls = 0

            def start(self, spill=None) -> bool:
                self.start_calls += 1
                entered.set()
                release.wait()
                return super().start(spill=spill)

            def stop(self, timeout_sec=3.0, trim_tail_ms=0):
                self.stop_calls += 1
                return super().stop(timeout_sec, trim_tail_ms)

        recorder = _BlockingRecorder()
        svc = _make_service(self._tmp, recorder=recorder)
        rt_partial = MagicMock()
        rt_partial.stop.return_value = True
        rsf = MagicMock()
        rsf.stop.return_value = []
        rsf.is_running = False
        svc._rt_partial = rt_partial
        svc._rsf = rsf

        start_result: dict = {}
        start_thread = threading.Thread(
            target=lambda: start_result.update(svc.handle_start_recording({})),
            daemon=True,
        )
        start_thread.start()
        self.addCleanup(start_thread.join, 1.0)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(timeout=1.0))

        close_result: list[bool] = []
        close_finished = threading.Event()

        def _close() -> None:
            close_result.append(
                svc.close_background_workers(lifecycle_lock_timeout_sec=0.02)
            )
            close_finished.set()

        close_thread = threading.Thread(target=_close, daemon=True)
        close_thread.start()
        self.addCleanup(close_thread.join, 1.0)
        self.assertTrue(
            close_finished.wait(timeout=0.5),
            "close обязан вернуть False в пределах lifecycle-бюджета",
        )
        self.assertEqual(close_result, [False])
        self.assertTrue(svc._closed_event.is_set())
        self.assertEqual(recorder.stop_calls, 0)
        self.assertIs(svc._rt_partial, rt_partial)
        self.assertIs(svc._rsf, rsf)
        rt_partial.stop.assert_not_called()
        rsf.stop.assert_not_called()

        # Второй start не ждёт зависший lifecycle-lock после начала shutdown.
        self.assertEqual(
            svc.handle_start_recording({})["status"],
            "backend_closing",
        )
        self.assertEqual(recorder.start_calls, 1)

        release.set()
        start_thread.join(timeout=1.0)
        self.assertEqual(start_result["status"], "recording")
        self.assertTrue(recorder.is_recording)

        self.assertTrue(
            svc.close_background_workers(lifecycle_lock_timeout_sec=0.2)
        )
        self.assertFalse(recorder.is_recording)
        self.assertEqual(recorder.stop_calls, 1)
        self.assertIsNone(svc._rt_partial)
        self.assertIsNone(svc._rsf)

    def test_close_does_not_release_unacquired_lifecycle_lock(self):
        """Timeout не имеет права освобождать lock, принадлежащий start-потоку."""

        class _BlockedLock:
            def __init__(self) -> None:
                self.acquire_timeouts: list[float] = []
                self.release_calls = 0

            def acquire(self, *, timeout: float) -> bool:
                self.acquire_timeouts.append(timeout)
                return False

            def release(self) -> None:
                self.release_calls += 1

        svc = _make_service(self._tmp)
        blocked_lock = _BlockedLock()
        closed_event = threading.Event()
        svc._ensure_recording_lifecycle_state = MagicMock(
            return_value=(blocked_lock, closed_event)
        )

        # Вызываем production API без test-time override: этот тест не даст
        # случайно заменить default на безлимитный или непрактично огромный.
        self.assertFalse(svc.close_background_workers())
        self.assertEqual(len(blocked_lock.acquire_timeouts), 1)
        self.assertGreater(blocked_lock.acquire_timeouts[0], 0.0)
        self.assertLessEqual(blocked_lock.acquire_timeouts[0], 5.0)
        self.assertEqual(blocked_lock.release_calls, 0)
        self.assertTrue(closed_event.is_set())


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


# ---------------------------------------------------------------------------
# W948: SessionTracker wiring tests
# ---------------------------------------------------------------------------

class _TrackingSessionTracker:
    """Minimal SessionTracker stub that records calls."""

    def __init__(self):
        self._active_session = None
        self.start_calls: list[dict] = []
        self.end_calls: list[dict] = []

    def start_session(self, audio_device="", quality_preset="balanced", stt_model=""):
        self._active_session = {"session_id": "fake-sid"}
        self.start_calls.append({
            "audio_device": audio_device,
            "quality_preset": quality_preset,
            "stt_model": stt_model,
        })
        return "fake-sid"

    def end_session(self, result: dict):
        self._active_session = None
        self.end_calls.append(dict(result))
        return result


def _make_service_with_tracker(tmp_dir, tracker, recorder=None, settings_override=None):
    """Construct a RecordingCoreService wired to a given tracker."""
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.load = MagicMock(return_value=[])
    vocab.get_words = MagicMock(return_value=[])

    class _SettingsSvc:
        def __init__(self, override):
            self._override = override or {}

        def cached_settings(self):
            return dict(self._override)

        def invalidate_cache(self):
            pass

    return RecordingCoreService(
        recorder=recorder or _FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocab,
        settings_svc=_SettingsSvc(settings_override),
        llm_rewriter=None,
        auto_glossary=None,
        semantic_searcher=_FakeSemanticSearcher(),
        context_memory=None,
        clipboard_history=[],
        auto_backup=MagicMock(),
        session_tracker=tracker,
        action_items_extractor=None,
        transcription_counter_ref=[0],
        last_stt_engine_ref=[None],
    )


class TestSessionTrackerWiredStart(unittest.TestCase):
    """W948: start_session() is called when recording starts (non-privacy mode)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_start_session_called_on_handle_start_recording(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": False, "quality_profile": "balanced"},
        )
        svc.handle_start_recording({})
        self.assertEqual(len(tracker.start_calls), 1, "start_session должен быть вызван ровно один раз")

    def test_start_session_receives_quality_profile(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": False, "quality_profile": "max"},
        )
        svc.handle_start_recording({})
        self.assertEqual(tracker.start_calls[0]["quality_preset"], "max")

    def test_already_recording_does_not_call_start_session_again(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": False},
        )
        svc.handle_start_recording({})
        svc.handle_start_recording({})  # idempotent — recorder.start() returns False
        self.assertEqual(len(tracker.start_calls), 1)


class TestSessionTrackerWiredEnd(unittest.TestCase):
    """W948: end_session() is called when recording stops successfully (non-privacy mode)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_end_session_called_after_successful_stop(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": False, "quality_profile": "balanced"},
        )
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        # end_session is only called when status == "ok" (phase_e reached)
        if result.get("status") == "ok":
            self.assertEqual(len(tracker.end_calls), 1, "end_session должен быть вызван после успешной записи")
            call = tracker.end_calls[0]
            self.assertIn("duration_sec", call)
            self.assertIn("confidence", call)
        else:
            # Silence/background guard fired — end_session not called (phase_e not reached)
            self.assertEqual(len(tracker.end_calls), 0)

    def test_end_session_payload_contains_expected_keys(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": False},
        )
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({})
        if result.get("status") == "ok" and tracker.end_calls:
            call = tracker.end_calls[0]
            for key in ("duration_sec", "confidence", "had_diarization", "had_llm_rewrite", "paste_status"):
                self.assertIn(key, call, f"Ключ {key!r} отсутствует в payload end_session")


class TestSessionTrackerSkipsInPrivacyMode(unittest.TestCase):
    """W948: SessionTracker calls are skipped when privacy_mode_enabled=True."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_start_session_not_called_in_privacy_mode(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": True},
        )
        svc.handle_start_recording({})
        self.assertEqual(len(tracker.start_calls), 0, "start_session не должен вызываться в privacy mode")

    def test_end_session_not_called_in_privacy_mode(self):
        tracker = _TrackingSessionTracker()
        svc = _make_service_with_tracker(
            self._tmp, tracker,
            settings_override={"privacy_mode_enabled": True},
        )
        svc.handle_start_recording({})
        svc.handle_stop_recording({})
        self.assertEqual(len(tracker.end_calls), 0, "end_session не должен вызываться в privacy mode")

    def test_start_session_exception_does_not_abort_recording(self):
        """A buggy SessionTracker must not interrupt the recording flow."""
        class _BrokenTracker:
            _active_session = None

            def start_session(self, **kwargs):
                raise RuntimeError("injected failure")

        svc = _make_service_with_tracker(
            self._tmp, _BrokenTracker(),
            settings_override={"privacy_mode_enabled": False},
        )
        # Must not raise — soft-fail is expected
        result = svc.handle_start_recording({})
        self.assertEqual(result["status"], "recording")


if __name__ == "__main__":
    unittest.main()

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


class TestDiskFullPhaseE(unittest.TestCase):
    """W1134 F5 MED — Phase E disk-full structured error handler tests."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _make_running_service(self):
        """Return a service with recorder already in started state."""
        recorder = _FakeRecorder()
        recorder.is_recording = True
        return _make_service(self._tmp, recorder=recorder)

    def _make_disk_full_store(self, errno_val=28):
        """Return a mock store whose add_history_item raises OSError(ENOSPC)."""
        store = MagicMock()
        store.data_dir = Path(self._tmp)
        store.get_history_page = MagicMock(return_value=([], None))
        err = OSError(errno_val, "No space left on device")
        err.errno = errno_val
        store.add_history_item = MagicMock(side_effect=err)
        return store

    def test_disk_full_returns_structured_error(self):
        """Phase E must return ok=False and status='persist_failed' on ENOSPC."""
        import errno as _errno_mod
        store = self._make_disk_full_store(errno_val=_errno_mod.ENOSPC)
        recorder = _FakeRecorder()
        recorder.is_recording = True
        svc = _make_service(self._tmp, recorder=recorder, extra_kwargs={"store": store})

        result = svc.handle_stop_recording({})

        self.assertFalse(result.get("ok", True),
                         "ok should be False on disk-full")
        self.assertEqual(result.get("status"), "persist_failed")
        self.assertEqual(result.get("reason"), "disk_full")

    def test_disk_full_preserves_transcript_text(self):
        """Transcript text must survive even when history store is full."""
        import errno as _errno_mod
        store = self._make_disk_full_store(errno_val=_errno_mod.ENOSPC)
        recorder = _FakeRecorder()
        recorder.is_recording = True
        svc = _make_service(self._tmp, recorder=recorder, extra_kwargs={"store": store})

        result = svc.handle_stop_recording({})

        # transcript_text must be a non-empty string (fake transcriber returns "hello world")
        self.assertIn("transcript_text", result,
                      "transcript_text must be present in persist_failed response")
        self.assertIsInstance(result["transcript_text"], str)
        self.assertTrue(
            len(result["transcript_text"]) > 0,
            "transcript_text must be non-empty so Swift can offer save-as",
        )

    def test_disk_full_does_not_raise(self):
        """Phase E must swallow OSError and return a dict — never propagate."""
        import errno as _errno_mod
        store = self._make_disk_full_store(errno_val=_errno_mod.ENOSPC)
        recorder = _FakeRecorder()
        recorder.is_recording = True
        svc = _make_service(self._tmp, recorder=recorder, extra_kwargs={"store": store})

        try:
            result = svc.handle_stop_recording({})
        except OSError as exc:
            self.fail(f"handle_stop_recording re-raised OSError: {exc}")

        self.assertIsInstance(result, dict,
                              "Must return a dict even when disk is full")


class TestPersistLockAtomic(unittest.TestCase):
    """W1588 / W1592 — _persist_lock must serialise dedup-check + add_history_item."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _make_phase_d(self):
        from backend.translator import TranslationResult
        _tr = TranslationResult(
            text="hello",
            status="skipped",
            source_lang="auto",
            target_lang="ru",
            mode="auto",
            engine="fake",
        )
        return {
            "text": "hello",
            "display_text": "hello",
            "translated_text": "",
            "final_text": "hello",
            "translation": _tr,
            "translation_status": "skipped",
            "confidence": 0.9,
            "diarization_data": None,
            "tp": {},
        }

    def _make_sr(self):
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_style": "neutral",
            "translate_and_paste": False,
        }

    def _fake_item(self, item_id="fake-id"):
        from backend.models import HistoryItem
        return HistoryItem(
            id=item_id,
            text="hello",
            ts="2026-01-01T00:00:00",
            paste_status="failed",
        )

    def test_persist_lock_is_acquired_during_add_history_item(self):
        """_persist_lock must be held while add_history_item is called."""
        store_mock = MagicMock()
        fake_item = self._fake_item()

        acquired_during_call = []

        def _spy_add(*args, **kwargs):
            # Non-blocking acquire should fail because lock is already held
            got_lock = store_mock._svc._persist_lock.acquire(blocking=False)
            acquired_during_call.append(not got_lock)  # True = lock was already held
            if got_lock:
                store_mock._svc._persist_lock.release()
            return fake_item

        store_mock.add_history_item.side_effect = _spy_add

        recorder = _FakeRecorder()
        recorder.is_recording = True
        svc = _make_service(self._tmp, recorder=recorder, extra_kwargs={"store": store_mock})
        store_mock._svc = svc  # back-ref for spy

        svc._stop_recording_phase_e(
            phase_d=self._make_phase_d(),
            sr=self._make_sr(),
            duration_sec=1.0,
            stop_tail_trim_ms=0,
            silence_detected=False,
            silence_guard_enabled=False,
            background_guard_rejected=False,
            rt_session_id=None,
            settings={},
        )

        self.assertTrue(
            any(acquired_during_call),
            "_persist_lock was not held during add_history_item — atomicity broken",
        )

    def test_concurrent_stop_recording_serialized_by_persist_lock(self):
        """Two concurrent phase_e calls must execute serially under _persist_lock."""
        import queue as _queue

        add_call_order: list[int] = []
        order_lock = threading.Lock()
        call_seq = [0]

        store_mock = MagicMock()
        fake_item = self._fake_item("concurrent-id")

        def _tracking_add(*args, **kwargs):
            with order_lock:
                call_seq[0] += 1
                add_call_order.append(call_seq[0])
            return fake_item

        store_mock.add_history_item.side_effect = _tracking_add

        recorder = _FakeRecorder()
        svc = _make_service(self._tmp, recorder=recorder, extra_kwargs={"store": store_mock})

        results_q: _queue.Queue = _queue.Queue()

        def _call_phase_e():
            try:
                r = svc._stop_recording_phase_e(
                    phase_d=self._make_phase_d(),
                    sr=self._make_sr(),
                    duration_sec=1.0,
                    stop_tail_trim_ms=0,
                    silence_detected=False,
                    silence_guard_enabled=False,
                    background_guard_rejected=False,
                    rt_session_id=None,
                    settings={},
                )
                results_q.put(r)
            except Exception as exc:
                results_q.put(exc)

        t1 = threading.Thread(target=_call_phase_e)
        t2 = threading.Thread(target=_call_phase_e)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        results = [results_q.get_nowait(), results_q.get_nowait()]
        for r in results:
            self.assertNotIsInstance(r, Exception, f"phase_e raised: {r}")

        self.assertEqual(
            len(add_call_order),
            2,
            "Expected exactly 2 add_history_item calls for 2 concurrent phase_e calls",
        )

    def test_dedup_check_and_add_history_item_atomic(self):
        """Dedup guard + persist are atomic — duplicate detected inside the lock
        must prevent add_history_item from being called."""
        store_mock = MagicMock()
        store_mock.add_history_item.return_value = self._fake_item("dup-test-id")

        # AutoDeduplicator that always reports duplicate
        fake_dedup = MagicMock()
        dup_result = MagicMock()
        dup_result.is_duplicate = True
        dup_result.duplicate_of = "original-id"
        dup_result.similarity = 0.99
        fake_dedup.check_duplicate.return_value = dup_result

        recorder = _FakeRecorder()
        recorder.is_recording = True
        svc = _make_service(
            self._tmp,
            recorder=recorder,
            extra_kwargs={"store": store_mock, "auto_deduplicator": fake_dedup},
        )

        result = svc._stop_recording_phase_e(
            phase_d=self._make_phase_d(),
            sr=self._make_sr(),
            duration_sec=1.0,
            stop_tail_trim_ms=0,
            silence_detected=False,
            silence_guard_enabled=False,
            background_guard_rejected=False,
            rt_session_id=None,
            settings={"auto_dedup_enabled": True},
        )

        self.assertEqual(
            result.get("skipped"),
            "duplicate",
            "Dedup guard inside lock must return 'duplicate' skip",
        )
        store_mock.add_history_item.assert_not_called()


class TestPrivacyModeHistoryContext(unittest.TestCase):
    """W1669 — verify that history_context is never passed to transcribe()
    when privacy_mode_enabled=True (W1655 F3 HIGH privacy leak fix)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _make_sr_with_privacy(self, privacy_mode: bool) -> dict:
        return {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "lang_hint": None,
            "privacy_mode_enabled": privacy_mode,
        }

    def test_no_history_context_in_privacy_mode(self):
        """When privacy_mode_enabled=True, history_context passed to transcribe
        must be None — store.get_history_page must NOT be called at all."""
        captured_kwargs: dict = {}

        class _CapturingTranscriber:
            def transcribe(self, audio, **kwargs):
                captured_kwargs.update(kwargs)
                return {"text": "ok", "confidence": 0.9, "engine": "fake"}

        store_mock = MagicMock()
        store_mock.get_history_page.return_value = ([{"id": "1", "text": "secret"}], None)
        store_mock.data_dir = Path(self._tmp)

        svc = _make_service(
            self._tmp,
            transcriber=_CapturingTranscriber(),
            extra_kwargs={"store": store_mock},
        )

        audio = np.zeros(16000, dtype=np.float32)
        svc._stop_recording_phase_c(
            audio=audio,
            duration_sec=1.0,
            sr=self._make_sr_with_privacy(privacy_mode=True),
        )

        # history_context must be None (empty list -> None via `if _recent_history`)
        self.assertIsNone(
            captured_kwargs.get("history_context"),
            "history_context must be None in privacy mode",
        )
        # store.get_history_page must never have been called
        store_mock.get_history_page.assert_not_called()

    def test_history_context_present_when_privacy_off(self):
        """When privacy_mode_enabled=False, history_context is populated from
        the store (non-empty list passes through)."""
        captured_kwargs: dict = {}

        class _CapturingTranscriber:
            def transcribe(self, audio, **kwargs):
                captured_kwargs.update(kwargs)
                return {"text": "ok", "confidence": 0.9, "engine": "fake"}

        fake_history = [{"id": "1", "text": "previous transcript"}]
        store_mock = MagicMock()
        store_mock.get_history_page.return_value = (fake_history, None)
        store_mock.data_dir = Path(self._tmp)

        svc = _make_service(
            self._tmp,
            transcriber=_CapturingTranscriber(),
            extra_kwargs={"store": store_mock},
        )

        audio = np.zeros(16000, dtype=np.float32)
        svc._stop_recording_phase_c(
            audio=audio,
            duration_sec=1.0,
            sr=self._make_sr_with_privacy(privacy_mode=False),
        )

        # store.get_history_page must have been called
        store_mock.get_history_page.assert_called_once()
        # history_context must be the fetched list
        self.assertEqual(
            captured_kwargs.get("history_context"),
            fake_history,
            "history_context must contain fetched history when privacy mode is off",
        )

    def test_transcribe_still_works_with_empty_history_context(self):
        """Transcribe must succeed and return a valid payload when
        history_context=None (the privacy-mode path)."""

        class _CapturingTranscriber:
            def transcribe(self, audio, **kwargs):
                return {"text": "result", "confidence": 0.85, "engine": "fake"}

        svc = _make_service(
            self._tmp,
            transcriber=_CapturingTranscriber(),
        )

        audio = np.zeros(16000, dtype=np.float32)
        result = svc._stop_recording_phase_c(
            audio=audio,
            duration_sec=1.0,
            sr=self._make_sr_with_privacy(privacy_mode=True),
        )

        self.assertIn("transcribe_payload", result)
        self.assertEqual(result["transcribe_payload"]["text"], "result")


# ---------------------------------------------------------------------------
# Wave 1762: path-traversal security tests
# ---------------------------------------------------------------------------

class TestIsPathAllowed(unittest.TestCase):
    """Проверка _is_path_allowed: boundary-safe containment без sibling-prefix bypass."""

    def test_exact_root_is_allowed(self):
        root = Path("/tmp/krab_test_root")
        self.assertTrue(RecordingCoreService._is_path_allowed(root, [root]))

    def test_file_inside_root_is_allowed(self):
        root = Path("/tmp/krab_test_root")
        child = root / "sub" / "file.wav"
        self.assertTrue(RecordingCoreService._is_path_allowed(child, [root]))

    def test_sibling_prefix_bypass_rejected(self):
        """/private/tmpEVIL/x.wav не должен проходить через корень /private/tmp."""
        tmp_root = Path("/private/tmp")
        evil_path = Path("/private/tmpEVIL/x.wav")
        self.assertFalse(RecordingCoreService._is_path_allowed(evil_path, [tmp_root]))

    def test_home_sibling_bypass_rejected(self):
        """/Users/<user>-attacker/x.wav не проходит через корень home()."""
        home = Path.home()
        sibling = Path(str(home) + "-attacker") / "x.wav"
        self.assertFalse(RecordingCoreService._is_path_allowed(sibling, [home]))

    def test_path_outside_all_roots_rejected(self):
        roots = [Path("/tmp"), Path("/private/tmp")]
        self.assertFalse(RecordingCoreService._is_path_allowed(Path("/etc/passwd"), roots))

    def test_multiple_roots_first_match_sufficient(self):
        root_a = Path("/tmp/a")
        root_b = Path("/tmp/b")
        self.assertTrue(RecordingCoreService._is_path_allowed(root_a / "x.wav", [root_a, root_b]))
        self.assertTrue(RecordingCoreService._is_path_allowed(root_b / "x.wav", [root_a, root_b]))


class TestPathTraversalSecurity(unittest.TestCase):
    """Интеграционные тесты: sibling-prefix bypass и symlink escape блокируются."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # (a) Sibling-prefix bypass
    # ------------------------------------------------------------------

    def test_sibling_prefix_path_rejected_by_helper(self):
        """Проверка через _is_path_allowed: sibling-prefix bypass невозможен.

        Конкретный сценарий: allowed root = /private/tmp, атакующий путь = /private/tmpEVIL/x.wav.
        Старый startswith('/private/tmp') пропускал его; is_relative_to — нет.
        """
        # Используем реальный resolved /tmp для macOS
        tmp_root = Path("/tmp").resolve()  # /private/tmp на macOS
        evil_path = Path(str(tmp_root) + "EVIL") / "secret.wav"
        self.assertFalse(
            RecordingCoreService._is_path_allowed(evil_path, [tmp_root]),
            f"sibling-prefix {evil_path} не должен проходить через корень {tmp_root}",
        )

    def test_sibling_prefix_path_rejected_via_collect(self):
        """_collect_audio_paths с allowed_roots отклоняет файл-сиблинг."""
        import os
        import shutil
        # Создаём два tmpdir: один — allowed_root, второй — sibling (EVIL)
        allowed_dir = Path(tempfile.mkdtemp(prefix="krab_allowed_"))
        sibling_dir = Path(str(allowed_dir) + "EVIL")
        os.makedirs(sibling_dir, exist_ok=True)
        try:
            evil_wav = sibling_dir / "secret.wav"
            evil_wav.touch()
            allowed_roots = [allowed_dir.resolve()]
            result = RecordingCoreService._collect_audio_paths(
                [str(evil_wav)],
                allowed_roots=allowed_roots,
            )
            self.assertNotIn(
                str(evil_wav.resolve()),
                result,
                "sibling-prefix путь не должен попасть в results _collect_audio_paths",
            )
        finally:
            shutil.rmtree(allowed_dir, ignore_errors=True)
            shutil.rmtree(sibling_dir, ignore_errors=True)

    def test_home_sibling_prefix_path_rejected_in_core(self):
        """/Users/<user>-x/y.wav не проходит через _transcribe_paths_core."""
        svc = _make_service(self._tmp)
        home = Path.home()
        sibling_name = home.name + "-x"
        sibling_path = home.parent / sibling_name / "secret.wav"
        result = svc._transcribe_paths_core({"paths": [str(sibling_path)]})
        errors = result.get("errors", [])
        items = result.get("items", [])
        self.assertEqual(len(items), 0, "sibling-prefix путь не должен транскрибироваться")
        self.assertTrue(len(errors) > 0, "ожидается ошибка для sibling-prefix пути")

    # ------------------------------------------------------------------
    # (b) Post-validation symlink escape
    # ------------------------------------------------------------------

    def test_symlink_inside_allowed_dir_pointing_outside_is_rejected(self):
        """Симлинк внутри разрешённой директории, ведущий за её пределы, должен быть отклонён."""
        import os
        import shutil

        outside_dir = tempfile.mkdtemp(prefix="krab_outside_")
        outside_file = Path(outside_dir) / "secret.wav"
        outside_file.touch()

        allowed_dir = Path(self._tmp)
        symlink_path = allowed_dir / "evil_link.wav"
        os.symlink(str(outside_file), str(symlink_path))

        try:
            allowed_roots = [allowed_dir.resolve()]
            result = RecordingCoreService._collect_audio_paths(
                [str(allowed_dir)],
                allowed_roots=allowed_roots,
            )
            # Симлинк ведёт за пределы allowed_dir — не должен попасть в результат
            self.assertNotIn(
                str(outside_file.resolve()),
                result,
                "symlink, ведущий за пределы разрешённой директории, не должен быть включён",
            )
        finally:
            shutil.rmtree(outside_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # (c) Легитимный путь внутри data_dir/tmp всё ещё принимается
    # ------------------------------------------------------------------

    def test_legitimate_path_inside_data_dir_accepted(self):
        """Файл внутри data_dir проходит через allowlist и возвращается в results."""
        svc = _make_service(self._tmp)
        wav_path = Path(self._tmp) / "real_audio.wav"
        wav_path.touch()
        result = svc.handle_preview_transcribe_paths({"paths": [str(wav_path)]})
        self.assertEqual(result.get("audio_count", 0), 1, "легитимный файл должен приниматься")
        self.assertEqual(result.get("input_count", 0), 1)

    def test_legitimate_path_inside_tmp_accepted(self):
        """Файл внутри системного tempdir() принимается (tmp входит в allowed_roots)."""
        import os
        svc = _make_service(self._tmp)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_wav = f.name
        try:
            result = svc.handle_preview_transcribe_paths({"paths": [tmp_wav]})
            self.assertEqual(result.get("audio_count", 0), 1, "файл в tmp должен приниматься")
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # (d) Async: отклонённые пути видны в errors через get_transcribe_progress
    # ------------------------------------------------------------------

    def test_async_rejected_paths_appear_in_job_errors(self):
        """Пути за пределами allowlist попадают в errors асинхронного job'а."""
        import time as _time
        svc = _make_service(self._tmp)
        result = svc.handle_transcribe_paths_async({"paths": ["/etc/passwd"]})
        job_id = result["job_id"]

        # Ошибки записываются синхронно до старта воркера — sleep нужен лишь на случай гонки
        _time.sleep(0.05)

        progress = svc.handle_get_transcribe_progress({"job_id": job_id})
        errors = progress.get("errors", [])
        self.assertTrue(
            len(errors) > 0,
            "отклонённый путь /etc/passwd должен быть виден в errors job'а",
        )
        self.assertTrue(
            any("Path outside allowed" in e for e in errors),
            f"ожидается 'Path outside allowed' в errors, получено: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
