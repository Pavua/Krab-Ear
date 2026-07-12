"""MeetingSessionService: аккумулятор, GPU-слот, CHUNK_STT, события (C2a).

Все тесты — без тредов: _run_due_job_once(now) зовётся напрямую.
"""
import sys
import threading
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
