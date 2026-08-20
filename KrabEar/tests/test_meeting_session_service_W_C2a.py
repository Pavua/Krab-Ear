"""MeetingSessionService: аккумулятор, GPU-слот, CHUNK_STT, события (C2a).

Все тесты — без тредов: _run_due_job_once(now) зовётся напрямую.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.meeting_session_service import (  # noqa: E402
    MeetingJob,
    MeetingSessionService,
)


class _FakeRecorder:
    def __init__(self, duration: float = 100.0) -> None:
        self.sample_rate = 16000
        self.is_recording = True
        self._duration = duration

    def get_duration_sec(self) -> float:
        return self._duration

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        n = max(0, int((to_sec - from_sec) * self.sample_rate))
        return np.ones(n, dtype=np.float32)


class _FakeTranscriber:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        self.calls.append(float(audio_data.size))
        return {"text": f"чанк{len(self.calls)}"}


class _FakeExtractorResult:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.action_items = []
        self.decisions = ["решение"]
        self.questions = []
        self.fallback_reason = None if ok else "llm_error"
        self.latency_ms = 5


class _FakeExtractor:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.texts: list[str] = []

    def extract(self, transcript: str, language: str = "ru"):
        self.texts.append(transcript)
        return _FakeExtractorResult(ok=self.ok)


class _FakeRecordingCore:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0
        self.started: list[dict] = []
        self.stopped: list[dict] = []
        self._next_generation = 0

    def handle_start_recording(self, params):
        self.started.append(params)
        self._next_generation += 1
        return {
            "status": "recording",
            "is_recording": True,
            "generation_token": f"meeting-generation-{self._next_generation}",
        }

    def handle_stop_recording(self, params):
        self.stopped.append(params)
        return {
            "status": "ok",
            "history_id": "hist-1",
            "text": "финал",
        }

    def pause_realtime_partials(self) -> None:
        self.paused += 1

    def resume_realtime_partials(self) -> None:
        self.resumed += 1


class _SpyBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, payload: dict) -> None:
        with self._lock:
            self.events.append((event_type, payload))

    def types(self) -> list[str]:
        with self._lock:
            return [t for t, _ in self.events]


def _make_svc(privacy: bool = False, extractor=None, recorder=None,
              settings_extra: dict | None = None):
    settings = {
        "privacy_mode_enabled": privacy,
        "meeting_chunk_stt_interval_sec": 25.0,
        "meeting_items_interval_sec": 60.0,
        "meeting_items_language": "ru",
        "llm_brain_lease_enabled": False,  # юниты: lease off (отдельный тест ниже)
    }
    settings.update(settings_extra or {})
    bus = _SpyBus()
    rec = recorder or _FakeRecorder()
    svc = MeetingSessionService(
        recorder=rec,
        transcriber=_FakeTranscriber(),
        recording_core=_FakeRecordingCore(),
        action_items_extractor=extractor,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=bus,
    )
    return svc, bus, rec


class MeetingStartStateTestCase(unittest.TestCase):
    def test_start_when_idle_starts_recording_and_session(self) -> None:
        svc, _, _ = _make_svc()
        svc._recording_core.__class__  # noqa: B018 -- доступность атрибута
        resp = svc.handle_meeting_start({})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp["promoted"])
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["active"])
        svc.close()

    def test_start_when_recording_promotes_with_cursor(self) -> None:
        rec = _FakeRecorder(duration=42.0)
        svc, _, _ = _make_svc(recorder=rec)
        svc._recording_core.handle_start_recording = lambda p: {
            "status": "already_recording", "is_recording": True,
        }
        resp = svc.handle_meeting_start({})
        self.assertTrue(resp["promoted"])
        # курсор аккумулятора = текущая длительность (начало доберёт финальный отчёт)
        self.assertAlmostEqual(svc._session.cursor_sec, 42.0, places=3)
        svc.close()

    def test_owner_conflict_rolls_back_meeting_without_stopping_foreign_recording(
        self,
    ) -> None:
        """Quick capture остаётся жива при запрещённом старте встречи."""
        rec = _FakeRecorder()
        rec.is_recording = True
        svc, bus, _ = _make_svc(recorder=rec)
        core = svc._recording_core
        core.handle_start_recording = lambda params: {
            "status": "owner_conflict",
            "is_recording": True,
            "owner": "quick_capture",
        }

        with self.assertRaises(RuntimeError):
            svc.handle_meeting_start({})

        self.assertTrue(rec.is_recording)
        self.assertIsNone(svc._session)
        self.assertEqual(core.stopped, [])
        self.assertNotIn("meeting.finished", bus.types())
        svc.close()

    def test_start_is_idempotent(self) -> None:
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        resp2 = svc.handle_meeting_start({})
        self.assertTrue(resp2["ok"])
        self.assertTrue(resp2.get("already_active"))
        svc.close()

    def test_privacy_refuses_start(self) -> None:
        svc, _, _ = _make_svc(privacy=True)
        resp = svc.handle_meeting_start({})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("skipped"), "privacy_mode")
        svc.close()


class ChunkSttJobTestCase(unittest.TestCase):
    def test_chunk_stt_appends_and_emits(self) -> None:
        svc, bus, _ = _make_svc()
        svc.handle_meeting_start({})
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertEqual(ran, MeetingJob.CHUNK_STT)
        self.assertIn("meeting.transcript_appended", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertIn("чанк1", state["transcript_tail"])
        self.assertGreater(state["transcript_len"], 0)
        svc.close()

    def test_cursor_advances_no_overlap(self) -> None:
        rec = _FakeRecorder(duration=100.0)
        svc, _, _ = _make_svc(recorder=rec)
        svc.handle_meeting_start({})
        t1 = svc._next_due[MeetingJob.CHUNK_STT] + 0.1
        svc._run_due_job_once(now=t1)
        cursor_after_first = svc._session.cursor_sec
        self.assertAlmostEqual(cursor_after_first, 100.0, places=3)
        # второй тик: длительность не выросла -> пустой диапазон -> STT не зовётся
        calls_before = len(svc._transcriber.calls)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertEqual(len(svc._transcriber.calls), calls_before)
        svc.close()

    def test_no_job_before_due(self) -> None:
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        ran = svc._run_due_job_once(now=0.0)
        self.assertIsNone(ran)
        svc.close()

    def test_out_of_band_stop_finalizes(self) -> None:
        rec = _FakeRecorder()
        svc, bus, _ = _make_svc(recorder=rec)
        svc.handle_meeting_start({})
        rec.is_recording = False  # запись остановили в обход
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertIn("meeting.finished", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])
        svc.close()


class ChunkSttBumpsSttActivityTestCase(unittest.TestCase):
    """Memory Conductor MED-3: CHUNK_STT реально гоняет транскрайбер каждый тик
    живой встречи, но эта активность нигде не отражалась в
    last_stt_activity_ts — долгая встреча могла схватить выгрузку rewriter'а
    посреди себя (см. также test_recording_core_service.TestBumpSttActivitySymmetry)."""

    def setUp(self) -> None:
        from backend import recording_core_service as rcs_module
        self._rcs_module = rcs_module
        self._prev_ts = rcs_module._LAST_STT_ACTIVITY["ts"]
        self.addCleanup(lambda: rcs_module._LAST_STT_ACTIVITY.__setitem__("ts", self._prev_ts))

    def test_chunk_stt_bumps_activity(self) -> None:
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        self._rcs_module._LAST_STT_ACTIVITY["ts"] = 0.0

        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)

        self.assertEqual(ran, MeetingJob.CHUNK_STT)
        self.assertGreater(
            self._rcs_module.last_stt_activity_ts(), 0.0,
            "CHUNK_STT реально вызвал transcribe_preview, но не бампнул активность",
        )
        svc.close()


class _SlowFakeRecordingCore(_FakeRecordingCore):
    """Как _FakeRecordingCore, но handle_start_recording искусственно медленный —
    расширяет окно гонки, чтобы тест надёжно ловил check-then-act баг."""

    def __init__(self, delay_sec: float = 0.2) -> None:
        super().__init__()
        self._delay_sec = delay_sec

    def handle_start_recording(self, params):
        time.sleep(self._delay_sec)
        return super().handle_start_recording(params)


class MeetingStartRaceTestCase(unittest.TestCase):
    def test_concurrent_start_calls_start_recording_once(self) -> None:
        settings = {
            "privacy_mode_enabled": False,
            "meeting_chunk_stt_interval_sec": 25.0,
            "meeting_items_interval_sec": 60.0,
            "meeting_items_language": "ru",
            "llm_brain_lease_enabled": False,
        }
        bus = _SpyBus()
        core = _SlowFakeRecordingCore(delay_sec=0.2)
        svc = MeetingSessionService(
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda k, d=None: settings.get(k, d),
            event_bus=bus,
        )
        results: list[dict] = []
        results_lock = threading.Lock()

        def _call() -> None:
            resp = svc.handle_meeting_start({})
            with results_lock:
                results.append(resp)

        threads = [threading.Thread(target=_call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(
            len(core.started), 1,
            "конкурентные handle_meeting_start НЕ должны стартовать запись дважды",
        )
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.get("ok") for r in results))
        svc.close()


class ItemsLlmJobTestCase(unittest.TestCase):
    def _grow_transcript(self, svc, chars: int = 300) -> None:
        with svc._lock:
            svc._session.chunks.append("х" * chars)
            svc._session.transcript_len += chars

    def test_items_llm_pauses_partials_and_replaces_list(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, bus, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        self.assertEqual(ran, MeetingJob.ITEMS_LLM)
        core = svc._recording_core
        self.assertEqual((core.paused, core.resumed), (1, 1),
                         "LLM-вызов обязан паузить и резюмить партиалы")
        self.assertIn("meeting.items_updated", bus.types())
        state = svc.handle_get_meeting_live_state({})
        self.assertEqual(state["decisions"], ["решение"])
        self.assertFalse(state["degraded"]["llm"])
        svc.close()

    def test_items_llm_resumes_partials_even_on_crash(self) -> None:
        class _BoomExtractor:
            def extract(self, transcript, language="ru"):
                raise RuntimeError("boom")

        svc, _, _ = _make_svc(extractor=_BoomExtractor())
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        with self.assertRaises(RuntimeError):
            svc._job_items_llm(svc._session)
        core = svc._recording_core
        self.assertEqual(core.resumed, core.paused, "resume обязан быть в finally")
        svc.close()

    def test_items_llm_skips_without_growth(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, _, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc, chars=300)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        # рост < 200 симв. -> extract не зовётся второй раз
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        self.assertEqual(len(extractor.texts), 1)
        svc.close()

    def test_no_extractor_sets_degraded(self) -> None:
        svc, _, _ = _make_svc(extractor=None)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["degraded"]["llm"])
        svc.close()

    def test_extract_failure_sets_degraded_keeps_old_items(self) -> None:
        extractor = _FakeExtractor(ok=True)
        svc, _, _ = _make_svc(extractor=extractor)
        svc.handle_meeting_start({})
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        extractor.ok = False
        self._grow_transcript(svc)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.ITEMS_LLM] + 0.1)
        state = svc.handle_get_meeting_live_state({})
        self.assertTrue(state["degraded"]["llm"])
        self.assertEqual(state["decisions"], ["решение"], "старые items сохраняются")
        svc.close()


class MeetingStopTestCase(unittest.TestCase):
    def test_stop_delegates_and_returns_history_id(self) -> None:
        svc, bus, _ = _make_svc()
        svc.handle_meeting_start({})
        resp = svc.handle_meeting_stop({})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["item_id"], "hist-1")
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 1)
        self.assertEqual(len(svc._recording_core.stopped), 1)
        self.assertEqual(
            svc._recording_core.stopped[0],
            {
                "source": "meeting",
                "generation_token": "meeting-generation-1",
            },
        )
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])

    def test_stop_without_session_is_noop(self) -> None:
        svc, _, _ = _make_svc()
        resp = svc.handle_meeting_stop({})
        self.assertTrue(resp["ok"])
        self.assertFalse(resp.get("active", False))

    def test_privacy_mid_meeting_stops_processing(self) -> None:
        settings_box = {"privacy": False}
        svc, bus, _ = _make_svc()
        svc._settings_get = lambda k, d=None: (
            settings_box["privacy"] if k == "privacy_mode_enabled"
            else {"meeting_chunk_stt_interval_sec": 25.0,
                  "meeting_items_interval_sec": 60.0,
                  "meeting_items_language": "ru",
                  "llm_brain_lease_enabled": False}.get(k, d))
        svc.handle_meeting_start({})
        settings_box["privacy"] = True
        ran = svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1)
        self.assertIsNone(ran)
        events_after = len(bus.events)
        svc._run_due_job_once(now=svc._next_due[MeetingJob.CHUNK_STT] + 99.0)
        self.assertEqual(len(bus.events), events_after, "после privacy событий нет")
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"])
        svc.close()


class MeetingRecordingOwnershipTestCase(unittest.TestCase):
    """R2: meeting-обёртка хранит и предъявляет Core lease поколения."""

    def test_privacy_enabled_mid_meeting_skips_core_and_finished(self) -> None:
        svc, bus, _ = _make_svc()
        started = svc.handle_meeting_start({})
        original_settings_get = svc._settings_get
        svc._settings_get = lambda key, default=None: (
            True
            if key == "privacy_mode_enabled"
            else original_settings_get(key, default)
        )

        response = svc.handle_meeting_stop({
            "generation_token": started["generation_token"],
        })

        self.assertEqual(response["status"], "privacy_mode")
        self.assertEqual(response["skipped"], "privacy_mode")
        self.assertEqual(
            response["generation_token"],
            started["generation_token"],
        )
        self.assertEqual(svc._recording_core.stopped, [])
        self.assertIsNone(svc._session)
        self.assertNotIn("meeting.finished", bus.types())

    def test_privacy_retained_worker_reports_retry_until_worker_dies(self) -> None:
        """Приватность не маскирует живой воркер ложным ответом о завершении."""
        svc, bus, _ = _make_svc()
        core = svc._recording_core
        token = svc.handle_meeting_start({})["generation_token"]
        session = svc._session
        self.assertIsNotNone(session)
        self.assertTrue(svc._stop_worker())

        class _RetainedWorker:
            def __init__(self) -> None:
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout=None) -> None:
                return None

        retained = _RetainedWorker()
        svc._worker = retained
        original_settings_get = svc._settings_get
        svc._settings_get = lambda key, default=None: (
            True
            if key == "privacy_mode_enabled"
            else original_settings_get(key, default)
        )
        try:
            pending = svc.handle_meeting_stop({"generation_token": token})

            self.assertEqual(pending["status"], "stop_in_progress")
            self.assertTrue(pending["active"])
            self.assertTrue(pending["privacy_mode_active"])
            self.assertEqual(pending["generation_token"], token)
            self.assertEqual(core.stopped, [])
            self.assertIs(svc._session, session)
            self.assertTrue(session.stop_retry_pending)
            self.assertTrue(session.privacy_stopped)
            self.assertNotIn("meeting.finalizing", bus.types())
            self.assertNotIn("meeting.finished", bus.types())

            retained.alive = False
            terminal = svc.handle_meeting_stop({"generation_token": token})

            self.assertEqual(terminal["status"], "privacy_mode")
            self.assertEqual(terminal["skipped"], "privacy_mode")
            self.assertFalse(terminal["active"])
            self.assertEqual(terminal["generation_token"], token)
            self.assertEqual(core.stopped, [])
            self.assertIsNone(svc._session)
        finally:
            retained.alive = False
            svc._settings_get = original_settings_get
            svc.close()

    def test_start_and_live_state_publish_core_generation_token(self) -> None:
        svc, _, _ = _make_svc()

        started = svc.handle_meeting_start({})
        state = svc.handle_get_meeting_live_state({})
        repeated = svc.handle_meeting_start({})

        self.assertEqual(started["generation_token"], "meeting-generation-1")
        self.assertEqual(state["generation_token"], started["generation_token"])
        self.assertEqual(repeated["generation_token"], started["generation_token"])
        svc.close()

    def test_fresh_start_forwards_and_returns_opaque_core_lease(self) -> None:
        """Новый G1 не меняет ID клиента и хранит revision для строгого stop."""
        svc, _, _ = _make_svc()
        core = svc._recording_core
        request_id = "  meeting-lease/α  "

        def _start(params):
            core.started.append(dict(params))
            return {
                "status": "recording",
                "is_recording": True,
                "generation_token": "meeting-g1",
                "owner_revision": 41,
                "start_request_id": request_id,
            }

        core.handle_start_recording = _start

        started = svc.handle_meeting_start({"start_request_id": request_id})
        state = svc.handle_get_meeting_live_state({})
        repeated = svc.handle_meeting_start({"start_request_id": "другой-id"})

        self.assertEqual(
            core.started,
            [{"source": "meeting", "start_request_id": request_id}],
        )
        for payload in (started, state, repeated):
            self.assertEqual(payload["generation_token"], "meeting-g1")
            self.assertEqual(payload["owner_revision"], 41)
            self.assertEqual(payload["start_request_id"], request_id)
        self.assertTrue(repeated["already_active"])
        svc.close()

    def test_promoted_start_uses_original_core_lease(self) -> None:
        """Повышение G1 возвращает ID исходной диктовки, а не meeting-клика."""
        svc, _, _ = _make_svc()
        core = svc._recording_core
        meeting_click_id = "meeting-click-id"
        dictation_start_id = "dictation-origin-id"

        def _promote(params):
            core.started.append(dict(params))
            return {
                "status": "already_recording",
                "is_recording": True,
                "generation_token": "shared-g1",
                "owner_promoted": True,
                "owner_revision": 42,
                "start_request_id": dictation_start_id,
            }

        core.handle_start_recording = _promote

        started = svc.handle_meeting_start({
            "start_request_id": meeting_click_id,
        })

        self.assertEqual(
            core.started,
            [{"source": "meeting", "start_request_id": meeting_click_id}],
        )
        self.assertTrue(started["promoted"])
        self.assertEqual(started["generation_token"], "shared-g1")
        self.assertEqual(started["owner_revision"], 42)
        self.assertEqual(started["start_request_id"], dictation_start_id)
        svc.close()

    def test_owner_mismatch_retains_strict_lease_without_finalizing(self) -> None:
        """Устаревший meeting-stop не трогает recorder и не теряет CAS-аренду."""
        svc, bus, recorder = _make_svc()
        core = svc._recording_core

        def _start(params):
            core.started.append(dict(params))
            return {
                "status": "recording",
                "is_recording": True,
                "generation_token": "meeting-g1",
                "owner_revision": 51,
                "start_request_id": "meeting-start-id",
            }

        def _owner_mismatch(params):
            core.stopped.append(dict(params))
            return {
                "status": "owner_mismatch",
                "owner": "dictation",
                "requested": "meeting",
                "owner_revision": 52,
            }

        core.handle_start_recording = _start
        core.handle_stop_recording = _owner_mismatch
        started = svc.handle_meeting_start({
            "start_request_id": "meeting-start-id",
        })

        first = svc.handle_meeting_stop({
            "generation_token": started["generation_token"],
        })
        repeated = svc.handle_meeting_stop({
            "generation_token": started["generation_token"],
        })

        expected_stop = {
            "source": "meeting",
            "generation_token": "meeting-g1",
            "expected_owner_revision": 51,
        }
        self.assertEqual(core.stopped, [expected_stop, expected_stop])
        for payload in (first, repeated):
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["status"], "owner_mismatch")
            self.assertEqual(payload["owner"], "dictation")
            self.assertEqual(payload["requested"], "meeting")
            self.assertEqual(payload["owner_revision"], 52)
            self.assertTrue(payload["active"])
        self.assertTrue(recorder.is_recording)
        self.assertIsNotNone(svc._session)
        self.assertTrue(svc._session.stop_retry_pending)
        self.assertEqual(bus.types().count("meeting.finalizing"), 0)
        self.assertEqual(bus.types().count("meeting.finished"), 0)
        svc.close()

    def test_live_events_publish_current_generation_token(self) -> None:
        """Все потоковые meeting-события несут G1 для защиты Swift от устаревших событий."""
        import backend.meeting_session_service as mss
        import tempfile

        class _FakeSoundFile:
            @staticmethod
            def write(path, data, samplerate) -> None:
                Path(path).write_bytes(b"RIFFfake")

        diar_result = {
            "segments": [{"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00"}],
            "speaker_embeddings": {"SPEAKER_00": [1.0] + [0.0] * 7},
        }
        svc, bus, _ = _make_svc(extractor=_FakeExtractor(ok=True))
        original_sf = mss._sf
        with tempfile.TemporaryDirectory() as tmp:
            svc._data_dir = Path(tmp)
            svc._diarize_window = lambda path: diar_result
            mss._sf = _FakeSoundFile()
            try:
                started = svc.handle_meeting_start({})
                token = started["generation_token"]
                session = svc._session
                self.assertIsNotNone(session)
                self.assertTrue(svc._stop_worker())

                svc._job_chunk_stt(session)
                with svc._lock:
                    session.chunks.append("х" * 300)
                    session.transcript_len += 300
                svc._job_items_llm(session)
                svc._job_diar_window(session)

                payloads = {
                    kind: payload
                    for kind, payload in bus.events
                    if kind in {
                        "meeting.transcript_appended",
                        "meeting.items_updated",
                        "meeting.speakers_updated",
                    }
                }
                self.assertEqual(
                    set(payloads),
                    {
                        "meeting.transcript_appended",
                        "meeting.items_updated",
                        "meeting.speakers_updated",
                    },
                )
                for payload in payloads.values():
                    self.assertEqual(payload["generation_token"], token)
            finally:
                mss._sf = original_sf
                svc.close()

    def test_tokenless_stop_uses_server_token_when_recorder_is_already_idle(
        self,
    ) -> None:
        """Legacy Swift не теряет G1, если worker успел объявить recorder idle."""
        svc, _, recorder = _make_svc()
        started = svc.handle_meeting_start({})
        recorder.is_recording = False

        response = svc.handle_meeting_stop({})

        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["generation_token"], started["generation_token"])
        self.assertEqual(
            svc._recording_core.stopped,
            [{
                "source": "meeting",
                "generation_token": started["generation_token"],
            }],
        )

    def test_wrong_or_invalid_token_does_not_call_core_or_replace_it(self) -> None:
        svc, bus, _ = _make_svc()
        started = svc.handle_meeting_start({})

        for supplied in ("foreign-generation", "", None, 42):
            with self.subTest(supplied=supplied):
                response = svc.handle_meeting_stop({"generation_token": supplied})

                self.assertEqual(response["status"], "unknown_generation")
                self.assertEqual(response["generation_token"], supplied)
                self.assertEqual(svc._recording_core.stopped, [])
                self.assertIsNotNone(svc._session)
                self.assertEqual(
                    svc._session.generation_token,
                    started["generation_token"],
                )
                self.assertNotIn("meeting.finalizing", bus.types())
                self.assertNotIn("meeting.finished", bus.types())
        svc.close()

    def test_recorder_timeout_retains_session_token_and_blocks_worker_self_finalize(
        self,
    ) -> None:
        svc, bus, recorder = _make_svc()
        core = svc._recording_core
        stop_results = [
            {
                "status": "recorder_timeout",
                "is_recording": False,
                "preview_text": "черновик встречи",
            },
            {
                "status": "ok",
                "history_id": "hist-recovered",
                "text": "финал встречи",
            },
        ]

        def _stop(params):
            core.stopped.append(params)
            recorder.is_recording = False
            return stop_results.pop(0)

        core.handle_stop_recording = _stop
        started = svc.handle_meeting_start({})
        token = started["generation_token"]

        timed_out = svc.handle_meeting_stop({"generation_token": token})

        self.assertEqual(timed_out["status"], "recorder_timeout")
        self.assertFalse(recorder.is_recording)
        self.assertIsNotNone(svc._session)
        self.assertEqual(svc._session.generation_token, token)
        self.assertTrue(svc._session.stop_retry_pending)
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 0)

        # Имитируем опоздавший тик прежнего worker-а: timeout не terminal.
        self.assertIsNone(
            svc._run_due_job_once(
                now=svc._next_due[MeetingJob.CHUNK_STT] + 0.1,
            )
        )
        self.assertIsNotNone(svc._session)
        self.assertEqual(bus.types().count("meeting.finished"), 0)

        recovered = svc.handle_meeting_stop({"generation_token": token})

        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["item_id"], "hist-recovered")
        self.assertEqual(
            core.stopped,
            [
                {"source": "meeting", "generation_token": token},
                {"source": "meeting", "generation_token": token},
            ],
        )
        self.assertIsNone(svc._session)
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 1)

    def test_repeated_timeout_keeps_retry_handle_after_client_budget_exhaustion(
        self,
    ) -> None:
        """Backend не владеет бюджетом Swift и не стирает G1 после трёх RPC."""
        svc, bus, recorder = _make_svc()
        core = svc._recording_core

        def _timeout(params):
            core.stopped.append(params)
            recorder.is_recording = False
            return {"status": "recorder_timeout", "is_recording": False}

        core.handle_stop_recording = _timeout
        token = svc.handle_meeting_start({})["generation_token"]

        responses = [
            svc.handle_meeting_stop({"generation_token": token})
            for _ in range(3)
        ]

        self.assertTrue(all(r["status"] == "recorder_timeout" for r in responses))
        self.assertIsNotNone(svc._session)
        self.assertEqual(svc._session.generation_token, token)
        self.assertTrue(svc._session.stop_retry_pending)
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 0)
        self.assertEqual(len(core.stopped), 3)
        svc.close()

    def test_core_stop_in_progress_is_nonterminal(self) -> None:
        svc, bus, _ = _make_svc()
        core = svc._recording_core

        def _in_progress(params):
            core.stopped.append(params)
            return {"status": "stop_in_progress"}

        core.handle_stop_recording = _in_progress
        token = svc.handle_meeting_start({})["generation_token"]

        response = svc.handle_meeting_stop({"generation_token": token})

        self.assertEqual(response["status"], "stop_in_progress")
        self.assertTrue(response["active"])
        self.assertIsNotNone(svc._session)
        self.assertTrue(svc._session.stop_retry_pending)
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 0)
        svc.close()

    def test_lost_terminal_response_replays_same_item_without_second_finished(
        self,
    ) -> None:
        svc, bus, _ = _make_svc()
        core = svc._recording_core
        terminal_cache: dict[str, dict] = {}
        physical_persists: list[str] = []

        def _stop_with_replay(params):
            core.stopped.append(params)
            token = params["generation_token"]
            replayed = terminal_cache.get(token)
            if replayed is not None:
                return dict(replayed)
            physical_persists.append(token)
            response = {
                "status": "ok",
                "history_id": "hist-terminal-cache",
            }
            terminal_cache[token] = dict(response)
            return response

        core.handle_stop_recording = _stop_with_replay
        started = svc.handle_meeting_start({})
        token = started["generation_token"]

        first = svc.handle_meeting_stop({"generation_token": token})
        replayed = svc.handle_meeting_stop({"generation_token": token})

        self.assertEqual(first["status"], "ok")
        self.assertEqual(replayed["status"], "ok")
        self.assertEqual(replayed["item_id"], first["item_id"])
        self.assertEqual(bus.types().count("meeting.finalizing"), 1)
        self.assertEqual(bus.types().count("meeting.finished"), 1)
        self.assertEqual(
            physical_persists,
            [token],
            "повтор после потери IPC-ответа обязан пройти через terminal replay",
        )
        self.assertEqual(
            svc._recording_core.stopped,
            [
                {"source": "meeting", "generation_token": token},
                {"source": "meeting", "generation_token": token},
            ],
        )

    def test_retained_worker_defers_core_and_keeps_lifecycle_one_shot(
        self,
    ) -> None:
        """Таймаут join сохраняет G1, а опоздавший воркер не дублирует события."""
        import backend.meeting_session_service as mss

        svc, bus, recorder = _make_svc()
        token = svc.handle_meeting_start({})["generation_token"]
        session = svc._session
        self.assertIsNotNone(session)
        self.assertTrue(svc._stop_worker())

        worker_at_idle_check = threading.Event()
        release_worker = threading.Event()
        original_settings_get = svc._settings_get

        def _settings_get(key, default=None):
            if (
                key == "privacy_mode_enabled"
                and threading.current_thread().name == "retained-meeting-worker"
            ):
                # Воркер уже прошёл проверку stop_retry_pending, но ещё не
                # успел самофинализировать неактивный рекордер. Это узкая гонка.
                worker_at_idle_check.set()
                release_worker.wait(2.0)
            return original_settings_get(key, default)

        svc._settings_get = _settings_get

        def _stale_worker() -> None:
            svc._run_due_job_once(time.monotonic())

        worker = threading.Thread(
            target=_stale_worker,
            name="retained-meeting-worker",
            daemon=True,
        )
        svc._worker = worker
        worker.start()

        original_timeout = mss._WORKER_JOIN_TIMEOUT_SEC
        try:
            self.assertTrue(worker_at_idle_check.wait(2.0))
            mss._WORKER_JOIN_TIMEOUT_SEC = 0.01

            deferred = svc.handle_meeting_stop({"generation_token": token})

            self.assertEqual(deferred["status"], "stop_in_progress")
            self.assertTrue(deferred["active"])
            self.assertEqual(deferred["generation_token"], token)
            self.assertEqual(svc._recording_core.stopped, [])
            self.assertIs(svc._session, session)
            self.assertTrue(svc._session.stop_retry_pending)
            self.assertEqual(
                [payload for kind, payload in bus.events
                 if kind == "meeting.finalizing"],
                [{"generation_token": token}],
            )
            self.assertEqual(
                [payload for kind, payload in bus.events
                 if kind == "meeting.finished"],
                [],
            )

            recorder.is_recording = False
            release_worker.set()
            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive())

            # Воркер был уже после ранней проверки флага повтора, поэтому
            # гард тождества и однократности обязан подавить его старую финализацию.
            self.assertIs(svc._session, session)
            self.assertEqual(svc._recording_core.stopped, [])
            self.assertEqual(bus.types().count("meeting.finalizing"), 1)
            self.assertEqual(bus.types().count("meeting.finished"), 0)

            recovered = svc.handle_meeting_stop({"generation_token": token})

            self.assertEqual(recovered["status"], "ok")
            self.assertEqual(
                svc._recording_core.stopped,
                [{"source": "meeting", "generation_token": token}],
            )
            self.assertIsNone(svc._session)
            self.assertEqual(
                [payload for kind, payload in bus.events
                 if kind == "meeting.finalizing"],
                [{"generation_token": token}],
            )
            self.assertEqual(
                [payload for kind, payload in bus.events
                 if kind == "meeting.finished"],
                [{"item_id": "hist-1", "generation_token": token}],
            )
        finally:
            mss._WORKER_JOIN_TIMEOUT_SEC = original_timeout
            release_worker.set()
            worker.join(timeout=2.0)
            svc._settings_get = original_settings_get
            svc.close()

    def test_concurrent_stop_uses_stored_token_after_terminal_teardown(
        self,
    ) -> None:
        """Конкурентный IPC видит G1 после снятия сессии и отвергает чужие токены."""
        svc, _, _ = _make_svc()
        core = svc._recording_core
        token = svc.handle_meeting_start({})["generation_token"]
        teardown_entered = threading.Event()
        release_teardown = threading.Event()
        original_teardown = svc._teardown_session
        first_response: dict[str, Any] = {}

        def _hold_teardown(*args, **kwargs):
            result = original_teardown(*args, **kwargs)
            teardown_entered.set()
            release_teardown.wait(2.0)
            return result

        svc._teardown_session = _hold_teardown

        def _first_stop() -> None:
            first_response["value"] = svc.handle_meeting_stop({
                "generation_token": token,
            })

        worker = threading.Thread(target=_first_stop, daemon=True)
        worker.start()
        try:
            self.assertTrue(teardown_entered.wait(2.0))
            self.assertIsNone(svc._session)

            tokenless = svc.handle_meeting_stop({})
            same_token = svc.handle_meeting_stop({"generation_token": token})

            self.assertEqual(tokenless["status"], "stop_in_progress")
            self.assertEqual(tokenless["generation_token"], token)
            self.assertEqual(same_token["status"], "stop_in_progress")
            self.assertEqual(same_token["generation_token"], token)

            for foreign in ("foreign-generation", "", None, 42):
                with self.subTest(foreign=foreign):
                    rejected = svc.handle_meeting_stop({
                        "generation_token": foreign,
                    })
                    self.assertEqual(rejected["status"], "unknown_generation")
                    self.assertEqual(rejected["generation_token"], foreign)

            self.assertEqual(
                core.stopped,
                [{"source": "meeting", "generation_token": token}],
            )
        finally:
            release_teardown.set()
            worker.join(timeout=2.0)
            svc._teardown_session = original_teardown
            svc.close()

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_response["value"]["status"], "ok")


class BrainLeaseTestCase(unittest.TestCase):
    def test_meeting_acquires_and_releases_lease(self) -> None:
        import backend.meeting_session_service as mss
        calls: list[tuple[str, Any]] = []

        class _FakeLeaseModule:
            @staticmethod
            def acquire_brain_lease(owner, ttl_sec=30.0, lock_path=None):
                calls.append(("acquire", owner))
                return True

            @staticmethod
            def release_brain_lease(owner, lock_path=None):
                calls.append(("release", owner))

        svc, _, _ = _make_svc(settings_extra={"llm_brain_lease_enabled": True})
        import sys as _sys
        real = _sys.modules.get("backend.brain_lease")
        _sys.modules["backend.brain_lease"] = _FakeLeaseModule()  # type: ignore[assignment]
        try:
            svc.handle_meeting_start({})
            svc.handle_meeting_stop({})
        finally:
            if real is not None:
                _sys.modules["backend.brain_lease"] = real
            else:
                _sys.modules.pop("backend.brain_lease", None)
        self.assertIn(("acquire", "krab_ear"), calls)
        self.assertIn(("release", "krab_ear"), calls)
        del mss  # noqa: F821 -- использован только для читаемости импорта


# ------------------------------------------------------------------------
# C2a Task 10 — фиксы 4 находок security-аудита (см. докстринг модуля):
# Фикс 1 (HIGH) гард двойного GPU-слот-воркера, Фикс 2 (MED) идемпотентный
# stop, Фикс 3 (LOW) lease в close(), Фикс 4 (LOW) rollback-зона накрывает
# _acquire_lease()/_start_worker().
# ------------------------------------------------------------------------


class StaleWorkerGuardTestCase(unittest.TestCase):
    def test_start_refused_while_stale_worker_alive(self) -> None:
        svc, bus, _ = _make_svc()

        # имитация: стоп запрошен, но старый воркер завис в MLX и пережил join
        class _StuckThread:
            def is_alive(self) -> bool:
                return True

        svc._worker = _StuckThread()
        svc._stop_event.set()
        resp = svc.handle_meeting_start({})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp.get("error"), "gpu_slot_busy")
        self.assertEqual(len(svc._recording_core.started), 0,
                         "запись НЕ должна стартовать при занятом GPU-слоте")
        state = svc.handle_get_meeting_live_state({})
        self.assertFalse(state["active"], "reservation не должна остаться")

    def test_live_worker_without_stop_requested_is_not_stale(self) -> None:
        """Живой воркер БЕЗ взведённого stop_event — обычная активная сессия
        (её отсекает already_active-проверка выше), а НЕ авария Фикс 1."""
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        self.assertFalse(svc._stop_event.is_set())
        resp = svc.handle_meeting_start({})
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("already_active"))
        self.assertNotEqual(resp.get("error"), "gpu_slot_busy")
        svc.close()

    def test_stop_worker_keeps_handle_when_join_times_out(self) -> None:
        svc, _, _ = _make_svc()

        class _NeverJoinsThread:
            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                return None  # имитация: поток пережил join-таймаут

        stuck = _NeverJoinsThread()
        svc._worker = stuck
        self.assertFalse(svc._stop_worker())
        self.assertIsNotNone(
            svc._worker, "handle воркера должен сохраниться при таймауте join")
        self.assertIs(svc._worker, stuck)

    def test_stop_worker_clears_handle_when_thread_actually_dead(self) -> None:
        """Контроль: нормальный случай (тред реально умер) по-прежнему
        обнуляет self._worker — регрессия не должна навечно "залипать"."""
        svc, _, _ = _make_svc()

        class _DeadThread:
            def __init__(self) -> None:
                self._joined = False

            def is_alive(self) -> bool:
                return not self._joined

            def join(self, timeout=None) -> None:
                self._joined = True

        svc._worker = _DeadThread()
        self.assertTrue(svc._stop_worker())
        self.assertIsNone(svc._worker)

    def test_close_returns_false_until_retained_worker_dies(self) -> None:
        """Shutdown не стирает meeting-session после join-timeout."""
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        self.assertTrue(svc._stop_worker())

        class _RetryThread:
            def __init__(self) -> None:
                self.alive = True

            def is_alive(self) -> bool:
                return self.alive

            def join(self, timeout=None) -> None:
                return None

        stuck = _RetryThread()
        svc._worker = stuck

        self.assertFalse(svc.close())
        self.assertIs(svc._worker, stuck)
        self.assertIsNotNone(svc._session)

        stuck.alive = False
        self.assertTrue(svc.close())
        self.assertIsNone(svc._worker)
        self.assertIsNone(svc._session)


class _SlowStopFakeRecordingCore(_FakeRecordingCore):
    """Как _FakeRecordingCore, но handle_stop_recording искусственно
    медленный — расширяет окно гонки для теста идемпотентности
    handle_meeting_stop (Фикс 2)."""

    def __init__(self, delay_sec: float = 0.2) -> None:
        super().__init__()
        self._delay_sec = delay_sec

    def handle_stop_recording(self, params):
        time.sleep(self._delay_sec)
        return super().handle_stop_recording(params)


class MeetingStopIdempotencyTestCase(unittest.TestCase):
    def test_concurrent_stop_calls_stop_recording_once(self) -> None:
        settings = {
            "privacy_mode_enabled": False,
            "meeting_chunk_stt_interval_sec": 25.0,
            "meeting_items_interval_sec": 60.0,
            "meeting_items_language": "ru",
            "llm_brain_lease_enabled": False,
        }
        bus = _SpyBus()
        core = _SlowStopFakeRecordingCore(delay_sec=0.2)
        svc = MeetingSessionService(
            recorder=_FakeRecorder(),
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda k, d=None: settings.get(k, d),
            event_bus=bus,
        )
        svc.handle_meeting_start({})

        results: list[dict] = []
        results_lock = threading.Lock()

        def _call() -> None:
            resp = svc.handle_meeting_stop({})
            with results_lock:
                results.append(resp)

        threads = [threading.Thread(target=_call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(
            len(core.stopped), 1,
            "конкурентные handle_meeting_stop НЕ должны звать stop_recording дважды",
        )
        self.assertEqual(bus.types().count("meeting.finished"), 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get("ok") for r in results))
        self.assertEqual(
            sorted(r.get("status") for r in results),
            ["ok", "stop_in_progress"],
        )

    def test_sequential_stop_calls_are_each_independent(self) -> None:
        """_stopping обязан сброситься в finally — второй, ПОСЛЕДОВАТЕЛЬНЫЙ
        (не конкурентный) стоп новой сессии не должен залипнуть в
        already_stopping навсегда."""
        svc, _, _ = _make_svc()
        svc.handle_meeting_start({})
        resp1 = svc.handle_meeting_stop({})
        self.assertTrue(resp1["ok"])
        self.assertNotIn("already_stopping", resp1)

        svc.handle_meeting_start({})
        resp2 = svc.handle_meeting_stop({})
        self.assertTrue(resp2["ok"])
        self.assertNotIn("already_stopping", resp2)
        svc.close()


class CloseReleasesLeaseTestCase(unittest.TestCase):
    def test_close_releases_lease(self) -> None:
        import sys as _sys
        calls: list[tuple[str, Any]] = []

        class _FakeLeaseModule:
            @staticmethod
            def acquire_brain_lease(owner, ttl_sec=30.0, lock_path=None):
                calls.append(("acquire", owner))
                return True

            @staticmethod
            def release_brain_lease(owner, lock_path=None):
                calls.append(("release", owner))

        svc, _, _ = _make_svc(settings_extra={"llm_brain_lease_enabled": True})
        real = _sys.modules.get("backend.brain_lease")
        _sys.modules["backend.brain_lease"] = _FakeLeaseModule()  # type: ignore[assignment]
        try:
            svc.handle_meeting_start({})
            svc.close()
        finally:
            if real is not None:
                _sys.modules["backend.brain_lease"] = real
            else:
                _sys.modules.pop("backend.brain_lease", None)
        self.assertIn(("release", "krab_ear"), calls)


class StartWorkerRollbackTestCase(unittest.TestCase):
    def test_start_worker_failure_rolls_back_before_recorder_and_lease(self) -> None:
        import sys as _sys
        calls: list[tuple[str, Any]] = []

        class _FakeLeaseModule:
            @staticmethod
            def acquire_brain_lease(owner, ttl_sec=30.0, lock_path=None):
                calls.append(("acquire", owner))
                return True

            @staticmethod
            def release_brain_lease(owner, lock_path=None):
                calls.append(("release", owner))

        svc, _, _ = _make_svc(settings_extra={"llm_brain_lease_enabled": True})

        def _boom() -> None:
            raise RuntimeError("meeting: предыдущий воркер ещё жив")

        svc._start_worker = _boom  # имитирует провал защитного пояса Фикс 1в

        real = _sys.modules.get("backend.brain_lease")
        _sys.modules["backend.brain_lease"] = _FakeLeaseModule()  # type: ignore[assignment]
        try:
            with self.assertRaises(RuntimeError):
                svc.handle_meeting_start({})
        finally:
            if real is not None:
                _sys.modules["backend.brain_lease"] = real
            else:
                _sys.modules.pop("backend.brain_lease", None)

        self.assertIsNone(
            svc._session, "сессия обязана откатиться при провале _start_worker")
        self.assertEqual(
            calls,
            [],
            "Preflight-провал происходит до захвата brain lease",
        )
        self.assertEqual(
            svc._recording_core.started,
            [],
            "Провал preflight worker не должен вообще запускать recorder",
        )

    def test_worker_failure_precedes_promote_side_effect(self) -> None:
        """Провал worker не должен даже запрашивать dictation→meeting."""
        svc, _, _ = _make_svc()
        core = svc._recording_core
        calls: list[dict] = []

        def _promote(params):
            calls.append(params)
            return {
                "status": "already_recording",
                "is_recording": True,
            }

        core.handle_start_recording = _promote

        def _boom() -> None:
            raise RuntimeError("meeting worker boom")

        svc._start_worker = _boom
        with self.assertRaises(RuntimeError):
            svc.handle_meeting_start({})

        self.assertEqual(calls, [])
        self.assertIsNone(svc._session)

    def test_start_recording_failure_still_rolls_back_reservation(self) -> None:
        """Регрессия: провал ДО создания session (ещё есть только reservation)
        не должен ломаться на UnboundLocalError в except-ветке."""
        svc, _, _ = _make_svc()

        def _boom(params):
            raise RuntimeError("recording start boom")

        svc._recording_core.handle_start_recording = _boom
        with self.assertRaises(RuntimeError):
            svc.handle_meeting_start({})
        self.assertIsNone(svc._session)


class MeetingTransitionSerializationTestCase(unittest.TestCase):
    def test_stop_and_new_start_wait_for_inflight_start_setup(self) -> None:
        """start→stop→start не должны подменить reservation под старым worker."""
        svc, _, _ = _make_svc()
        setup_entered = threading.Event()
        release_setup = threading.Event()
        stop_started = threading.Event()
        stop_done = threading.Event()
        second_started = threading.Event()
        second_done = threading.Event()
        errors: list[BaseException] = []
        original_start_worker = svc._start_worker

        def _blocking_start_worker() -> None:
            setup_entered.set()
            if not release_setup.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил setup первой встречи")
            original_start_worker()

        svc._start_worker = _blocking_start_worker

        def _call_start(done: threading.Event) -> None:
            try:
                svc.handle_meeting_start({})
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        def _call_stop() -> None:
            stop_started.set()
            try:
                svc.handle_meeting_stop({})
            except BaseException as exc:
                errors.append(exc)
            finally:
                stop_done.set()

        first_done = threading.Event()
        first = threading.Thread(target=_call_start, args=(first_done,), daemon=True)
        stop = threading.Thread(target=_call_stop, daemon=True)

        def _call_second_start() -> None:
            second_started.set()
            _call_start(second_done)

        second = threading.Thread(target=_call_second_start, daemon=True)
        first.start()
        self.assertTrue(setup_entered.wait(timeout=1.0))
        stop.start()
        second.start()
        self.assertTrue(stop_started.wait(timeout=1.0))
        self.assertTrue(second_started.wait(timeout=1.0))
        try:
            self.assertFalse(
                stop_done.wait(timeout=0.1),
                "meeting_stop не должен разбирать незавершённый start-setup",
            )
            self.assertFalse(
                second_done.wait(timeout=0.1),
                "Новый meeting_start не должен обгонять setup первой сессии",
            )
        finally:
            release_setup.set()

        for thread in (first, stop, second):
            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        svc.close()


class MeetingPreflightLifecycleTestCase(unittest.TestCase):
    def test_worker_waits_armed_while_recording_start_is_pending(self) -> None:
        """Preflight-thread не должен self-finalize до recorder.start."""
        recorder = _FakeRecorder()
        recorder.is_recording = False
        core = _FakeRecordingCore()
        bus = _SpyBus()
        core_entered = threading.Event()
        release_core = threading.Event()
        result: dict = {}
        errors: list[BaseException] = []

        def _blocking_start(params):
            core_entered.set()
            if not release_core.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил RecordingCore.start")
            recorder.is_recording = True
            core.started.append(params)
            return {"status": "recording", "is_recording": True}

        core.handle_start_recording = _blocking_start
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=bus,
        )

        def _call_start() -> None:
            try:
                result.update(svc.handle_meeting_start({}))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_call_start, daemon=True)
        thread.start()
        self.assertTrue(core_entered.wait(timeout=1.0))
        try:
            self.assertFalse(
                release_core.wait(timeout=0.7)
            )
            self.assertIsNotNone(
                svc._session,
                "Неармированный worker не должен убрать preflight-session",
            )
            self.assertNotIn("meeting.finished", bus.types())
        finally:
            release_core.set()

        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(result.get("ok"))
        svc.close()

    def test_early_shutdown_rejects_inflight_start_after_core_returns(
        self,
    ) -> None:
        """Ранний гейт не даёт вернуть успех до позднего close worker-а."""
        recorder = _FakeRecorder()
        recorder.is_recording = False
        core = _FakeRecordingCore()
        core_entered = threading.Event()
        release_core = threading.Event()
        errors: list[BaseException] = []
        aborted_owners: list[str] = []

        def _blocking_start(params):
            core_entered.set()
            if not release_core.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил RecordingCore.start")
            recorder.is_recording = True
            return {"status": "recording", "is_recording": True}

        core.handle_start_recording = _blocking_start

        def _abort_owned(owner: str) -> bool:
            aborted_owners.append(owner)
            recorder.is_recording = False
            return True

        core.abort_recording_if_owner = _abort_owned
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )

        thread = threading.Thread(
            target=lambda: self._capture_start_error(svc, errors),
            daemon=True,
        )
        thread.start()
        self.assertTrue(core_entered.wait(timeout=1.0))
        svc.begin_shutdown()
        release_core.set()
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNone(svc._session)
        self.assertFalse(
            recorder.is_recording,
            "close не должен оставить свежий orphan-рекордер",
        )
        self.assertEqual(aborted_owners, ["meeting"])
        svc.close()

    def test_close_rolls_back_inflight_promotion_without_stopping_dictation(
        self,
    ) -> None:
        """Promote rollback возвращает owner, но сохраняет исходную запись."""
        recorder = _FakeRecorder()
        recorder.is_recording = True
        core = _FakeRecordingCore()
        core_entered = threading.Event()
        release_core = threading.Event()
        errors: list[BaseException] = []
        owner_state = {"owner": "meeting", "revision": 7}
        rollback_calls: list[dict] = []

        def _blocking_promote(params):
            core_entered.set()
            if not release_core.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил promote")
            return {
                "status": "already_recording",
                "is_recording": True,
                "owner_promoted": True,
                "owner_revision": owner_state["revision"],
            }

        def _rollback_owner(**kwargs) -> bool:
            rollback_calls.append(kwargs)
            if (
                kwargs["expected_revision"] != owner_state["revision"]
                or kwargs["expected_owner"] != owner_state["owner"]
            ):
                return False
            owner_state["owner"] = kwargs["restore_owner"]
            owner_state["revision"] += 1
            return True

        core.handle_start_recording = _blocking_promote
        core.rollback_owner_transition = _rollback_owner
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )

        thread = threading.Thread(
            target=lambda: self._capture_start_error(svc, errors),
            daemon=True,
        )
        thread.start()
        self.assertTrue(core_entered.wait(timeout=1.0))
        svc.close()
        release_core.set()
        thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(
            recorder.is_recording,
            "rollback promote не должен гасить исходную диктовку",
        )
        self.assertEqual(owner_state["owner"], "dictation")
        self.assertEqual(
            rollback_calls,
            [{
                "expected_revision": 7,
                "expected_owner": "meeting",
                "restore_owner": "dictation",
            }],
        )

    def test_failed_promote_rollback_is_retried_by_close(self) -> None:
        """Исключение CAS сохраняет revision и исходную диктовку для retry."""
        recorder = _FakeRecorder()
        recorder.is_recording = True
        core = _FakeRecordingCore()
        core_entered = threading.Event()
        release_core = threading.Event()
        errors: list[BaseException] = []
        owner_state = {"owner": "meeting", "revision": 11}
        rollback_calls: list[dict] = []

        def _blocking_promote(params):
            core_entered.set()
            if not release_core.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил promote")
            return {
                "status": "already_recording",
                "is_recording": True,
                "owner_promoted": True,
                "owner_revision": owner_state["revision"],
            }

        def _rollback_owner(**kwargs) -> bool:
            rollback_calls.append(kwargs)
            if len(rollback_calls) == 1:
                raise RuntimeError("временная ошибка CAS")
            owner_state["owner"] = kwargs["restore_owner"]
            owner_state["revision"] += 1
            return True

        core.handle_start_recording = _blocking_promote
        core.rollback_owner_transition = _rollback_owner
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )

        thread = threading.Thread(
            target=lambda: self._capture_start_error(svc, errors),
            daemon=True,
        )
        thread.start()
        self.assertTrue(core_entered.wait(timeout=1.0))
        self.assertFalse(svc.close())
        release_core.set()
        thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(recorder.is_recording)
        self.assertEqual(owner_state["owner"], "meeting")
        self.assertTrue(svc._recovery_pending)
        self.assertEqual(svc._recovery_owner_revision, 11)
        self.assertIsNotNone(svc._session)

        self.assertTrue(svc.close())
        self.assertTrue(recorder.is_recording)
        self.assertEqual(owner_state["owner"], "dictation")
        self.assertFalse(svc._recovery_pending)
        self.assertIsNone(svc._session)
        self.assertEqual(len(rollback_calls), 2)

    def test_meeting_stop_retries_promote_rollback_without_stopping_dictation(
        self,
    ) -> None:
        """Даже privacy-stop не превращает promote-recovery в physical stop."""
        recorder = _FakeRecorder()
        recorder.is_recording = True
        core = _FakeRecordingCore()
        owner_state = {"owner": "meeting", "revision": 21}
        rollback_calls: list[dict] = []
        settings = {"privacy_mode_enabled": False}

        core.handle_start_recording = lambda params: {
            "status": "already_recording",
            "is_recording": True,
            "owner_promoted": True,
            "owner_revision": owner_state["revision"],
        }

        def _rollback_owner(**kwargs) -> bool:
            rollback_calls.append(kwargs)
            if len(rollback_calls) == 1:
                raise RuntimeError("временная ошибка CAS")
            owner_state["owner"] = kwargs["restore_owner"]
            owner_state["revision"] += 1
            return True

        core.rollback_owner_transition = _rollback_owner
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": settings["privacy_mode_enabled"],
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )
        svc._arm_worker = lambda: (_ for _ in ()).throw(
            RuntimeError("arm failed")
        )

        with self.assertRaises(RuntimeError):
            svc.handle_meeting_start({})
        self.assertTrue(svc._recovery_pending)
        self.assertEqual(owner_state["owner"], "meeting")
        settings["privacy_mode_enabled"] = True

        response = svc.handle_meeting_stop({})

        self.assertTrue(response["ok"])
        self.assertEqual(response.get("recovered"), "owner_rollback")
        self.assertTrue(recorder.is_recording)
        self.assertEqual(owner_state["owner"], "dictation")
        self.assertEqual(core.stopped, [])
        self.assertFalse(svc._recovery_pending)
        self.assertIsNone(svc._session)
        self.assertEqual(len(rollback_calls), 2)

    def test_failed_fresh_compensation_retains_retryable_session(self) -> None:
        """Неуспешный abort сохраняет handle до повторного close."""
        recorder = _FakeRecorder()
        recorder.is_recording = False
        core = _FakeRecordingCore()
        abort_results = [False, True]

        def _start(params):
            recorder.is_recording = True
            return {
                "status": "recording",
                "is_recording": True,
                "owner_promoted": False,
                "owner_revision": 1,
            }

        def _abort(owner: str) -> bool:
            result = abort_results.pop(0)
            if result:
                recorder.is_recording = False
            return result

        core.handle_start_recording = _start
        core.abort_recording_if_owner = _abort
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )
        svc._arm_worker = lambda: (_ for _ in ()).throw(
            RuntimeError("arm failed")
        )

        with self.assertRaises(RuntimeError):
            svc.handle_meeting_start({})

        self.assertTrue(recorder.is_recording)
        self.assertTrue(svc._recovery_pending)
        self.assertIsNotNone(
            svc._session,
            "session — retry-handle, её нельзя стирать до подтверждённого abort",
        )
        self.assertTrue(svc.close())
        self.assertFalse(recorder.is_recording)
        self.assertFalse(svc._recovery_pending)
        self.assertIsNone(svc._session)

    def test_close_retains_inflight_setup_until_recovery_is_published(
        self,
    ) -> None:
        """close не стирает reservation раньше решения start-компенсации."""
        recorder = _FakeRecorder()
        recorder.is_recording = False
        core = _FakeRecordingCore()
        core_entered = threading.Event()
        release_core = threading.Event()
        abort_results = [False, True]
        errors: list[BaseException] = []

        def _blocking_start(params):
            core_entered.set()
            if not release_core.wait(timeout=2.0):
                raise TimeoutError("Тест не отпустил RecordingCore.start")
            recorder.is_recording = True
            return {
                "status": "recording",
                "is_recording": True,
                "owner_promoted": False,
                "owner_revision": 1,
            }

        def _abort(owner: str) -> bool:
            result = abort_results.pop(0)
            if result:
                recorder.is_recording = False
            return result

        core.handle_start_recording = _blocking_start
        core.abort_recording_if_owner = _abort
        svc = MeetingSessionService(
            recorder=recorder,
            transcriber=_FakeTranscriber(),
            recording_core=core,
            action_items_extractor=None,
            settings_get=lambda key, default=None: {
                "privacy_mode_enabled": False,
                "meeting_chunk_stt_interval_sec": 25.0,
                "meeting_items_interval_sec": 60.0,
                "llm_brain_lease_enabled": False,
            }.get(key, default),
            event_bus=_SpyBus(),
        )

        thread = threading.Thread(
            target=lambda: self._capture_start_error(svc, errors),
            daemon=True,
        )
        thread.start()
        self.assertTrue(core_entered.wait(timeout=1.0))

        self.assertFalse(
            svc.close(),
            "close обязан сохранить reservation незавершённого setup",
        )
        self.assertIsNotNone(svc._session)
        release_core.set()
        thread.join(timeout=3.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(recorder.is_recording)
        self.assertTrue(svc._recovery_pending)
        self.assertIsNotNone(svc._session)

        self.assertTrue(svc.close())
        self.assertFalse(recorder.is_recording)
        self.assertFalse(svc._recovery_pending)
        self.assertIsNone(svc._session)

    @staticmethod
    def _capture_start_error(
        svc: MeetingSessionService,
        errors: list[BaseException],
    ) -> None:
        try:
            svc.handle_meeting_start({})
        except BaseException as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
