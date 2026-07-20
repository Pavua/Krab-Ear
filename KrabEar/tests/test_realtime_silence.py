"""Тесты для RealtimeSilenceFilter и вспомогательных утилит.

Импортирует только из backend.realtime_silence_filter, чтобы избежать
тяжёлых зависимостей engine.py (mlx_whisper, torch) в CI/test-окружении.
"""

import sys
import os
import threading
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
            "rt_silence_check_sec": 0.5,
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

    def test_stop_timeout_preserves_handle_and_blocks_restart(self):
        """При timeout RSF сохраняет worker и не очищает его общий Event."""
        rsf = self._make_filter(_make_speech(5.0))
        release = threading.Event()
        stuck_thread = threading.Thread(target=release.wait, daemon=True)
        with rsf._lock:
            rsf._thread = stuck_thread
        stuck_thread.start()
        self.addCleanup(stuck_thread.join, 1.0)
        self.addCleanup(release.set)

        rsf.stop(timeout_sec=0.01)
        self.assertTrue(rsf.is_running)
        self.assertIs(rsf._thread, stuck_thread)
        self.assertTrue(rsf._stop_event.is_set())

        rsf.start()
        self.assertIs(rsf._thread, stuck_thread)
        self.assertTrue(rsf._stop_event.is_set())

        release.set()
        stuck_thread.join(timeout=1.0)
        rsf.stop(timeout_sec=0.1)
        self.assertFalse(rsf.is_running)
        self.assertIsNone(rsf._thread)


# ---------------------------------------------------------------------------
# TestSilenceDetection
# ---------------------------------------------------------------------------
class TestSilenceDetection(unittest.TestCase):
    def _run_filter_direct(self, audio: np.ndarray, max_silence_sec: float = 8.0):
        """Drive _check_once() directly — no thread, no wall-clock sleep.

        wave-34 clamps rt_silence_check_sec to max(0.5, value), so the old
        pattern of ``time.sleep(0.5)`` was only barely ≥1 check interval even
        on an unloaded machine.  Under -P4 parallel CI (4 concurrent pytest
        processes) the background thread frequently did not fire before
        ``stop()`` was called, causing ``assertGreater(len(ranges), 0)`` to
        fail with 0.  Calling ``_check_once()`` directly is deterministic and
        still exercises the full detection logic without racing the scheduler.
        """
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 60.0,  # disable auto-tick; we call manually
            "rt_silence_window_sec": 30.0,
            "rt_silence_max_sec": max_silence_sec,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        return rsf.get_silence_ranges()

    def test_detects_long_silence(self):
        audio = _make_silence(10.0)
        ranges = self._run_filter_direct(audio, max_silence_sec=8.0)
        self.assertGreater(len(ranges), 0)

    def test_no_false_positive_on_speech(self):
        audio = _make_speech(10.0)
        ranges = self._run_filter_direct(audio, max_silence_sec=8.0)
        self.assertEqual(len(ranges), 0)

    def test_short_silence_below_threshold(self):
        audio = _make_silence(3.0)
        ranges = self._run_filter_direct(audio, max_silence_sec=8.0)
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
            # Use a large check_sec so the auto-tick never fires; call _check_once()
            # directly to avoid a wall-clock race under -P4 parallel CI.
            "rt_silence_check_sec": 60.0,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings, event_bus_emit=fake_emit)
        rsf._check_once()

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
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings, event_bus_emit=fake_emit)
        rsf.start()
        time.sleep(1.2)
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
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(1.2)
        ranges_mid = rsf.get_silence_ranges()
        rsf.stop()
        self.assertIsInstance(ranges_mid, list)

    def test_ranges_are_merged_no_duplicates(self):
        audio = _make_silence(10.0)
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.5,
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
            "rt_silence_check_sec": 0.5,
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
            "rt_silence_check_sec": 0.5,
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
            "rt_silence_check_sec": 0.5,
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
            # Disable auto-tick; drive deterministically via _check_once()
            # to avoid wall-clock race under -P4 parallel CI.
            "rt_silence_check_sec": 60.0,
            "rt_silence_window_sec": 15.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        ranges = rsf.get_silence_ranges()
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
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 12.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(1.2)
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
            # Disable auto-tick; drive deterministically via _check_once()
            # to avoid wall-clock race under -P4 parallel CI.
            "rt_silence_check_sec": 60.0,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 1.0,  # очень низкий порог → всё тихое попадёт
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        ranges = rsf.get_silence_ranges()
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
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf.start()
        time.sleep(1.2)
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
            "rt_silence_check_sec": 0.5,
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
        time.sleep(1.2)
        rsf.stop()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Ошибки в потоках: {errors}")


