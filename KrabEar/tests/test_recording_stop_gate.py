"""Токенный гейт остановки и lifecycle finalizing-поколений (R2 Task 3).

Тесты фиксируют границу права на физический ``recorder.stop()``: только
совпавший token или запрос старого клиента без поля token проходят дальше.
После успешной phase A поколение G1 остаётся адресуемым во время тяжёлых фаз,
не мешая новой записи G2, а терминализация удаляет только собственную G1.
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

from backend.recorder import AudioRecorderStopTimeout  # noqa: E402
from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


def _audio_result() -> tuple[np.ndarray, float]:
    """Вернуть короткий речеподобный буфер без обращения к аудиоустройству."""
    timeline = np.linspace(
        0.0,
        1.0,
        16000,
        endpoint=False,
        dtype=np.float32,
    )
    audio = (
        np.sin(2.0 * np.pi * 440.0 * timeline) * 0.3
    ).astype(np.float32)
    return audio, 1.0


class _CountingRecorder:
    """Управляемый recorder со счётчиками физических start/stop."""

    sample_rate = 16000
    channels = 1

    def __init__(self, *, stop_audio: np.ndarray | None = None) -> None:
        self.is_recording = False
        self.received_spill = None
        self.start_calls = 0
        self.stop_calls = 0
        self._stop_audio = stop_audio

    def start(self, spill=None):
        self.start_calls += 1
        self.received_spill = spill
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.stop_calls += 1
        if not self.is_recording:
            return None
        self.is_recording = False
        if self._stop_audio is not None:
            return self._stop_audio, 1.0
        return _audio_result()

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _TimeoutThenAudioRecorder(_CountingRecorder):
    """Первый stop истекает, повторный возвращает сохранённое аудио."""

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.stop_calls += 1
        if self.stop_calls == 1:
            self.is_recording = False
            raise AudioRecorderStopTimeout("worker ещё завершает чанк")
        return _audio_result()


class _FakeTranscriber:
    def __init__(self, *, text: str = "stop gate", fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    def transcribe(self, audio, **kwargs):
        if self.fail:
            raise RuntimeError("STT упал в тесте stop-gate")
        return {
            "text": self.text,
            "confidence": 0.9,
            "engine": "fake",
        }


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


class _FakeSettingsService:
    def __init__(self, overrides: dict | None = None) -> None:
        self.overrides = dict(overrides or {})

    def cached_settings(self):
        settings = {
            "recording_spill_enabled": False,
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
            "llm_brain_unload_on_recording": False,
            "llm_brain_lease_enabled": False,
            "auto_glossary_enabled": False,
            "stt_hotwords_enabled": False,
            "diarization_enabled": False,
            "recording_owner_enforce": False,
        }
        settings.update(self.overrides)
        return settings

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(
    data_dir: Path,
    *,
    recorder: _CountingRecorder | None = None,
    transcriber: _FakeTranscriber | None = None,
    settings_overrides: dict | None = None,
) -> RecordingCoreService:
    """Собрать Core без настоящих audio/STT/background worker-ов."""
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    vocabulary.load.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder or _CountingRecorder(),
        transcriber=transcriber or _FakeTranscriber(),
        translator=_FakeTranslator(),
        store=StateStore(data_dir=data_dir),
        vocabulary=vocabulary,
        settings_svc=_FakeSettingsService(settings_overrides),
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
        rescue_dir=data_dir / "rescue",
    )


class StopGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self._service_index = 0

    def _service(
        self,
        *,
        recorder: _CountingRecorder | None = None,
        transcriber: _FakeTranscriber | None = None,
        settings_overrides: dict | None = None,
    ) -> RecordingCoreService:
        self._service_index += 1
        data_dir = self._tmp / f"service-{self._service_index}"
        service = _make_service(
            data_dir,
            recorder=recorder,
            transcriber=transcriber,
            settings_overrides=settings_overrides,
        )
        self.addCleanup(service.close_background_workers)
        return service

    def _assert_generation_finished(
        self,
        service: RecordingCoreService,
        token: str,
    ) -> None:
        self.assertIsNone(service._active_generation)
        self.assertNotIn(
            token,
            getattr(service, "_finalizing_generations", {}),
        )

    def test_matching_token_stops_normally(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording({"source": "dictation"})

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_foreign_token_rejects_before_any_worker_teardown(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service.handle_start_recording({"source": "meeting"})
        partial = MagicMock()
        partial.stop.return_value = True
        rsf = MagicMock()
        rsf.stop.return_value = []
        rsf.is_running = False
        service._rt_partial = partial
        service._rsf = rsf
        active_generation = service._active_generation

        with patch.object(
            service,
            "_stop_preview_worker",
            wraps=service._stop_preview_worker,
        ) as stop_preview:
            response = service.handle_stop_recording(
                {"generation_token": "foreign-token"}
            )

        self.assertEqual(response["status"], "unknown_generation")
        self.assertEqual(
            response["generation_token"],
            "foreign-token",
        )
        self.assertEqual(recorder.stop_calls, 0)
        stop_preview.assert_not_called()
        partial.stop.assert_not_called()
        rsf.stop.assert_not_called()
        self.assertIs(service._active_generation, active_generation)
        self.assertTrue(recorder.is_recording)

    def test_only_absent_token_uses_legacy_path(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        service.handle_start_recording({})

        response = service.handle_stop_recording({})

        self.assertEqual(response["status"], "ok")
        self.assertEqual(recorder.stop_calls, 1)

    def test_malformed_present_tokens_are_rejected_without_teardown(self):
        malformed_tokens = ("", None, 0, [], {})
        for token in malformed_tokens:
            with self.subTest(token=token):
                recorder = _CountingRecorder()
                service = self._service(recorder=recorder)
                service.handle_start_recording({})

                response = service.handle_stop_recording(
                    {"generation_token": token}
                )

                self.assertEqual(
                    response["status"],
                    "unknown_generation",
                )
                self.assertEqual(recorder.stop_calls, 0)
                self.assertTrue(recorder.is_recording)

    def test_replay_hook_returns_without_touching_new_capture(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording({"source": "meeting"})
        replay = {
            "status": "ok",
            "history_id": "old-history",
            "text": "старый результат",
        }

        with patch.object(
            service,
            "_replay_terminal_response",
            return_value=replay,
        ) as replay_hook:
            response = service.handle_stop_recording(
                {"generation_token": "old-token"}
            )

        self.assertEqual(response, replay)
        replay_hook.assert_called_once_with("old-token")
        self.assertEqual(recorder.stop_calls, 0)
        self.assertTrue(recorder.is_recording)
        self.assertEqual(
            service._active_generation["token"],
            started["generation_token"],
        )

    def test_matching_token_does_not_bypass_owner_enforcement(self):
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_owner_enforce": True},
        )
        started = service.handle_start_recording({"source": "meeting"})

        response = service.handle_stop_recording(
            {
                "generation_token": started["generation_token"],
                "source": "dictation",
            }
        )

        self.assertEqual(response["status"], "owner_mismatch")
        self.assertEqual(response["owner"], "meeting")
        self.assertEqual(response["requested"], "dictation")
        self.assertEqual(recorder.stop_calls, 0)
        self.assertTrue(recorder.is_recording)

    def test_retry_g1_while_g2_captures_reports_stop_in_progress(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started_g1 = service.handle_start_recording(
            {"source": "dictation"}
        )
        phase_b_entered = threading.Event()
        release_phase_b = threading.Event()
        phase_b_lock = threading.Lock()
        phase_b_calls = 0
        errors: list[BaseException] = []
        stop_g1_result: dict = {}
        original_phase_b = service._stop_recording_phase_b

        def _blocking_first_phase_b(*args, **kwargs):
            nonlocal phase_b_calls
            with phase_b_lock:
                phase_b_calls += 1
                current_call = phase_b_calls
            if current_call == 1:
                phase_b_entered.set()
                if not release_phase_b.wait(timeout=2.0):
                    raise TimeoutError("Тест не отпустил phase B поколения G1")
            return original_phase_b(*args, **kwargs)

        def _stop_g1() -> None:
            try:
                stop_g1_result.update(
                    service.handle_stop_recording(
                        {
                            "generation_token": (
                                started_g1["generation_token"]
                            )
                        }
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        service._stop_recording_phase_b = _blocking_first_phase_b
        stop_thread = threading.Thread(target=_stop_g1, daemon=True)
        stop_thread.start()
        try:
            self.assertTrue(phase_b_entered.wait(timeout=1.0))
            self.assertIn(
                started_g1["generation_token"],
                service._finalizing_generations,
            )
            started_g2 = service.handle_start_recording(
                {"source": "meeting"}
            )
            active_g2 = service._active_generation

            retry = service.handle_stop_recording(
                {"generation_token": started_g1["generation_token"]}
            )

            self.assertEqual(retry["status"], "stop_in_progress")
            self.assertIs(service._active_generation, active_g2)
            self.assertEqual(
                active_g2["token"],
                started_g2["generation_token"],
            )
            self.assertTrue(recorder.is_recording)
        finally:
            release_phase_b.set()
            stop_thread.join(timeout=3.0)

        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stop_g1_result.get("status"), "ok")
        self.assertNotIn(
            started_g1["generation_token"],
            service._finalizing_generations,
        )
        self.assertIs(service._active_generation, active_g2)

    def test_terminalizer_compare_delete_waits_for_lifecycle_lock(self):
        service = self._service()
        generation_g1 = {
            "token": "G1",
            "owner": "dictation",
            "state": "finalizing",
            "started_at": 1.0,
            "promoted_from": None,
            "revision": 1,
        }
        service._finalizing_generations = {"G1": generation_g1}
        lifecycle_lock, _ = service._ensure_recording_lifecycle_state()
        entered = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []

        def _terminalize_g1() -> None:
            entered.set()
            try:
                service._terminalize_generation(
                    generation_g1,
                    {"status": "ok"},
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        with lifecycle_lock:
            thread = threading.Thread(
                target=_terminalize_g1,
                daemon=True,
            )
            thread.start()
            self.assertTrue(entered.wait(timeout=1.0))
            self.assertFalse(
                finished.wait(timeout=0.1),
                "terminalizer обязан ждать общий lifecycle-lock",
            )
            generation_g2 = service._publish_active_generation_locked(
                token="G2",
                owner="meeting",
            )

        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(finished.is_set())
        self.assertNotIn("G1", service._finalizing_generations)
        self.assertIs(service._active_generation, generation_g2)

    def test_recorder_timeout_preserves_generation_for_token_retry(self):
        recorder = _TimeoutThenAudioRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        started = service.handle_start_recording({"source": "meeting"})
        generation = service._active_generation
        spill = service._active_spill

        timed_out = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(timed_out["status"], "recorder_timeout")
        self.assertIs(service._active_generation, generation)
        self.assertEqual(generation["state"], "capturing")
        self.assertIs(service._active_spill, spill)
        self.assertEqual(
            getattr(service, "_finalizing_generations", {}),
            {},
        )

        start_calls_before_retry = recorder.start_calls
        blocked_start = service.handle_start_recording(
            {"source": "dictation"}
        )

        self.assertEqual(blocked_start["status"], "recorder_stopping")
        self.assertFalse(blocked_start["is_recording"])
        self.assertEqual(
            blocked_start["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(blocked_start["owner"], "meeting")
        self.assertEqual(recorder.start_calls, start_calls_before_retry)
        self.assertIs(service._active_generation, generation)
        self.assertIs(service._active_spill, spill)
        self.assertEqual(
            getattr(service, "_finalizing_generations", {}),
            {},
        )

        retried = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(retried["status"], "ok")
        self.assertEqual(recorder.stop_calls, 2)
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_backlog_limit_blocks_fresh_capture_before_spill_and_start(self):
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        service._finalizing_generations = {
            f"G{index}": {
                "token": f"G{index}",
                "owner": "dictation",
                "state": "finalizing",
                "started_at": float(index),
                "promoted_from": None,
                "revision": index,
            }
            for index in range(8)
        }
        map_before = service._finalizing_generations

        blocked = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(blocked["status"], "recorder_stopping")
        self.assertEqual(recorder.start_calls, 0)
        self.assertIs(service._finalizing_generations, map_before)
        self.assertEqual(len(map_before), 8)
        self.assertIsNone(service._active_generation)
        self.assertFalse(
            service._rescue_dir.exists()
            and any(service._rescue_dir.iterdir())
        )

        service._terminalize_generation(
            map_before["G0"],
            {"status": "ok"},
        )
        started = service.handle_start_recording({"source": "meeting"})
        self.assertEqual(started["status"], "recording")
        self.assertEqual(recorder.start_calls, 1)

    def test_backlog_limit_does_not_block_repeat_of_active_capture(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording({"source": "meeting"})
        service._finalizing_generations = {
            f"G{index}": {
                "token": f"G{index}",
                "owner": "dictation",
                "state": "finalizing",
                "started_at": float(index),
                "promoted_from": None,
                "revision": index,
            }
            for index in range(8)
        }

        repeated = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(repeated["status"], "already_recording")
        self.assertEqual(
            repeated["generation_token"],
            started["generation_token"],
        )
        self.assertIsNotNone(service._active_generation)

    def test_legacy_new_instance_lazily_gets_finalizing_registry(self):
        service = RecordingCoreService.__new__(RecordingCoreService)
        lifecycle_lock, _ = service._ensure_recording_lifecycle_state()

        with lifecycle_lock:
            registry = service._finalizing_generations_locked()

        self.assertEqual(registry, {})
        self.assertIs(registry, service._finalizing_generations)

    def test_already_stopped_terminalizes_original_generation(self):
        recorder = _CountingRecorder()
        service = self._service(recorder=recorder)
        started = service.handle_start_recording({})
        recorder.is_recording = False

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "already_stopped")
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_empty_audio_terminalizes_generation(self):
        recorder = _CountingRecorder(
            stop_audio=np.array([], dtype=np.float32)
        )
        service = self._service(recorder=recorder)
        started = service.handle_start_recording({})

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "empty_audio")
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_phase_b_silence_terminalizes_generation(self):
        recorder = _CountingRecorder(
            stop_audio=np.zeros(16000, dtype=np.float32)
        )
        service = self._service(
            recorder=recorder,
            settings_overrides={"silence_guard_enabled": True},
        )
        started = service.handle_start_recording({})

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "empty_audio")
        self.assertTrue(response["silence_detected"])
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_phase_c_failure_terminalizes_and_keeps_spill_for_rescue(self):
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            transcriber=_FakeTranscriber(fail=True),
            settings_overrides={"recording_spill_enabled": True},
        )
        started = service.handle_start_recording({})
        spill_path = service._active_spill.part_path

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "stt_failed")
        self.assertTrue(spill_path.exists())
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_phase_d_empty_text_terminalizes_generation(self):
        service = self._service(
            transcriber=_FakeTranscriber(text=""),
        )
        started = service.handle_start_recording({})

        response = service.handle_stop_recording(
            {"generation_token": started["generation_token"]}
        )

        self.assertEqual(response["status"], "empty_text")
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_all_phase_e_responses_terminalize_generation(self):
        phase_e_responses = (
            {"status": "ok", "history_id": "h1"},
            {"status": "ok", "skipped": "duplicate"},
            {"status": "persist_failed", "history_id": None},
        )
        for expected in phase_e_responses:
            with self.subTest(response=expected):
                service = self._service()
                started = service.handle_start_recording({})
                with patch.object(
                    service,
                    "_stop_recording_phase_e",
                    return_value=dict(expected),
                ):
                    response = service.handle_stop_recording(
                        {
                            "generation_token": (
                                started["generation_token"]
                            )
                        }
                    )

                self.assertEqual(response, expected)
                self._assert_generation_finished(
                    service,
                    started["generation_token"],
                )

    def test_unexpected_tail_exception_becomes_terminal_failure(self):
        service = self._service(
            settings_overrides={"recording_spill_enabled": True}
        )
        started = service.handle_start_recording({})
        spill_path = service._active_spill.part_path

        with patch.object(
            service,
            "_stop_recording_phase_b",
            side_effect=RuntimeError("неожиданная ошибка phase B"),
        ):
            response = service.handle_stop_recording(
                {"generation_token": started["generation_token"]}
            )

        self.assertEqual(response["status"], "finalization_failed")
        self.assertFalse(response["ok"])
        self.assertEqual(
            response["generation_token"],
            started["generation_token"],
        )
        self.assertTrue(spill_path.exists())
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )

    def test_phase_a_exception_after_physical_stop_terminalizes_g1(self):
        """Fallible hook видит G1 в map и не оставляет stale active-slot."""
        recorder = _CountingRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        started = service.handle_start_recording({})
        spill_path = service._active_spill.part_path
        observed: dict = {}

        def _fail_after_observation(*args, **kwargs):
            observed["active"] = service._active_generation
            observed["in_finalizing"] = (
                started["generation_token"]
                in service._finalizing_generations
            )
            raise RuntimeError("breadcrumb недоступен")

        with patch(
            "backend.recording_core_service.add_breadcrumb",
            side_effect=_fail_after_observation,
        ):
            response = service.handle_stop_recording(
                {"generation_token": started["generation_token"]}
            )

        self.assertFalse(recorder.is_recording)
        self.assertIsNone(observed["active"])
        self.assertTrue(observed["in_finalizing"])
        self.assertEqual(response["status"], "finalization_failed")
        self.assertEqual(
            response["generation_token"],
            started["generation_token"],
        )
        self.assertTrue(spill_path.exists())
        self._assert_generation_finished(
            service,
            started["generation_token"],
        )
        started_g2 = service.handle_start_recording({"source": "meeting"})
        self.assertEqual(started_g2["status"], "recording")
        self.assertNotEqual(
            started_g2["generation_token"],
            started["generation_token"],
        )


if __name__ == "__main__":
    unittest.main()
