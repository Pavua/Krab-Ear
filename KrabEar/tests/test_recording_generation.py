"""Поколение записи связывает token, owner и rescue-spill (R2 Task 2).

Тесты фиксируют единый lifecycle-контракт RecordingCoreService: успешный
физический start публикует одно поколение под lifecycle-lock, promote сохраняет
его token, CAS-rollback меняет только owner/revision, а завершение снимает
поколение. Токен одновременно является session_id spill-файла.
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

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.recorder import AudioRecorderStopTimeout  # noqa: E402
from backend.recording_spill import RecordingSpillWriter  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FakeRecorder:
    """Минимальный recorder без фоновых потоков для generation-контракта."""

    sample_rate = 16000
    channels = 1

    def __init__(self, *, start_ok: bool = True) -> None:
        self.is_recording = False
        self.start_ok = start_ok
        self.received_spill = None

    def start(self, spill=None):
        self.received_spill = spill
        if not self.start_ok or self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        timeline = np.linspace(
            0.0, 1.0, self.sample_rate, endpoint=False, dtype=np.float32
        )
        audio = (
            np.sin(2.0 * np.pi * 440.0 * timeline) * 0.3
        ).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _RaisesOnRepeatRecorder(_FakeRecorder):
    """Повторный start бросает вместо штатного False."""

    def __init__(self) -> None:
        super().__init__()
        self.repeat_spill = None

    def start(self, spill=None):
        if self.is_recording:
            self.repeat_spill = spill
            raise RuntimeError("повторный start упал")
        return super().start(spill=spill)


class _RaisesAfterCaptureRecorder(_FakeRecorder):
    """Физически захватывает микрофон, затем имитирует late exception."""

    def start(self, spill=None):
        self.received_spill = spill
        self.is_recording = True
        raise RuntimeError("ошибка после захвата")


class _RaisesTypeErrorAfterCaptureRecorder(_FakeRecorder):
    """Внутренний TypeError после захвата не равен несовместимой сигнатуре."""

    def start(self, spill=None):
        self.received_spill = spill
        self.is_recording = True
        raise TypeError("внутренняя ошибка recorder.start")


class _TimeoutOnceRecorder(_FakeRecorder):
    """Первый stop оставляет generation как retry-handle."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_calls = 0

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.stop_calls += 1
        if self.stop_calls == 1:
            # Реальный AudioRecorder перед join уже снимает is_recording, но
            # сохраняет живой thread и spill для повторной попытки.
            self.is_recording = False
            raise AudioRecorderStopTimeout("worker ещё жив")
        return super().stop(
            timeout_sec=timeout_sec,
            trim_tail_ms=trim_tail_ms,
        )


class _FakeTranscriber:
    def transcribe(self, audio, **kwargs):
        return {"text": "generation test", "confidence": 0.9, "engine": "fake"}


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
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
            "llm_brain_unload_on_recording": False,
            "llm_brain_lease_enabled": False,
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
    tmp_dir: Path,
    rescue_dir: Path,
    *,
    recorder: _FakeRecorder | None = None,
    settings_overrides: dict | None = None,
) -> RecordingCoreService:
    """Собрать Core без настоящих audio/STT worker-ов."""
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder or _FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=StateStore(data_dir=tmp_dir),
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
        rescue_dir=rescue_dir,
    )


class SpillWriterSessionIdTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.rescue_dir = Path(self._tmp_ctx.name) / "rescue"

    def test_accepts_external_session_id(self):
        writer = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=16000,
            channels=1,
            source="dictation",
            session_id="tok-123",
        )
        self.assertEqual(writer.session_id, "tok-123")
        self.assertEqual(writer.part_path.name, "tok-123.f32.part")

    def test_generates_own_id_when_omitted(self):
        writer = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=16000,
            channels=1,
            source="dictation",
        )
        self.assertTrue(writer.session_id)

    def test_rejects_path_like_external_session_id(self):
        for unsafe in ("../escape", "nested/token", r"nested\token", ".."):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    RecordingSpillWriter(
                        rescue_dir=self.rescue_dir,
                        sample_rate=16000,
                        channels=1,
                        source="dictation",
                        session_id=unsafe,
                    )

    def test_duplicate_external_session_id_does_not_append_or_rewrite(self):
        first = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=16000,
            channels=1,
            source="dictation",
            session_id="same-token",
        )
        self.assertTrue(first.open())
        first.append(np.ones(16, dtype=np.float32))
        first.close()
        original_audio = first.part_path.read_bytes()
        original_meta = first._meta_path.read_bytes()

        second = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=8000,
            channels=2,
            source="meeting",
            session_id="same-token",
        )

        self.assertFalse(second.open())
        second.discard()
        self.assertEqual(first.part_path.read_bytes(), original_audio)
        self.assertEqual(first._meta_path.read_bytes(), original_meta)
        first.discard()

    def test_discard_retries_after_partial_unlink_failure(self):
        """Временная ошибка unlink не отнимает у writer право на cleanup."""
        writer = RecordingSpillWriter(
            rescue_dir=self.rescue_dir,
            sample_rate=16000,
            channels=1,
            source="dictation",
            session_id="retry-discard",
        )
        self.assertTrue(writer.open())
        writer.append(np.ones(16, dtype=np.float32))
        writer.close()
        original_unlink = Path.unlink
        failed_once = False

        def _unlink_with_one_fault(path, *args, **kwargs):
            nonlocal failed_once
            if path == writer.part_path and not failed_once:
                failed_once = True
                raise OSError("временная ошибка unlink")
            return original_unlink(path, *args, **kwargs)

        with patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=_unlink_with_one_fault,
        ):
            writer.discard()

        self.assertTrue(writer.part_path.exists())
        self.assertFalse(writer._meta_path.exists())
        self.assertTrue(writer._owns_paths)

        writer.discard()

        self.assertFalse(writer.part_path.exists())
        self.assertFalse(writer._meta_path.exists())
        self.assertFalse(writer._owns_paths)


class RecordingGenerationTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self.rescue_dir = self._tmp / "rescue"

    def _service(
        self,
        *,
        recorder: _FakeRecorder | None = None,
        settings_overrides: dict | None = None,
    ) -> RecordingCoreService:
        service = _make_service(
            self._tmp,
            self.rescue_dir,
            recorder=recorder,
            settings_overrides=settings_overrides,
        )
        self.addCleanup(service.close_background_workers)
        return service

    def test_start_creates_generation_and_returns_token(self):
        service = self._service()

        response = service.handle_start_recording({})

        self.assertEqual(response["status"], "recording")
        self.assertTrue(response["generation_token"])
        self.assertEqual(response["owner"], "dictation")
        generation = service._active_generation
        self.assertEqual(generation["state"], "capturing")
        self.assertEqual(generation["token"], response["generation_token"])
        self.assertEqual(generation["owner"], "dictation")
        self.assertEqual(generation["revision"], response["owner_revision"])
        self.assertIsNone(generation["promoted_from"])

    def test_generation_token_is_spill_session_id(self):
        recorder = _FakeRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )

        response = service.handle_start_recording({})

        self.assertIsNotNone(recorder.received_spill)
        self.assertEqual(
            recorder.received_spill.session_id,
            response["generation_token"],
        )
        self.assertTrue(
            recorder.received_spill.part_path.name.startswith(
                response["generation_token"]
            )
        )

    def test_spill_disabled_still_creates_generation(self):
        service = self._service(
            settings_overrides={"recording_spill_enabled": False}
        )

        response = service.handle_start_recording({})

        self.assertEqual(response["status"], "recording")
        self.assertTrue(response["generation_token"])
        self.assertIsNone(service._active_spill)

    def test_state_exposes_same_token_owner_and_start_request_id(self):
        service = self._service()
        start_request_id = "  meeting-lease/α  "
        response = service.handle_start_recording({
            "source": "meeting",
            "start_request_id": start_request_id,
        })

        state = service.handle_get_recording_state({})

        self.assertEqual(state["owner"], "meeting")
        self.assertEqual(
            state["generation_token"],
            response["generation_token"],
        )
        self.assertEqual(response["start_request_id"], start_request_id)
        self.assertEqual(state["start_request_id"], start_request_id)
        self.assertEqual(
            service._active_generation["start_request_id"],
            start_request_id,
        )

    def test_same_owner_and_start_request_id_replays_successful_start(self):
        """Один client lease повторяет успех без нового physical start."""
        recorder = _RaisesOnRepeatRecorder()
        service = self._service(recorder=recorder)
        start_request_id = "  opaque lease/α  "
        started = service.handle_start_recording({
            "source": "dictation",
            "start_request_id": start_request_id,
        })

        replayed = service.handle_start_recording({
            "source": "dictation",
            "start_request_id": start_request_id,
        })

        self.assertEqual(started["status"], "recording")
        self.assertEqual(replayed["status"], "recording")
        self.assertEqual(
            replayed["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(
            replayed["owner_revision"],
            started["owner_revision"],
        )
        self.assertEqual(replayed["start_request_id"], start_request_id)
        self.assertIsNone(recorder.repeat_spill)

    def test_foreign_start_request_id_keeps_original_lease(self):
        """Чужой ID не присваивает себе уже активное поколение."""
        service = self._service()
        started = service.handle_start_recording({
            "source": "meeting",
            "start_request_id": "meeting-owner-A",
        })

        foreign = service.handle_start_recording({
            "source": "meeting",
            "start_request_id": "meeting-owner-B",
        })

        self.assertEqual(foreign["status"], "already_recording")
        self.assertEqual(
            foreign["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(
            foreign["start_request_id"],
            "meeting-owner-A",
        )
        self.assertEqual(
            service._active_generation["start_request_id"],
            "meeting-owner-A",
        )

    def test_invalid_start_request_id_is_rejected_before_capture(self):
        """Тип, пустота и верхняя граница проверяются до recorder.start."""
        invalid_values = (None, 7, "", "x" * 257)
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                recorder = _FakeRecorder()
                service = self._service(recorder=recorder)

                with self.assertRaises(ValueError):
                    service.handle_start_recording({
                        "source": "dictation",
                        "start_request_id": invalid_value,
                    })

                self.assertFalse(recorder.is_recording)
                self.assertIsNone(service._active_generation)

    def test_promote_preserves_token_and_advances_revision(self):
        service = self._service()
        started = service.handle_start_recording({})

        promoted = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(promoted["status"], "already_recording")
        self.assertTrue(promoted["owner_promoted"])
        self.assertEqual(
            promoted["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(promoted["owner"], "meeting")
        self.assertGreater(
            promoted["owner_revision"],
            started["owner_revision"],
        )
        self.assertEqual(service._active_generation["owner"], "meeting")
        self.assertEqual(
            service._active_generation["promoted_from"],
            "dictation",
        )

    def test_promote_rollback_preserves_token(self):
        service = self._service()
        started = service.handle_start_recording({})
        promoted = service.handle_start_recording({"source": "meeting"})

        rolled_back = service.rollback_owner_transition(
            expected_revision=promoted["owner_revision"],
            expected_owner="meeting",
            restore_owner="dictation",
        )

        self.assertTrue(rolled_back)
        self.assertEqual(
            service._active_generation["token"],
            started["generation_token"],
        )
        self.assertEqual(service._active_generation["owner"], "dictation")
        self.assertIsNone(service._active_generation["promoted_from"])
        self.assertGreater(
            service._active_generation["revision"],
            promoted["owner_revision"],
        )

    def test_stale_cas_from_previous_generation_cannot_change_new_one(self):
        """Revision G1 не является правом менять owner уже созданной G2."""
        service = self._service()
        service.handle_start_recording({"source": "dictation"})
        promoted_g1 = service.handle_start_recording({"source": "meeting"})
        stale_revision = promoted_g1["owner_revision"]
        service.handle_stop_recording({"quality_profile": "balanced"})

        started_g2 = service.handle_start_recording({"source": "dictation"})
        promoted_g2 = service.handle_start_recording({"source": "meeting"})

        self.assertFalse(
            service.rollback_owner_transition(
                expected_revision=stale_revision,
                expected_owner="meeting",
                restore_owner="dictation",
            )
        )
        self.assertEqual(
            service._active_generation["token"],
            started_g2["generation_token"],
        )
        self.assertEqual(service._active_generation["owner"], "meeting")
        self.assertEqual(
            service._active_generation["revision"],
            promoted_g2["owner_revision"],
        )

    def test_repeat_same_owner_returns_existing_generation(self):
        service = self._service()
        started = service.handle_start_recording({"source": "dictation"})

        repeated = service.handle_start_recording({"source": "dictation"})

        self.assertEqual(repeated["status"], "already_recording")
        self.assertFalse(repeated["owner_promoted"])
        self.assertEqual(repeated["owner"], "dictation")
        self.assertEqual(
            repeated["generation_token"],
            started["generation_token"],
        )
        self.assertIsNone(repeated["start_request_id"])

    def test_promote_does_not_reenter_recorder_or_replace_live_spill(self):
        """Owner-переход решается до recorder.start и placeholder B."""
        recorder = _RaisesOnRepeatRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        started = service.handle_start_recording({"source": "dictation"})
        live_spill = service._active_spill
        self.assertIsNotNone(live_spill)

        promoted = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(promoted["status"], "already_recording")
        self.assertTrue(promoted["owner_promoted"])
        self.assertIs(service._active_spill, live_spill)
        self.assertTrue(live_spill.part_path.exists())
        self.assertEqual(
            service._active_generation["token"],
            started["generation_token"],
        )
        self.assertEqual(service._active_generation["owner"], "meeting")
        self.assertIsNone(recorder.repeat_spill)

    def test_post_capture_exception_publishes_matching_generation(self):
        """Late exception возвращает честный degraded start с тем же token."""
        recorder = _RaisesAfterCaptureRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )

        response = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(response["status"], "recording")
        self.assertTrue(response["post_start_degraded"])
        self.assertEqual(response["owner"], "meeting")
        self.assertIs(service._active_spill, recorder.received_spill)
        self.assertEqual(
            response["generation_token"],
            service._active_generation["token"],
        )
        self.assertEqual(
            response["generation_token"],
            recorder.received_spill.session_id,
        )

    def test_internal_type_error_after_capture_keeps_matching_spill(self):
        """TypeError из тела start не запускает опасный legacy retry."""
        recorder = _RaisesTypeErrorAfterCaptureRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )

        response = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(response["status"], "recording")
        self.assertTrue(response["post_start_degraded"])
        self.assertIs(service._active_spill, recorder.received_spill)
        self.assertTrue(recorder.received_spill.part_path.exists())
        self.assertEqual(
            response["generation_token"],
            recorder.received_spill.session_id,
        )
        self.assertEqual(
            response["generation_token"],
            service._active_generation["token"],
        )

    def test_recorder_timeout_preserves_generation_token_and_spill(self):
        """Timeout не превращает ещё живую запись в завершённое поколение."""
        recorder = _TimeoutOnceRecorder()
        service = self._service(
            recorder=recorder,
            settings_overrides={"recording_spill_enabled": True},
        )
        started = service.handle_start_recording({"source": "meeting"})
        generation = service._active_generation
        spill = service._active_spill

        response = service.handle_stop_recording({})

        self.assertEqual(response["status"], "recorder_timeout")
        self.assertIs(service._active_generation, generation)
        self.assertEqual(
            service._active_generation["token"],
            started["generation_token"],
        )
        self.assertIs(service._active_spill, spill)
        self.assertTrue(spill.part_path.exists())
        state = service.handle_get_recording_state({})
        self.assertEqual(
            state["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(state["owner"], "meeting")

    def test_finished_g1_tail_cannot_clear_new_g2(self):
        """Тяжёлый хвост stop G1 не владеет generation/spill уже идущей G2."""
        service = self._service(
            settings_overrides={"recording_spill_enabled": True}
        )
        started_g1 = service.handle_start_recording({"source": "dictation"})
        spill_g1 = service._active_spill
        phase_b_entered = threading.Event()
        release_phase_b = threading.Event()
        errors: list[BaseException] = []
        stop_result: dict = {}
        original_phase_b = service._stop_recording_phase_b

        def _blocking_phase_b(*args, **kwargs):
            phase_b_entered.set()
            if not release_phase_b.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил phase B поколения G1")
            return original_phase_b(*args, **kwargs)

        def _stop_g1() -> None:
            try:
                stop_result.update(service.handle_stop_recording({}))
            except BaseException as exc:
                errors.append(exc)

        service._stop_recording_phase_b = _blocking_phase_b
        stop_thread = threading.Thread(target=_stop_g1, daemon=True)
        stop_thread.start()
        self.assertTrue(phase_b_entered.wait(timeout=1.0))
        try:
            self.assertIsNone(service._active_generation)
            self.assertIsNone(service._active_spill)
            started_g2 = service.handle_start_recording(
                {"source": "meeting"}
            )
            spill_g2 = service._active_spill
            self.assertNotEqual(
                started_g2["generation_token"],
                started_g1["generation_token"],
            )
            self.assertIsNot(spill_g2, spill_g1)
        finally:
            release_phase_b.set()

        stop_thread.join(timeout=3.0)
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stop_result.get("status"), "ok")
        self.assertEqual(
            service._active_generation["token"],
            started_g2["generation_token"],
        )
        self.assertEqual(service._active_generation["owner"], "meeting")
        self.assertIs(service._active_spill, spill_g2)
        self.assertTrue(spill_g2.part_path.exists())

    def test_privacy_mode_without_spill_still_publishes_generation(self):
        """Generation не зависит ни от rescue-файла, ни от SessionTracker."""
        service = self._service(
            settings_overrides={
                "privacy_mode_enabled": True,
                "recording_spill_enabled": False,
            }
        )

        response = service.handle_start_recording({"source": "meeting"})

        self.assertEqual(response["status"], "recording")
        self.assertTrue(response["generation_token"])
        self.assertEqual(service._active_generation["owner"], "meeting")
        self.assertIsNone(service._active_spill)
        service._session_tracker.start_session.assert_not_called()

    def test_stop_clears_generation_from_state(self):
        service = self._service()
        service.handle_start_recording({})

        service.handle_stop_recording({"quality_profile": "balanced"})
        state = service.handle_get_recording_state({})

        self.assertIsNone(service._active_generation)
        self.assertIsNone(state["owner"])
        self.assertIsNone(state["generation_token"])

    def test_failed_start_does_not_publish_generation(self):
        recorder = _FakeRecorder(start_ok=False)
        service = self._service(recorder=recorder)

        response = service.handle_start_recording({})

        self.assertEqual(response["status"], "recorder_stopping")
        self.assertIsNone(service._active_generation)
        self.assertIsNone(service._active_spill)


if __name__ == "__main__":
    unittest.main()