# ---------------------------------------------------------------------------
# TestCursorAdvanceBugW1325F2  (W1330 regression tests)
# ---------------------------------------------------------------------------
class TestCursorAdvanceBugW1325F2(unittest.TestCase):
    """W1330 — verify the cursor is NOT advanced on early-return (W1325 F2 HIGH).

    The W1140 bug advanced _checked_up_to_sec to total_duration BEFORE the
    ``total_silence < _max_silence_sec`` early-return check.  On the common
    path (no silence) the cursor jumped forward and the next tick found
    ``skip_samples >= audio_window.size`` → returned immediately → filter
    effectively stopped scanning after the first non-silence tick.

    These three tests verify the corrected behaviour.
    """

    # ------------------------------------------------------------------
    # test_cursor_not_advanced_on_early_return
    # ------------------------------------------------------------------
    def test_cursor_not_advanced_on_early_return(self):
        """_checked_up_to_sec stays at 0 when total_silence < _max_silence_sec."""
        recorder = FakeRecorder(_make_speech(10.0))  # speech → no silence detected
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()  # invoke directly — speech path should early-return

        # Cursor must NOT have advanced; the window will be re-examined next tick.
        self.assertEqual(
            rsf._checked_up_to_sec,
            0.0,
            "_checked_up_to_sec must stay at 0 when early-return fires (no silence)",
        )

    # ------------------------------------------------------------------
    # test_cursor_advanced_on_silence_detection
    # ------------------------------------------------------------------
    def test_cursor_advanced_on_silence_detection(self):
        """_checked_up_to_sec is advanced to total_duration when long silence found."""
        audio = _make_silence(10.0)
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 8.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()

        total_duration = len(audio) / SAMPLE_RATE
        self.assertAlmostEqual(
            rsf._checked_up_to_sec,
            total_duration,
            places=2,
            msg="_checked_up_to_sec must advance to total_duration after silence detected",
        )

    # ------------------------------------------------------------------
    # test_subsequent_ticks_continue_scanning
    # ------------------------------------------------------------------
    def test_subsequent_ticks_continue_scanning(self):
        """After a no-silence tick, the next tick can still detect new silence.

        This is the core regression guard: with the W1140 bug, the second
        _check_once call would see skip_samples >= audio_window.size and return
        immediately even after new silence was appended.
        """
        # First tick: speech-only buffer → no silence, cursor stays at 0.
        speech = _make_speech(5.0)
        recorder = FakeRecorder(speech)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 0.5,
            "rt_silence_window_sec": 20.0,
            "rt_silence_max_sec": 4.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        self.assertEqual(rsf._checked_up_to_sec, 0.0, "Cursor must stay 0 after speech tick")

        # Second tick: recorder now returns silence-heavy audio.
        recorder._audio = _make_silence(10.0)
        rsf._check_once()

        ranges = rsf.get_silence_ranges()
        self.assertGreater(
            len(ranges),
            0,
            "Filter must detect silence on second tick after a no-silence first tick",
        )


