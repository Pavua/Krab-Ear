"""Проверки проводки RecordingCoreService, AudioSelfHealer и wake-word watchdog.

AudioSelfHealer отдельно покрыт в test_audio_selfheal.py. Здесь проверяются
реальные точки вызова из handle_stop_recording, а также полный жизненный цикл
BackendService: фоновые watchdog и wake-word listener обязаны завершаться до
выгрузки CFFI/PortAudio при остановке процесса.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.service import (  # noqa: E402
    BackendService,
    _exit_without_python_finalize_if_wake_word_hung,
)
from backend.state_store import StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (mirrors test_recording_core_service.py's shared fixtures)
# ---------------------------------------------------------------------------

class _FakeRecorder:
    """Recording that stops with normal, non-silent speech-like audio."""

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
    """Recorder whose captured audio is all-zero — the PortAudio-wedged shape."""

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        return np.zeros(32000, dtype=np.float32), 1.0


class _FakeTranscriber:
    """Always returns a real, non-empty transcript."""

    def transcribe(self, audio, **kwargs):
        return {"text": "hello world", "confidence": 0.9, "engine": "fake"}


class _EmptyTextTranscriber:
    """STT ran but produced nothing — the second empty-result shape."""

    def transcribe(self, audio, **kwargs):
        return {"text": "", "confidence": 0.0, "engine": "fake"}


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


class _CallRecordingSelfHealer:
    """Stand-in for AudioSelfHealer that just counts which method fired."""

    def __init__(self):
        self.empty_calls = 0
        self.success_calls = 0

    def record_empty_result(self):
        self.empty_calls += 1

    def record_success(self):
        self.success_calls += 1


def _make_service(tmp_dir, recorder=None, transcriber=None):
    store = StateStore(data_dir=Path(tmp_dir))
    vocab = MagicMock()
    vocab.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class AudioSelfHealWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_defaults_to_none_when_not_wired(self):
        """A service that never got _audio_selfheal injected must behave
        exactly as before (no attribute error, no side effect)."""
        svc = _make_service(self._tmp)
        self.assertIsNone(svc._audio_selfheal)
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "ok")

    def test_silence_guard_trip_calls_record_empty_result(self):
        healer = _CallRecordingSelfHealer()
        svc = _make_service(self._tmp, recorder=_SilentRecorder())
        svc._audio_selfheal = healer
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "empty_audio")
        self.assertTrue(result["silence_detected"])
        self.assertEqual(healer.empty_calls, 1)
        self.assertEqual(healer.success_calls, 0)

    def test_empty_transcript_at_nonzero_duration_calls_record_empty_result(self):
        healer = _CallRecordingSelfHealer()
        svc = _make_service(self._tmp, transcriber=_EmptyTextTranscriber())
        svc._audio_selfheal = healer
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "empty_text")
        self.assertGreater(result["duration_sec"], 0.0)
        self.assertEqual(healer.empty_calls, 1)
        self.assertEqual(healer.success_calls, 0)

    def test_successful_transcript_calls_record_success(self):
        healer = _CallRecordingSelfHealer()
        svc = _make_service(self._tmp)
        svc._audio_selfheal = healer
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(healer.success_calls, 1)
        self.assertEqual(healer.empty_calls, 0)

    def test_background_guard_rejection_does_not_count_as_empty(self):
        """Background-guard rejection is a DIFFERENT heuristic (distant/uniform
        speech, e.g. a TV in the room) — must not feed the wedged-hardware
        counter alongside the RMS silence guard."""
        healer = _CallRecordingSelfHealer()
        svc = _make_service(self._tmp)
        svc._looks_like_distant_background_speech = lambda **kwargs: True
        svc._audio_selfheal = healer
        svc.handle_start_recording({})
        result = svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(result["status"], "empty_audio")
        self.assertTrue(result["background_guard_rejected"])
        self.assertFalse(result["silence_detected"])
        self.assertEqual(healer.empty_calls, 0)
        self.assertEqual(healer.success_calls, 0)

    def test_multiple_stops_accumulate_on_the_same_healer_instance(self):
        """Confirms the SAME AudioSelfHealer instance persists across
        recordings (it is constructed once in BackendService.__init__, not
        per-recording) — a fresh instance every call would make the streak
        counter meaningless."""
        healer = _CallRecordingSelfHealer()
        svc = _make_service(self._tmp, recorder=_SilentRecorder())
        svc._audio_selfheal = healer
        for _ in range(3):
            svc.handle_start_recording({})
            svc.handle_stop_recording({"quality_profile": "balanced"})
        self.assertEqual(healer.empty_calls, 3)


class _FakeDiagnosticsEngine:
    """Минимальный stub AudioEngine — HealthCheckService.handle_get_diagnostics
    читает transcriber.engine.* напрямую (без try/except вокруг самого
    dict-литерала "stt"), в отличие от RecordingCoreService, которому
    достаточно голого _FakeTranscriber.transcribe()."""

    quality_profile = "balanced"
    current_model = "fake-model"

    def _resolve_diarization_device(self) -> str:
        return "cpu"


class _FakeTranscriberWithEngine(_FakeTranscriber):
    """_FakeTranscriber + .engine — нужен только для полноценного BackendService
    (см. _FakeDiagnosticsEngine)."""

    def __init__(self) -> None:
        self.engine = _FakeDiagnosticsEngine()


class BackendServiceWakeWordWatchdogWiringTests(unittest.TestCase):
    """Проверяет проводку AudioReinitCoordinator + WakeWordWatchdog внутри
    полного BackendService (2026-07-15, спека wake-word-watchdog-design.md
    §4.2/§4.3).

    Классы выше конструируют голый RecordingCoreService — координатор и
    watchdog живут на уровень выше, в BackendService.__init__, поэтому эти
    тесты конструируют настоящий BackendService (тот же паттерн, что
    BackendServiceTestCase в test_backend_service.py: FakeRecorder/
    FakeTranscriber/FakeTranslator + обязательный service.close() в
    tearDown — правило #1782 про daemon-треды в chunked CI).
    """

    def setUp(self) -> None:
        # ignore_cleanup_errors=True: BackendService плодит фоновые треды,
        # которые могут писать в data dir уже после конца теста -> OSError
        # при очистке в CI (см. BackendServiceTestCase).
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        store = StateStore(data_dir=Path(self._tmp_ctx.name) / "data")
        self.service = BackendService(
            store=store,
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriberWithEngine(),
            translator=_FakeTranslator(),
        )

    def tearDown(self) -> None:
        # Правило #1782: BackendService(...) без close() в tearDown роняет
        # весь файл чанка на daemon-тредах при завершении интерпретатора.
        self.service.close()

    def test_reinit_coordinator_wired(self):
        coord = self.service._audio_reinit_coordinator
        self.assertIsNotNone(coord)
        self.assertIs(coord._wake_word_adapter, self.service._oww_adapter)
        self.assertIs(self.service._audio_selfheal._reinit_coordinator, coord)

    def test_watchdog_wired_and_running(self):
        wd = self.service._wake_word_watchdog
        self.assertIsNotNone(wd)
        self.assertIs(wd._adapter, self.service._oww_adapter)
        self.assertIs(wd._coordinator, self.service._audio_reinit_coordinator)
        self.assertTrue(wd._thread is not None and wd._thread.is_alive())

    def test_close_stops_watchdog_thread(self):
        wd = self.service._wake_word_watchdog
        self.service.close()
        self.assertFalse(wd._thread is not None and wd._thread.is_alive())

    def test_close_stops_wake_word_listener(self):
        """Shutdown останавливает listener до выгрузки CFFI/PortAudio."""
        stop = MagicMock(return_value=True)
        self.service._oww_adapter.stop = stop

        self.assertTrue(self.service.close())

        stop.assert_called_once_with()

    def test_close_reports_hung_wake_word_listener(self):
        """False от listener должен дойти до process-level shutdown policy."""
        self.service._oww_adapter.stop = MagicMock(return_value=False)

        self.assertFalse(self.service.close())

    def test_hung_listener_uses_controlled_exit_without_python_finalize(self):
        """CFFI-клин обходит небезопасный _Py_Finalize, но только при клине."""
        exit_fn = MagicMock()

        _exit_without_python_finalize_if_wake_word_hung(True, exit_fn=exit_fn)
        _exit_without_python_finalize_if_wake_word_hung(None, exit_fn=exit_fn)
        exit_fn.assert_not_called()

        _exit_without_python_finalize_if_wake_word_hung(False, exit_fn=exit_fn)
        exit_fn.assert_called_once_with(70)

    def test_diagnostics_contains_watchdog_section(self):
        diag = self.service.handle_request(
            {"id": "t", "method": "get_diagnostics", "params": {}},
        )
        section = diag["result"]["wake_word_watchdog"]
        self.assertIn("enabled", section)
        self.assertIn("wedged", section)


if __name__ == "__main__":
    unittest.main()
