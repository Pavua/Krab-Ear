"""DIAR_WINDOW-тик C2b: планирование за рубильником, исполнитель, state/события."""
import sys
import tempfile
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

# Фейки — копия конвенций test_meeting_session_service_W_C2a.py
# (если там появится общий helper — переиспользуй его, не дублируй).


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
    """Копия конвенции test_meeting_session_service_W_C2a.py: нужна, потому что
    test_toggle_off_is_byte_identical_c2a зовёт _run_due_job_once() напрямую —
    CHUNK_STT тоже "созревает" на далёком now и падает на transcriber=None."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        self.calls.append(float(audio_data.size))
        return {"text": f"чанк{len(self.calls)}"}


class _FakeRecordingCore:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    def handle_start_recording(self, params):
        # Статуса "started" продовый RecordingCoreService не возвращает
        # никогда — успешный старт это "recording", промоут поверх идущей
        # записи это "already_recording". R2 валидирует статус строго,
        # поэтому заглушка обязана говорить на языке реального Core.
        return {"status": "recording", "is_recording": True}

    def handle_stop_recording(self, params):
        return {"history_id": "hist-1"}

    def pause_realtime_partials(self):
        self.paused += 1

    def resume_realtime_partials(self):
        self.resumed += 1


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type, payload):
        self.events.append((event_type, payload))


class _FakeWavModule:
    """Подмена soundfile: записывает вызовы, создаёт файл-пустышку."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, int, int]] = []

    def write(self, path, data, samplerate):
        Path(path).write_bytes(b"RIFFfake")
        self.writes.append((str(path), int(getattr(data, "size", 0)), int(samplerate)))


def _diar_result(label="SPEAKER_00", direction=0):
    v = [0.0] * 8
    v[direction] = 1.0
    return {"segments": [{"start": 0.0, "end": 3.0, "speaker": label}],
            "speaker_embeddings": {label: v}}


def _make_service(tmp: str, enabled: bool = True, diarize=None,
                  settings_extra: dict | None = None):
    settings: dict[str, Any] = {
        "meeting_live_speakers_enabled": enabled,
        "meeting_diar_interval_sec": 90.0,
        "meeting_diar_window_sec": 90.0,
        "meeting_speaker_match_threshold": 0.72,
        "llm_brain_lease_enabled": False,
    }
    settings.update(settings_extra or {})
    bus = _FakeBus()
    core = _FakeRecordingCore()
    svc = MeetingSessionService(
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        recording_core=core,
        action_items_extractor=None,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=bus,
        diarize_window=diarize,
        data_dir=Path(tmp),
    )
    return svc, bus, core


class _SfPatchMixin(unittest.TestCase):
    """Обратимая подмена модульной _sf (урок «sys.modules-стаб без снятия»:
    невосстановленный модульный стаб отравляет соседей по CI-чанку)."""

    def setUp(self):
        super().setUp()
        import backend.meeting_session_service as mss
        self.fake_sf = _FakeWavModule()
        orig = mss._sf
        mss._sf = self.fake_sf
        self.addCleanup(lambda: setattr(mss, "_sf", orig))


