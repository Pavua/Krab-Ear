"""Владелец в get_recording_state — живой фикс F1 волны R2.

Тест доказывает контракт между RecordingCoreService и Swift-агентом:
хоткей должен видеть владельца общей записи и не останавливать встречу
или быструю заметку при рассинхроне локального состояния.
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_core_service import RecordingCoreService  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class _FakeRecorder:
    """Минимальный рекордер из test_recording_spill_wiring."""

    sample_rate = 16000
    channels = 1

    def __init__(self):
        self.is_recording = False

    def start(self, spill=None):
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        if not self.is_recording:
            return None
        self.is_recording = False
        timeline = np.linspace(
            0.0, 1.0, 16000, endpoint=False, dtype=np.float32
        )
        audio = (
            np.sin(2.0 * np.pi * 440.0 * timeline) * 0.3
        ).astype(np.float32)
        return audio, 1.0

    def snapshot_rms(self):
        return 0.05

    def snapshot_audio(self):
        return None


class _BlockingStartRecorder(_FakeRecorder):
    """Открывает окно между recorder.is_recording и возвратом из start."""

    def __init__(self):
        super().__init__()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def start(self, spill=None):
        if self.is_recording:
            return False
        self.is_recording = True
        self.start_entered.set()
        if not self.release_start.wait(timeout=2.0):
            raise TimeoutError("Тест не отпустил блокированный recorder.start")
        return True


class _RetainedThread:
    """Управляемый retained handle для двухшагового abort."""

    def __init__(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class _RetryAbortRecorder(_FakeRecorder):
    """Первый abort сбрасывает флаг, но оставляет живой worker-handle."""

    def __init__(self) -> None:
        super().__init__()
        self._thread = _RetainedThread()
        self.abort_calls = 0

    def abort(self, timeout_sec=3.0):
        self.abort_calls += 1
        self.is_recording = False
        self._thread.alive = self.abort_calls == 1
        return self.abort_calls > 1


class _RetryPartialWorker:
    """Первый stop имитирует timeout, второй подтверждает завершение."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self, timeout_sec=3.0):
        self.stop_calls += 1
        return self.stop_calls > 1


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


class _FakeSettingsService:
    def cached_settings(self):
        return {
            "silence_guard_enabled": False,
            "background_guard_enabled": False,
            # Owner-тестам не нужны фоновые preview/partial/RSF workers:
            # выключаем их явно, чтобы daemon-треды не переживали сам тест.
            "realtime_preview_enabled": False,
            "realtime_partial_enabled": False,
            "realtime_silence_filter_enabled": False,
        }

    def invalidate_cache(self):
        pass


class _FakeSemanticSearcher:
    is_enabled = False

    def index_item(self, item_id, text):
        pass


