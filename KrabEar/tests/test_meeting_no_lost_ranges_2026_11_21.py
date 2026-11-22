"""KE05C-10 / KE05C-07: встреча не теряет аудио и текст.

* ``transcribe_preview`` при занятом GPU отвечает ``{"text": "", "skipped": "mlx_busy"}``;
  CHUNK_STT всё равно продвигал курсор — диапазон аудио терялся навсегда.
* При приросте транскрипта > окна (12 000 символов) в экстрактор уходил только хвост, а
  ``last_extract_len`` сдвигался к концу — середина никогда не обрабатывалась.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import backend.meeting_session_service as mss  # noqa: E402
from backend.meeting_session_service import MeetingSessionService  # noqa: E402


class _Recorder:
    sample_rate = 16000
    is_recording = True

    def __init__(self) -> None:
        self.duration = 100.0
        self.ranges: list[tuple[float, float]] = []

    def get_duration_sec(self):
        return self.duration

    def snapshot_range(self, a, b):
        self.ranges.append((round(a, 2), round(b, 2)))
        return np.ones(max(0, int((b - a) * 16000)), dtype=np.float32)


class _Transcriber:
    def __init__(self, replies):
        self.replies = list(replies)

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return self.replies.pop(0)


class _Core:
    def handle_start_recording(self, params):
        return {"status": "recording", "is_recording": True}

    def handle_stop_recording(self, params):
        return {"history_id": "h"}

    def pause_realtime_partials(self):
        pass

    def resume_realtime_partials(self):
        pass


class _Bus:
    def __init__(self):
        self.events = []

    def emit(self, name, payload):
        self.events.append((name, payload))


class _Extractor:
    def __init__(self):
        self.windows: list[str] = []

    def extract(self, text, language="ru"):
        self.windows.append(text)
        return SimpleNamespace(ok=True, action_items=[], decisions=[], questions=[])


def _service(transcriber, extractor=None):
    rec = _Recorder()
    settings = {
        "meeting_live_speakers_enabled": False,
        "llm_brain_lease_enabled": False,
    }
    svc = MeetingSessionService(
        recorder=rec,
        transcriber=transcriber,
        recording_core=_Core(),
        action_items_extractor=extractor,
        settings_get=lambda k, d=None: settings.get(k, d),
        event_bus=_Bus(),
        diarize_window=None,
        data_dir=None,
    )
    return svc, rec


class ChunkCursorNotAdvancedOnSkip(unittest.TestCase):
    def test_busy_gpu_keeps_cursor_and_retries_same_range(self):
        svc, rec = _service(_Transcriber([{"text": "", "skipped": "mlx_busy"}, {"text": "привет мир"}]))
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            start = s.cursor_sec
            svc._job_chunk_stt(s)
            self.assertEqual(s.cursor_sec, start, "курсор продвинут при пропущенной расшифровке")
            rec.duration = 130.0
            svc._job_chunk_stt(s)
            # повторная попытка снимает ТОТ ЖЕ начальный диапазон, расширенный до новой длительности
            self.assertEqual(rec.ranges[-1][0], round(start, 2))
            self.assertEqual(s.cursor_sec, 130.0)
            self.assertIn("привет мир", "".join(s.chunks))
        finally:
            svc.close()

    def test_error_payload_also_keeps_cursor(self):
        svc, rec = _service(_Transcriber([{"text": "", "error": "engine_failed"}]))
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            start = s.cursor_sec
            svc._job_chunk_stt(s)
            self.assertEqual(s.cursor_sec, start)
        finally:
            svc.close()

    def test_genuine_silence_still_advances_cursor(self):
        # пустой текст БЕЗ маркера пропуска — реальная тишина: курсор обязан идти вперёд
        svc, rec = _service(_Transcriber([{"text": ""}]))
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            svc._job_chunk_stt(s)
            self.assertEqual(s.cursor_sec, 100.0)
        finally:
            svc.close()


class ItemsWindowCoversTheWholeGrowth(unittest.TestCase):
    def test_growth_larger_than_window_is_processed_in_consecutive_windows(self):
        ex = _Extractor()
        svc, _rec = _service(_Transcriber([]), extractor=ex)
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            # 30 000 символов, каждый 1000-символьный блок помечен своим маркером
            blocks = [f"M{i:02d}" + "x" * 997 for i in range(30)]
            s.chunks = [b for b in blocks]
            s.transcript_len = sum(len(b) for b in blocks)
            for _ in range(10):
                svc._job_items_llm(s)
                if s.last_extract_len >= s.transcript_len:
                    break
            seen = "\n".join(ex.windows)
            missing = [f"M{i:02d}" for i in range(30) if f"M{i:02d}" not in seen]
            self.assertEqual(missing, [], f"блоки не попали ни в одно окно: {missing}")
            self.assertEqual(s.last_extract_len, s.transcript_len)
            self.assertTrue(all(len(w) <= mss._ITEMS_WINDOW_CHARS for w in ex.windows))
        finally:
            svc.close()

    def test_small_growth_keeps_tail_window_behaviour(self):
        ex = _Extractor()
        svc, _rec = _service(_Transcriber([]), extractor=ex)
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            s.chunks = ["a" * 5000]
            s.transcript_len = 5000
            svc._job_items_llm(s)
            self.assertEqual(len(ex.windows), 1)
            self.assertEqual(ex.windows[0], "a" * 5000)
            self.assertEqual(s.last_extract_len, 5000)
        finally:
            svc.close()

    def test_regular_long_meeting_still_sends_tail_with_context(self):
        ex = _Extractor()
        svc, _rec = _service(_Transcriber([]), extractor=ex)
        try:
            self.assertTrue(svc.handle_meeting_start({})["ok"])
            s = svc._session
            s.chunks = ["a" * 20000]
            s.transcript_len = 20000
            s.last_extract_len = 19500  # прирост 500 < окна: берём последние 12 000 (контекст)
            s.chunks.append("b" * 500)
            s.transcript_len = 20500
            svc._job_items_llm(s)
            self.assertEqual(len(ex.windows[0]), mss._ITEMS_WINDOW_CHARS)
            self.assertTrue(ex.windows[0].endswith("b" * 500))
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