class DiarSchedulingTests(_SfPatchMixin):
    def test_toggle_on_schedules_diar_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, *_ = _make_service(tmp, enabled=True, diarize=lambda p: _diar_result())
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            try:
                self.assertIn(MeetingJob.DIAR_WINDOW, svc._next_due)
                self.assertIsNotNone(svc._session.tracker)
            finally:
                svc.close()

    def test_toggle_off_is_byte_identical_c2a(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            svc, *_ = _make_service(tmp, enabled=False,
                                    diarize=lambda p: calls.append(p))
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            try:
                jobs = {k for k in svc._next_due if isinstance(k, MeetingJob)}
                self.assertEqual(jobs, {MeetingJob.CHUNK_STT, MeetingJob.ITEMS_LLM})
                self.assertIsNone(svc._session.tracker)
                svc._run_due_job_once(1e9)  # далёкое будущее: диар всё равно не зовётся
                self.assertEqual(calls, [])
            finally:
                svc.close()

    def test_job_interval_reads_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, *_ = _make_service(tmp, settings_extra={"meeting_diar_interval_sec": 61.0})
            self.assertEqual(svc._job_interval(MeetingJob.DIAR_WINDOW), 61.0)


class DiarJobTests(_SfPatchMixin):
    def _started(self, tmp, **kw):
        svc, bus, core = _make_service(tmp, **kw)
        self.assertTrue(svc.handle_meeting_start({})["ok"])
        return svc, bus, core

    def test_tick_produces_speakers_state_and_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=lambda p: _diar_result())
            try:
                svc._job_diar_window(svc._session)
                state = svc.handle_get_meeting_live_state({})
                self.assertEqual(len(state["speakers"]), 1)
                self.assertEqual(state["speakers"][0]["label"], "Спикер 1")
                self.assertAlmostEqual(state["speakers"][0]["talk_sec"], 3.0)
                self.assertFalse(state["degraded"]["diarization"])
                names = [e[0] for e in bus.events]
                self.assertIn("meeting.speakers_updated", names)
                self.assertEqual(core.paused, 1)
                self.assertEqual(core.resumed, 1)
                self.assertEqual(len(self.fake_sf.writes), 1)
            finally:
                svc.close()

    def test_temp_wav_removed_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            def boom(path):
                raise RuntimeError("pipeline упал")
            svc, bus, core = self._started(tmp, diarize=boom)
            try:
                svc._job_diar_window(svc._session)  # не должен поднять исключение
                state = svc.handle_get_meeting_live_state({})
                self.assertTrue(state["degraded"]["diarization"])
                self.assertEqual(core.resumed, 1)  # resume в finally
                leftovers = list((Path(tmp) / "tmp_meeting").glob("*.wav"))
                self.assertEqual(leftovers, [])
            finally:
                svc.close()

    def test_success_resets_degraded_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=lambda p: _diar_result())
            try:
                svc._session.degraded_diarization = True
                svc._job_diar_window(svc._session)
                self.assertFalse(
                    svc.handle_get_meeting_live_state({})["degraded"]["diarization"])
            finally:
                svc.close()

    def test_short_session_skips_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            svc, bus, core = self._started(
                tmp, diarize=lambda p: calls.append(p) or _diar_result())
            try:
                svc._recorder._duration = 3.0  # < минимума 5с
                svc._job_diar_window(svc._session)
                self.assertEqual(calls, [])
                self.assertEqual(self.fake_sf.writes, [])
            finally:
                svc.close()

    def test_no_diarize_callable_marks_degraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=None)
            try:
                # Первый тик — громкий WARN о недоступном коллаборатора
                # (анти-декоративная-проводка): тихо сломавшийся getattr в
                # service.py не должен деградировать спикеров без следа в логах.
                with self.assertLogs("krab_ear.backend", level="WARNING") as cm:
                    svc._job_diar_window(svc._session)
                self.assertIn("DIAR_WINDOW недоступен", cm.output[0])
                self.assertTrue(
                    svc.handle_get_meeting_live_state({})["degraded"]["diarization"])
                # Повторный тик той же сессии — БЕЗ повторного WARN
                # (лог только на переходе флага, не каждые 90с).
                with self.assertNoLogs("krab_ear.backend", level="WARNING"):
                    svc._job_diar_window(svc._session)
                self.assertTrue(
                    svc.handle_get_meeting_live_state({})["degraded"]["diarization"])
            finally:
                svc.close()

    def test_stale_session_tick_drops_mutation_and_event(self):
        # Fable-гейт Finding 2: воркер, переживший _stop_worker (join-таймаут на
        # лок-контеншене), не должен эмиттить meeting.speakers_updated ПОСЛЕ
        # meeting.finished и мутировать снятую сессию.
        with tempfile.TemporaryDirectory() as tmp:
            svc, bus, core = self._started(tmp, diarize=lambda p: _diar_result())
            try:
                stale = svc._session
                svc.handle_meeting_stop({})
                before = [e for e in bus.events if e[0] == "meeting.speakers_updated"]
                svc._job_diar_window(stale)  # протухший in-flight тик
                after = [e for e in bus.events if e[0] == "meeting.speakers_updated"]
                self.assertEqual(before, after)
                self.assertEqual(stale.speakers, [])
            finally:
                svc.close()

    def test_cross_window_stitching_accumulates(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = [_diar_result("SPEAKER_00", 0), _diar_result("SPEAKER_03", 0)]
            svc, bus, core = self._started(tmp, diarize=lambda p: results.pop(0))
            try:
                svc._job_diar_window(svc._session)
                svc._job_diar_window(svc._session)
                speakers = svc.handle_get_meeting_live_state({})["speakers"]
                self.assertEqual(len(speakers), 1)  # разные метки, один голос
                self.assertAlmostEqual(speakers[0]["talk_sec"], 6.0)
            finally:
                svc.close()