# ---------------------------------------------------------------------------
# TestW1136Fixes — W1136 F1 + F2 HIGH
# ---------------------------------------------------------------------------
class TestW1136Fixes(unittest.TestCase):
    """W1136 F1+F2 HIGH — settings-driven threshold + _checked_up_to_sec activation."""

    # ------------------------------------------------------------------
    # F2: threshold_from_settings
    # ------------------------------------------------------------------
    def test_threshold_from_settings(self):
        """_threshold_db должен браться из settings, а не быть захардкожен."""
        recorder = FakeRecorder(_make_silence(5.0))
        custom_threshold = -30.0
        settings = {
            "realtime_silence_filter_enabled": True,
            "realtime_silence_threshold_db": custom_threshold,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        self.assertAlmostEqual(
            rsf._threshold_db, custom_threshold,
            msg="_threshold_db должен читаться из settings['realtime_silence_threshold_db']",
        )

    def test_threshold_default_when_not_in_settings(self):
        """wave1531 changed default to -55 dBFS (PRESERVE_WHISPER)."""
        recorder = FakeRecorder(_make_silence(5.0))
        rsf = RealtimeSilenceFilter(recorder, {})
        from backend.realtime_silence_filter import _DEFAULT_THRESHOLD_DB
        self.assertAlmostEqual(rsf._threshold_db, _DEFAULT_THRESHOLD_DB)

    # ------------------------------------------------------------------
    # F1: _checked_up_to_sec skips already-analyzed prefix
    # ------------------------------------------------------------------
    def test_checked_up_to_sec_skips_prefix(self):
        """После первого _check_once _checked_up_to_sec == total_duration,
        и второй вызов пропускает уже проанализированный префикс (skip_samples > 0).
        """
        # 10s silence — длиннее window_sec чтобы total_duration > window
        audio = _make_silence(15.0)
        recorder = FakeRecorder(audio)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 60.0,   # prevent auto-tick
            "rt_silence_window_sec": 10.0,
            "rt_silence_max_sec": 1.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)

        # Manually call _check_once to simulate first tick
        rsf._check_once()

        first_checked = rsf._checked_up_to_sec
        total_dur = len(audio) / SAMPLE_RATE  # 15.0
        self.assertAlmostEqual(
            first_checked, total_dur, places=1,
            msg="_checked_up_to_sec должен быть обновлён до total_duration после первого тика",
        )

        # On second call with same recorder state (total_duration unchanged),
        # skip_samples should equal or exceed the audio_window size → no new work.
        # Capture call count via a counter on detect_silence.
        call_count = [0]
        original_detect = rsf._detector.detect_silence

        def counting_detect(audio_arr, sr, **kw):
            call_count[0] += 1
            return original_detect(audio_arr, sr, **kw)

        rsf._detector.detect_silence = counting_detect
        rsf._check_once()

        self.assertEqual(
            call_count[0], 0,
            "Второй тик с теми же данными не должен вызывать detect_silence — "
            "весь префикс уже проанализирован.",
        )


# ---------------------------------------------------------------------------
# TestW1769SampleTimeAnchor — wall-clock vs sample-count drift
# ---------------------------------------------------------------------------
class _DriftRecorder:
    """Стаб-рекордер, в котором wall-clock РАСХОДИТСЯ с числом семплов.

    Имитирует реальный ``AudioRecorder``: ``snapshot_audio`` отдаёт хвост
    буфера (семплы) + НАСТЕННОЕ время записи (``time.monotonic()`` дрейф),
    а ``_chunks_total_samples`` — истинное число буферизованных семплов.

    Сценарий: процессинг-стол / задержка / потеря аудио-кадров → wall-clock
    ушёл вперёд относительно реально записанных семплов.
    """

    def __init__(
        self,
        audio: np.ndarray,
        wallclock_duration_sec: float,
        sample_rate: int = SAMPLE_RATE,
    ):
        self._audio = audio
        self.sample_rate = sample_rate
        self.is_recording: bool = True
        # Истинное число буферизованных семплов (как O(1)-счётчик рекордера).
        self._chunks_total_samples = int(audio.size)
        # Настенное время — намеренно БОЛЬШЕ, чем audio.size / sr (дрейф вперёд).
        self._wallclock_duration_sec = float(wallclock_duration_sec)

    def snapshot_audio(self, max_duration_sec: float):
        n = min(len(self._audio), int(max_duration_sec * self.sample_rate))
        window = self._audio[-n:] if n > 0 else np.zeros(0, dtype=np.float32)
        # Возвращаем ИМЕННО wall-clock duration (а не len/sr) — здесь и кроется
        # рассинхрон, который ломал прежний якорь окна.
        return window, self._wallclock_duration_sec


