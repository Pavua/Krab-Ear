"""Тесты для RealtimeSilenceFilter и вспомогательных утилит.

Импортирует только из backend.realtime_silence_filter, чтобы избежать
тяжёлых зависимостей engine.py (mlx_whisper, torch) в CI/test-окружении.
"""

import sys
import os
import time
import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRABEAR_ROOT not in sys.path:
    sys.path.insert(0, KRABEAR_ROOT)

from backend.realtime_silence_filter import (  # noqa: E402
    RealtimeSilenceFilter,
    _merge_ranges,
    zero_silence_ranges,
)

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000


def _make_silence(duration_sec: float) -> np.ndarray:
    """Возвращает пустой (нулевой) numpy-массив заданной длины."""
    return np.zeros(int(duration_sec * SAMPLE_RATE), dtype=np.float32)


def _make_speech(duration_sec: float) -> np.ndarray:
    """Возвращает синусоидальный сигнал, имитирующий речь (RMS > -40 dB)."""
    t = np.linspace(0, duration_sec, int(duration_sec * SAMPLE_RATE), dtype=np.float32)
    return (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


class FakeRecorder:
    """Stub-рекордер для тестов, не использует реальное аудиоустройство."""

    def __init__(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE):
        self._audio = audio
        self.sample_rate = sample_rate
        self.is_recording: bool = True

    def snapshot_audio(self, max_duration_sec: float):
        """Возвращает (audio_window, total_duration)."""
        n = min(len(self._audio), int(max_duration_sec * self.sample_rate))
        window = self._audio[-n:] if n > 0 else np.zeros(0, dtype=np.float32)
        total_duration = len(self._audio) / self.sample_rate
        return window, total_duration


# ---------------------------------------------------------------------------
# TestMergeRanges
# ---------------------------------------------------------------------------
class TestMergeRanges(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_merge_ranges([]), [])

    def test_single(self):
        self.assertEqual(_merge_ranges([(1.0, 3.0)]), [(1.0, 3.0)])

    def test_no_overlap(self):
        result = _merge_ranges([(0.0, 1.0), (2.0, 3.0)])
        self.assertEqual(result, [(0.0, 1.0), (2.0, 3.0)])

    def test_overlapping(self):
        result = _merge_ranges([(0.0, 2.0), (1.5, 4.0)])
        self.assertEqual(result, [(0.0, 4.0)])

    def test_adjacent(self):
        result = _merge_ranges([(0.0, 1.0), (1.0, 2.0)])
        self.assertEqual(result, [(0.0, 2.0)])

    def test_unsorted_input(self):
        result = _merge_ranges([(3.0, 4.0), (0.0, 1.0), (0.5, 2.0)])
        self.assertEqual(result, [(0.0, 2.0), (3.0, 4.0)])

    def test_multiple_merges(self):
        result = _merge_ranges([(0.0, 1.0), (0.5, 1.5), (1.4, 3.0)])
        self.assertEqual(result, [(0.0, 3.0)])


# ---------------------------------------------------------------------------
# TestRealtimeSilenceFilterDisabled
# ---------------------------------------------------------------------------
class TestRealtimeSilenceFilterDisabled(unittest.TestCase):
    def _make_filter(self, enabled=False):
        recorder = FakeRecorder(_make_silence(10.0))
        settings = {
            "realtime_silence_filter_enabled": enabled,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        return RealtimeSilenceFilter(recorder, settings)

    def test_disabled_by_default(self):
        rsf = RealtimeSilenceFilter(FakeRecorder(_make_silence(5.0)), {})
        self.assertFalse(rsf.enabled)

    def test_start_does_nothing_when_disabled(self):
        rsf = self._make_filter(enabled=False)
        rsf.start()
        self.assertIsNone(rsf._thread)
        rsf.stop()


# ---------------------------------------------------------------------------
# TestRealtimeSilenceFilterLifecycle
# ---------------------------------------------------------------------------
class TestRealtimeSilenceFilterLifecycle(unittest.TestCase):
    def _make_filter(self, audio: np.ndarray, check_sec: float = 0.05):
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": check_sec,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        return RealtimeSilenceFilter(recorder, settings)

    def test_thread_starts(self):
        rsf = self._make_filter(_make_speech(5.0))
        rsf.start()
        self.assertIsNotNone(rsf._thread)
        self.assertTrue(rsf._thread.is_alive())
        rsf.stop()

    def test_stop_joins_thread(self):
        rsf = self._make_filter(_make_speech(5.0))
        rsf.start()
        rsf.stop()
        self.assertIsNone(rsf._thread)

    def test_start_is_idempotent(self):
        rsf = self._make_filter(_make_speech(5.0))
        rsf.start()
        thread1 = rsf._thread
        rsf.start()
        thread2 = rsf._thread
        self.assertIs(thread1, thread2)
        rsf.stop()


# ---------------------------------------------------------------------------
# TestSilenceDetection
# ---------------------------------------------------------------------------
class TestSilenceDetection(unittest.TestCase):
    def _run_filter(self, audio: np.ndarray, max_silence_sec: float = 8.0):
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.02,
            "rt_silence_window_sec": 30.0,
            "rt_silence_max_sec": max_silence_sec,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.5)
        return rsf.stop()

    def test_detects_long_silence(self):
        audio = _make_silence(10.0)
        ranges = self._run_filter(audio, max_silence_sec=8.0)
        self.assertGreater(len(ranges), 0)

    def test_no_false_positive_on_speech(self):
        audio = _make_speech(10.0)
        ranges = self._run_filter(audio, max_silence_sec=8.0)
        self.assertEqual(len(ranges), 0)

    def test_short_silence_below_threshold(self):
        audio = _make_silence(3.0)
        ranges = self._run_filter(audio, max_silence_sec=8.0)
        self.assertEqual(len(ranges), 0)


# ---------------------------------------------------------------------------
# TestEventEmission
# ---------------------------------------------------------------------------
class TestEventEmission(unittest.TestCase):
    def test_event_emitted_on_long_silence(self):
        events = []

        def fake_emit(event_type, payload):
            events.append((event_type, payload))

        recorder = FakeRecorder(_make_silence(10.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings, event_bus_emit=fake_emit)
        rsf.start()
        time.sleep(0.2)
        rsf.stop()

        self.assertGreater(len(events), 0)
        et, payload = events[0]
        self.assertEqual(et, "recording.silence_detected")
        self.assertIn("total_silence_sec", payload)
        self.assertIn("ranges_count", payload)

    def test_no_event_on_speech(self):
        events = []

        def fake_emit(event_type, payload):
            events.append((event_type, payload))

        recorder = FakeRecorder(_make_speech(10.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings, event_bus_emit=fake_emit)
        rsf.start()
        time.sleep(0.2)
        rsf.stop()

        self.assertEqual(len(events), 0)


# ---------------------------------------------------------------------------
# TestMultipleSilenceRanges
# ---------------------------------------------------------------------------
class TestMultipleSilenceRanges(unittest.TestCase):
    def test_get_ranges_without_stop(self):
        recorder = FakeRecorder(_make_silence(10.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.2)
        ranges_mid = rsf.get_silence_ranges()
        rsf.stop()
        self.assertIsInstance(ranges_mid, list)

    def test_ranges_are_merged_no_duplicates(self):
        audio = _make_silence(10.0)
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.3)
        ranges = rsf.stop()

        for i in range(len(ranges) - 1):
            self.assertLessEqual(ranges[i][1], ranges[i + 1][0],
                                 "Overlapping ranges found — merge failed")


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------
class TestEdgeCases(unittest.TestCase):
    def test_empty_buffer(self):
        recorder = FakeRecorder(np.zeros(0, dtype=np.float32))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.15)
        ranges = rsf.stop()
        self.assertEqual(ranges, [])

    def test_not_recording_skips_check(self):
        recorder = FakeRecorder(_make_silence(10.0))
        recorder.is_recording = False
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.15)
        ranges = rsf.stop()
        self.assertEqual(ranges, [])

    def test_stop_clears_thread_reference(self):
        recorder = FakeRecorder(_make_speech(5.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        rsf.stop()
        self.assertIsNone(rsf._thread)


# ---------------------------------------------------------------------------
# TestZeroSilenceRangesHelper
# ---------------------------------------------------------------------------
class TestZeroSilenceRangesHelper(unittest.TestCase):
    def test_zeros_applied(self):
        audio = _make_speech(2.0)
        ranges = [(0.5, 1.0)]
        result = zero_silence_ranges(audio, ranges, sample_rate=SAMPLE_RATE)
        s = int(0.5 * SAMPLE_RATE)
        e = int(1.0 * SAMPLE_RATE)
        self.assertTrue(np.all(result[s:e] == 0), "Samples in range should be zeroed")
        self.assertFalse(np.all(result[:s] == 0), "Samples before range should not be zeroed")

    def test_original_not_modified(self):
        audio = _make_speech(2.0)
        original_copy = audio.copy()
        zero_silence_ranges(audio, [(0.5, 1.0)], sample_rate=SAMPLE_RATE)
        np.testing.assert_array_equal(audio, original_copy)

    def test_empty_ranges_returns_original(self):
        audio = _make_speech(2.0)
        result = zero_silence_ranges(audio, [], sample_rate=SAMPLE_RATE)
        self.assertIs(result, audio)

    def test_out_of_bounds_ranges_clipped(self):
        audio = _make_speech(1.0)
        result = zero_silence_ranges(audio, [(0.5, 999.0)], sample_rate=SAMPLE_RATE)
        s = int(0.5 * SAMPLE_RATE)
        self.assertTrue(np.all(result[s:] == 0))
        self.assertEqual(len(result), len(audio))


class TestRealtimeSilenceFilterWave145(unittest.TestCase):
    """Wave 145 — required named tests."""

    # ------------------------------------------------------------------
    # test_suppress_during_silence
    # ------------------------------------------------------------------
    def test_suppress_during_silence(self):
        """Фильтр обнаруживает диапазоны тишины и накапливает их."""
        recorder = FakeRecorder(_make_silence(15.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 15.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.25)
        ranges = rsf.stop()
        # Должны быть диапазоны тишины для 15-секундного пустого буфера
        self.assertGreater(len(ranges), 0, "Должны быть диапазоны тишины")
        for s, e in ranges:
            self.assertLess(s, e, "start должен быть меньше end")

    # ------------------------------------------------------------------
    # test_allow_during_speech
    # ------------------------------------------------------------------
    def test_allow_during_speech(self):
        """На речевом сигнале диапазоны тишины не накапливаются."""
        recorder = FakeRecorder(_make_speech(12.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 12.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.25)
        ranges = rsf.stop()
        self.assertEqual(ranges, [], "На речи не должно быть диапазонов тишины")

    # ------------------------------------------------------------------
    # test_adaptive_threshold
    # ------------------------------------------------------------------
    def test_adaptive_threshold(self):
        """При высоком пороге тишины (1 сек) даже короткая тишина засчитывается."""
        recorder = FakeRecorder(_make_silence(10.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 1.0,  # очень низкий порог → всё тихое попадёт
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.25)
        ranges = rsf.stop()
        self.assertGreater(len(ranges), 0,
                           "С низким порогом тишины должны быть диапазоны")

    # ------------------------------------------------------------------
    # test_empty_chunk_handled
    # ------------------------------------------------------------------
    def test_empty_chunk_handled(self):
        """Пустой аудиобуфер не вызывает исключений и не даёт диапазонов."""
        recorder = FakeRecorder(np.zeros(0, dtype=np.float32))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(0.2)
        ranges = rsf.stop()
        self.assertEqual(ranges, [])

    # ------------------------------------------------------------------
    # test_concurrent_filter
    # ------------------------------------------------------------------
    def test_concurrent_filter(self):
        """get_silence_ranges() потокобезопасен при конкурентных вызовах."""
        import threading

        recorder = FakeRecorder(_make_silence(15.0))
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.05,
            "rt_silence_window_sec": 15.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()

        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(20):
                    _ = rsf.get_silence_ranges()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        rsf.stop()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")


if __name__ == "__main__":
    unittest.main()
