"""Интеграция spill в AudioRecorder (R1 Task 2) — без sounddevice."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recorder import AudioRecorder  # noqa: E402


class FakeSpill:
    def __init__(self):
        self.appended = []
        self.closed = False
        self.discarded = False
        self.failed = False

    def append(self, chunk):
        self.appended.append(np.asarray(chunk).size)

    def close(self):
        self.closed = True

    def discard(self):
        self.discarded = True


class RecorderSpillTest(unittest.TestCase):
    def test_start_accepts_spill_kwarg_and_stores_it(self):
        r = AudioRecorder()
        spill = FakeSpill()
        # sd отсутствует в CI: воркер умрёт сразу, но start() обязан принять kwarg
        r.start(spill=spill)
        self.assertIs(r._spill, spill)
        r.abort(timeout_sec=0.5)

    def test_stop_closes_spill_never_discards(self):
        r = AudioRecorder()
        spill = FakeSpill()
        # Симулируем состояние «запись шла»: без реального воркера
        with r._lock:
            r._spill = spill
            r._is_recording = True
            r._chunks = [np.zeros(160, dtype=np.float32)]
            r._chunks_total_samples = 160
        result = r.stop(timeout_sec=0.5)
        self.assertIsNotNone(result)
        self.assertTrue(spill.closed)
        self.assertFalse(spill.discarded)
        self.assertIsNone(r._spill)

    def test_abort_closes_spill_keeps_files(self):
        r = AudioRecorder()
        spill = FakeSpill()
        with r._lock:
            r._spill = spill
            r._is_recording = True
        self.assertTrue(r.abort(timeout_sec=0.5))
        self.assertTrue(spill.closed)
        self.assertFalse(spill.discarded)
        self.assertIsNone(r._spill)

    def test_start_without_spill_backward_compatible(self):
        r = AudioRecorder()
        r.start()
        self.assertIsNone(r._spill)
        r.abort(timeout_sec=0.5)

    def test_thread_start_failure_rolls_back_state_and_closes_spill(self):
        """Неудача создания worker-а не публикует ложную запись."""
        recorder = AudioRecorder()
        spill = FakeSpill()

        with patch(
            "backend.recorder.threading.Thread.start",
            side_effect=RuntimeError("can't start new thread"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "can't start new thread",
            ):
                recorder.start(spill=spill)

        self.assertFalse(recorder.is_recording)
        self.assertIsNone(recorder._thread)
        self.assertIsNone(recorder._spill)
        self.assertTrue(spill.closed)
        self.assertIsNone(recorder.stop())


if __name__ == "__main__":
    unittest.main()