class TestW1769SampleTimeAnchor(unittest.TestCase):
    """W1769 — диапазоны тишины якорятся к семпл-времени, не к wall-clock.

    Регрессия: ``_check_once`` строил ``window_start_sec`` из ``total_duration``
    (настенные часы), но ``detect_silence`` даёт смещения в семпл-времени, а
    ``zero_silence_ranges`` обнуляет по индексу семплов. При дрейфе настенных
    часов вперёд относительно числа буферизованных семплов диапазоны уезжали
    и обнуляли РЕАЛЬНУЮ речь (или промахивались мимо тишины).
    """

    # Раскладка буфера: [тишина 0–10с) + [речь 10–20с). Окно = весь буфер.
    # При дрейфе wall-clock=25с прежний якорь = 25-20 = 5с → диапазон [5,15],
    # что обнуляет семплы [80000,240000] → первые 5с РЕЧИ (160000..240000)!
    def _build_silence_then_speech(self):
        silence = _make_silence(10.0)
        speech = _make_speech(10.0)
        return np.concatenate([silence, speech]).astype(np.float32)

    def test_drift_does_not_zero_real_speech(self):
        """fail-before/pass-after: дрейф wall-clock НЕ обнуляет реальную речь."""
        buffer = self._build_silence_then_speech()  # 20с, 320000 семплов
        speech_start = int(10.0 * SAMPLE_RATE)  # 160000

        # Wall-clock ушёл на 5с вперёд относительно реально записанных семплов.
        recorder = _DriftRecorder(buffer, wallclock_duration_sec=25.0)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 60.0,  # без авто-тика — зовём вручную
            "rt_silence_window_sec": 20.0,  # окно = весь буфер
            "rt_silence_max_sec": 4.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        ranges = rsf.get_silence_ranges()

        self.assertGreater(len(ranges), 0, "Тишина 10с должна быть обнаружена")

        # Применяем маску к ИТОГОВОМУ буферу (как делает engine.transcribe).
        masked = zero_silence_ranges(buffer, ranges, sample_rate=SAMPLE_RATE)

        # Главное утверждение: НАЧАЛО речи (10–13с) сохранено. Прежний баг давал
        # диапазон ≈[5,15] и обнулял семплы [80000,240000] → первые ~5с РЕЧИ
        # (160000..240000) уничтожались. ``np.max`` по всему хвосту это пропускал
        # (вторая половина речи выживала), поэтому проверяем ИМЕННО начало речи.
        early_speech = masked[speech_start:speech_start + int(3.0 * SAMPLE_RATE)]
        self.assertGreater(
            float(np.max(np.abs(early_speech))),
            0.1,
            "НАЧАЛО реальной речи (10–13с) не должно обнуляться при дрейфе wall-clock",
        )
        # Вся речь целиком тоже не должна быть обнулена в среднем (sanity).
        speech_after = masked[speech_start:]
        nonzero_ratio = float(np.count_nonzero(speech_after)) / float(speech_after.size)
        self.assertGreater(
            nonzero_ratio, 0.95,
            "Подавляющее большинство семплов речи должно сохраниться",
        )
        # И тишина (0–10с) действительно подавлена (поведение сохранено).
        silence_after = masked[:speech_start]
        self.assertTrue(
            np.all(silence_after == 0),
            "Диапазон тишины должен быть обнулён (suppression сохранён)",
        )
        # Диапазоны должны лежать в области тишины [0,10], а не уезжать в речь [5,15].
        for s, e in ranges:
            self.assertLess(
                e, 10.0 + 0.05,
                f"end={e} должен оставаться в зоне тишины [0,10], а не залезать в речь",
            )

    def test_aligned_case_still_suppresses_silence(self):
        """Контроль: при совпадении wall-clock и семплов тишина подавляется."""
        buffer = self._build_silence_then_speech()  # тишина[0,10)+речь[10,20)
        speech_start = int(10.0 * SAMPLE_RATE)

        # Aligned: wall-clock == len(audio)/sr == 20.0.
        recorder = _DriftRecorder(buffer, wallclock_duration_sec=20.0)
        settings = {
            "realtime_silence_filter_enabled": True,
            "rt_silence_check_sec": 60.0,
            "rt_silence_window_sec": 20.0,
            "rt_silence_max_sec": 4.0,
        }
        rsf = RealtimeSilenceFilter(recorder, settings)
        rsf._check_once()
        ranges = rsf.get_silence_ranges()

        self.assertGreater(len(ranges), 0, "Тишина должна быть обнаружена")
        masked = zero_silence_ranges(buffer, ranges, sample_rate=SAMPLE_RATE)
        # Тишина обнулена, речь сохранена.
        self.assertTrue(np.all(masked[:speech_start] == 0))
        self.assertGreater(float(np.max(np.abs(masked[speech_start:]))), 0.1)


if __name__ == "__main__":
    unittest.main()