def _make_service(tmp_dir: Path, rescue_dir: Path, recorder: _FakeRecorder):
    """Собрать RecordingCoreService тем же способом, что spill-регрессии."""
    store = StateStore(data_dir=tmp_dir)
    vocabulary = MagicMock()
    vocabulary.get_words.return_value = []
    session_tracker = MagicMock()
    session_tracker._active_session = None
    return RecordingCoreService(
        recorder=recorder,
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        store=store,
        vocabulary=vocabulary,
        settings_svc=_FakeSettingsService(),
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


class RecordingOwnerStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self._tmp = Path(self._tmp_ctx.name)
        self.rescue_dir = self._tmp / "rescue"

    def test_owner_is_none_when_idle(self):
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        state = service.handle_get_recording_state({})
        self.assertIsNone(state["owner"])

    def test_owner_defaults_to_dictation(self):
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        service.handle_start_recording({})
        self.assertEqual(
            service.handle_get_recording_state({})["owner"], "dictation"
        )

    def test_owner_reflects_explicit_source(self):
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        service.handle_start_recording({"source": "meeting"})
        self.assertEqual(
            service.handle_get_recording_state({})["owner"], "meeting"
        )

    def test_owner_changes_to_meeting_when_dictation_is_promoted(self):
        """Promote обязан защищать встречу уже в независимом Task 1."""
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        service.handle_start_recording({})
        response = service.handle_start_recording({"source": "meeting"})
        self.assertEqual(response["status"], "already_recording")
        self.assertEqual(
            service.handle_get_recording_state({})["owner"], "meeting"
        )

    def test_promote_owner_rollback_is_revision_bound(self):
        """Протухший rollback не меняет owner после более нового перехода."""
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        service.handle_start_recording({})
        promoted = service.handle_start_recording({"source": "meeting"})
        revision = promoted["owner_revision"]
        self.assertTrue(promoted["owner_promoted"])

        self.assertFalse(
            service.rollback_owner_transition(
                expected_revision=revision - 1,
                expected_owner="meeting",
                restore_owner="dictation",
            )
        )
        self.assertEqual(service._active_owner, "meeting")
        self.assertTrue(
            service.rollback_owner_transition(
                expected_revision=revision,
                expected_owner="meeting",
                restore_owner="dictation",
            )
        )
        self.assertEqual(service._active_owner, "dictation")

    def test_state_waits_for_atomic_owner_publication(self):
        """Нельзя публиковать is_recording=True раньше owner той же записи."""
        recorder = _BlockingStartRecorder()
        service = _make_service(self._tmp, self.rescue_dir, recorder=recorder)
        start_done = threading.Event()
        state_call_started = threading.Event()
        state_done = threading.Event()
        state_result: dict = {}
        errors: list[BaseException] = []

        def _start() -> None:
            try:
                service.handle_start_recording({"source": "meeting"})
            except BaseException as exc:
                errors.append(exc)
            finally:
                start_done.set()

        def _read_state() -> None:
            state_call_started.set()
            try:
                state_result.update(service.handle_get_recording_state({}))
            except BaseException as exc:
                errors.append(exc)
            finally:
                state_done.set()

        start_thread = threading.Thread(target=_start, daemon=True)
        state_thread = threading.Thread(target=_read_state, daemon=True)
        start_thread.start()
        self.assertTrue(recorder.start_entered.wait(timeout=1.0))
        state_thread.start()
        self.assertTrue(state_call_started.wait(timeout=1.0))
        try:
            self.assertFalse(
                state_done.wait(timeout=0.1),
                "State-read обязан ждать атомарной публикации owner",
            )
        finally:
            recorder.release_start.set()

        start_thread.join(timeout=3.0)
        state_thread.join(timeout=3.0)
        self.assertTrue(start_done.is_set())
        self.assertTrue(state_done.is_set())
        self.assertEqual(errors, [])
        self.assertTrue(state_result["is_recording"])
        self.assertEqual(state_result["owner"], "meeting")

    def test_owner_cleared_after_stop(self):
        service = _make_service(
            self._tmp, self.rescue_dir, recorder=_FakeRecorder()
        )
        service.handle_start_recording({})
        service.handle_stop_recording({"quality_profile": "balanced"})
        self.assertIsNone(service.handle_get_recording_state({})["owner"])

    def test_shutdown_abort_is_atomic_and_owner_bound(self):
        """Shutdown-компенсация не имеет права погасить чужую запись."""
        recorder = _FakeRecorder()
        service = _make_service(self._tmp, self.rescue_dir, recorder=recorder)
        service.handle_start_recording({"source": "meeting"})

        self.assertFalse(service.abort_recording_if_owner("dictation"))
        self.assertTrue(recorder.is_recording)
        self.assertEqual(
            service.handle_get_recording_state({})["owner"],
            "meeting",
        )

        self.assertTrue(service.abort_recording_if_owner("meeting"))
        self.assertFalse(recorder.is_recording)
        self.assertIsNone(service.handle_get_recording_state({})["owner"])

    def test_shutdown_abort_retries_retained_recorder_thread(self):
        """Сброшенный recorder-флаг не скрывает живой thread-handle."""
        recorder = _RetryAbortRecorder()
        service = _make_service(self._tmp, self.rescue_dir, recorder=recorder)
        service.handle_start_recording({"source": "meeting"})

        self.assertFalse(service.abort_recording_if_owner("meeting"))
        self.assertFalse(recorder.is_recording)
        self.assertTrue(recorder._thread.is_alive())
        self.assertEqual(
            service.handle_get_recording_state({})["owner"],
            "meeting",
        )

        self.assertTrue(service.abort_recording_if_owner("meeting"))
        self.assertFalse(recorder._thread.is_alive())
        self.assertEqual(recorder.abort_calls, 2)
        self.assertIsNone(service.handle_get_recording_state({})["owner"])

    def test_shutdown_abort_retries_retained_partial_worker(self):
        """Owner остаётся retry-токеном, пока RT-worker не остановлен."""
        recorder = _FakeRecorder()
        service = _make_service(self._tmp, self.rescue_dir, recorder=recorder)
        service.handle_start_recording({"source": "meeting"})
        partial = _RetryPartialWorker()
        service._rt_partial = partial

        self.assertFalse(service.abort_recording_if_owner("meeting"))
        self.assertFalse(recorder.is_recording)
        self.assertIs(service._rt_partial, partial)
        self.assertEqual(
            service.handle_get_recording_state({})["owner"],
            "meeting",
        )

        self.assertTrue(service.abort_recording_if_owner("meeting"))
        self.assertIsNone(service._rt_partial)
        self.assertEqual(partial.stop_calls, 2)
        self.assertIsNone(service.handle_get_recording_state({})["owner"])


if __name__ == "__main__":
    unittest.main()
