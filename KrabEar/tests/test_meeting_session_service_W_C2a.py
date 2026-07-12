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

    def handle_start_recording(self, params):
        self.started.append(params)
        return {"status": "started", "is_recording": True}

    def handle_stop_recording(self, params):
        self.stopped.append(params)
        return {"history_id": "hist-1", "text": "финал"}

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
        svc._stop_worker()
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
        svc._stop_worker()
        self.assertIsNone(svc._worker)


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
    def test_start_worker_failure_rolls_back_session_and_releases_lease(self) -> None:
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
        self.assertIn(("acquire", "krab_ear"), calls)
        self.assertIn(("release", "krab_ear"), calls)

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


if __name__ == "__main__":
    unittest.main()
