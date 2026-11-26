"""Tests for AudioRecorder bounded buffer + snapshot tail optimization (W1327 F1 HIGH / W1331).

Проверяет:
1. _chunks_total_samples — O(1) счётчик точно отражает накопленные семплы.
2. MAX_RECORDING_SAMPLES cap останавливает запись по достижении лимита.
3. snapshot_audio() с tail-walk не конкатенирует весь буфер при запросе малого окна.
"""
from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import patch, PropertyMock

import numpy as np

# Path setup
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_ROOT not in sys.path:
    sys.path.insert(0, KRAB_ROOT)

from backend.recorder import AudioRecorder, MAX_RECORDING_SAMPLES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(n_samples: int, value: float = 0.5) -> np.ndarray:
    """Возвращает 2-D чанк формата (n_samples, 1) как sounddevice."""
    return np.full((n_samples, 1), value, dtype=np.float32)


def _inject_chunks(recorder: AudioRecorder, chunks: list[np.ndarray]) -> None:
    """Вставляет чанки напрямую в _chunks + обновляет счётчик (bypass worker)."""
    with recorder._lock:
        recorder._chunks = list(chunks)
        recorder._chunks_total_samples = sum(c.reshape(-1).size for c in chunks)


# ---------------------------------------------------------------------------
# Test: _chunks_total_samples counter accurate
# ---------------------------------------------------------------------------

class TestChunksTotalSamplesCounter(unittest.TestCase):

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_counter_zero_after_init(self):
        self.assertEqual(self.recorder._chunks_total_samples, 0)

    def test_counter_reset_on_start(self):
        """start() must reset counter even if previous recording left residue."""
        # Simulate leftover state
        with self.recorder._lock:
            self.recorder._chunks_total_samples = 999
        # Patch _worker so it doesn't really run
        with patch.object(self.recorder, '_worker'):
            self.recorder.start()
        self.assertEqual(self.recorder._chunks_total_samples, 0)

    def test_counter_accurate_after_inject(self):
        chunks = [_make_chunk(1600), _make_chunk(3200), _make_chunk(800)]
        _inject_chunks(self.recorder, chunks)
        expected = 1600 + 3200 + 800
        self.assertEqual(self.recorder._chunks_total_samples, expected)

    def test_counter_reset_on_stop(self):
        """stop() must clear _chunks_total_samples after drain."""
        chunks = [_make_chunk(4800)]
        _inject_chunks(self.recorder, chunks)
        # Manually set recording flag so stop() proceeds
        with self.recorder._lock:
            self.recorder._is_recording = True
        # set stop event immediately so thread join is instant
        self.recorder._stop_event.set()
        result = self.recorder.stop(timeout_sec=0.1)
        self.assertIsNotNone(result)
        self.assertEqual(self.recorder._chunks_total_samples, 0)

    def test_chunks_total_samples_counter_accurate(self):
        """Comprehensive: counter sums all injected chunks."""
        n_chunks = 10
        samples_per = 1600
        chunks = [_make_chunk(samples_per) for _ in range(n_chunks)]
        _inject_chunks(self.recorder, chunks)
        self.assertEqual(self.recorder._chunks_total_samples, n_chunks * samples_per)


# ---------------------------------------------------------------------------
# Test: 4-hour cap stops recording
# ---------------------------------------------------------------------------

class TestChunksCappedAt4Hours(unittest.TestCase):

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_max_recording_samples_constant_equals_4_hours(self):
        expected = 16000 * 60 * 60 * 4
        self.assertEqual(MAX_RECORDING_SAMPLES, expected)

    def test_chunks_capped_at_4_hours(self):
        """Реальный _worker останавливает запись на глобальном 4-часовом потолке.

        G3 audio-02 A02-01: прежний тест копировал if из _worker и проверял сам
        себя. Счётчик подкручен до потолка, поэтому реальный буфер ~1 чанк.
        """
        chunk_size = self.recorder.chunk_size
        samples_near_cap = MAX_RECORDING_SAMPLES - chunk_size + 1
        pushes = _run_worker_until_cap(self.recorder, samples_near_cap)

        self.assertFalse(self.recorder._is_recording)
        self.assertEqual(pushes, [MAX_RECORDING_SAMPLES],
                         "остановка обязана идти через max-duration, не иначе")
        audio, _duration = self.recorder._pending_result
        # Влезающий хвост последнего чанка сохранён (chunk_size - 1 сэмплов).
        self.assertEqual(audio.size, chunk_size - 1)

    def test_worker_stops_when_cap_exceeded(self):
        """End-to-end: _worker останавливается РОВНО на маленьком cap.

        W1753: крошечный cap (2 с = 32 000 семплов) вместо 4 ч ≈ 880 МБ.
        A02-01: стрим отдаёт настоящий int read_available (guarded read и
        дренаж идут боевым путём), а проверяем сохранённое аудио и причину
        остановки — после cap счётчик обнуляется, и прежнее «<= cap» было
        истинно всегда.
        """
        tiny_cap = 16000 * 2
        recorder = AudioRecorder(sample_rate=16000, max_recording_samples=tiny_cap)
        pushes = _run_worker_until_cap(recorder, 0)

        self.assertFalse(recorder._is_recording)
        self.assertEqual(pushes, [tiny_cap])
        audio, _duration = recorder._pending_result
        self.assertEqual(audio.size, tiny_cap,
                         "в запись обязано попасть ровно cap сэмплов")


class _ReadyStream:
    """Поток с целым read_available: перед чтением готов ровно один чанк."""

    def __init__(self, chunk_size: int) -> None:
        self._chunk = chunk_size
        self._just_read = False
        self.reads = 0

    @property
    def read_available(self) -> int:
        if self._just_read:  # дренаж после read(): накопленного нет
            self._just_read = False
            return 0
        return self._chunk

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def read(self, frames):
        self.reads += 1
        if self.reads > 10_000:  # предохранитель: cap так и не сработал
            raise RuntimeError("cap не сработал")
        self._just_read = True
        return np.zeros((int(frames), 1), dtype=np.float32), False


def _run_worker_until_cap(recorder: AudioRecorder, primed_samples: int) -> list:
    pushes: list = []
    recorder._push_max_duration_error = lambda max_samples: pushes.append(max_samples)
    stream = _ReadyStream(recorder.chunk_size)
    with patch("backend.recorder.sd") as mock_sd:
        mock_sd.InputStream.return_value = stream
        recorder._stop_event.clear()
        with recorder._lock:
            recorder._is_recording = True
            recorder._started_at = 0.0
            recorder._chunks = []
            recorder._chunks_total_samples = primed_samples
        t = threading.Thread(target=recorder._worker, daemon=True)
        t.start()
        t.join(timeout=5.0)
    assert not t.is_alive(), "worker не остановился"
    return pushes


# ---------------------------------------------------------------------------
# Test: snapshot_audio tail-walk avoids full concat
# ---------------------------------------------------------------------------

class TestSnapshotAudioLastNSecondsAvoidFullConcat(unittest.TestCase):

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_snapshot_returns_last_n_seconds(self):
        """snapshot_audio(12) returns last 12s of audio regardless of total buffer size."""
        sample_rate = 16000
        total_sec = 60  # simulate 60-second recording
        window_sec = 12.0
        chunk_sec = 1  # 1-second chunks

        chunks = []
        for i in range(total_sec):
            # Each second has a unique value for identification
            chunks.append(np.full((sample_rate * chunk_sec, 1), float(i), dtype=np.float32))

        _inject_chunks(self.recorder, chunks)
        with self.recorder._lock:
            self.recorder._is_recording = True

        audio, _ = self.recorder.snapshot_audio(max_duration_sec=window_sec)

        expected_samples = int(sample_rate * window_sec)
        self.assertEqual(audio.size, expected_samples)
        # The last window_sec chunks should be from seconds 48–59 (values 48.0–59.0)
        self.assertAlmostEqual(float(audio[0]), 48.0, places=1)
        self.assertAlmostEqual(float(audio[-1]), 59.0, places=1)

    def test_snapshot_audio_last_n_seconds_avoids_full_concat(self):
        """Verify snapshot_audio only concatenates tail chunks, not the whole buffer."""
        sample_rate = 16000
        # Build a large buffer: 300 × 1-second chunks
        n_chunks = 300
        chunks = [np.full((sample_rate, 1), float(i), dtype=np.float32)
                  for i in range(n_chunks)]
        _inject_chunks(self.recorder, chunks)
        with self.recorder._lock:
            self.recorder._is_recording = True

        # Request only last 5 seconds
        window_sec = 5.0
        audio, _ = self.recorder.snapshot_audio(max_duration_sec=window_sec)

        expected_samples = int(sample_rate * window_sec)
        self.assertEqual(audio.size, expected_samples)
        # Last 5 chunks (seconds 295–299) should have values 295.0–299.0
        self.assertAlmostEqual(float(audio[0]), 295.0, places=1)
        self.assertAlmostEqual(float(audio[-1]), 299.0, places=1)

    def test_snapshot_empty_when_no_chunks(self):
        audio, duration = self.recorder.snapshot_audio(max_duration_sec=12.0)
        self.assertEqual(audio.size, 0)
        self.assertEqual(duration, 0.0)

    def test_snapshot_whole_buffer_when_max_duration_zero(self):
        """max_duration_sec=0 should return the entire buffer."""
        chunks = [_make_chunk(3200), _make_chunk(1600)]
        _inject_chunks(self.recorder, chunks)
        with self.recorder._lock:
            self.recorder._is_recording = True
        audio, _ = self.recorder.snapshot_audio(max_duration_sec=0)
        self.assertEqual(audio.size, 3200 + 1600)

    def test_snapshot_shorter_than_window(self):
        """Buffer shorter than window: returns all available samples."""
        chunks = [_make_chunk(800)]  # 0.05 seconds
        _inject_chunks(self.recorder, chunks)
        with self.recorder._lock:
            self.recorder._is_recording = True
        audio, _ = self.recorder.snapshot_audio(max_duration_sec=12.0)
        self.assertEqual(audio.size, 800)

    def test_snapshot_exact_window_size(self):
        """Buffer exactly matches requested window."""
        sample_rate = 16000
        window_sec = 12.0
        samples = int(sample_rate * window_sec)
        chunks = [_make_chunk(samples)]
        _inject_chunks(self.recorder, chunks)
        with self.recorder._lock:
            self.recorder._is_recording = True
        audio, _ = self.recorder.snapshot_audio(max_duration_sec=window_sec)
        self.assertEqual(audio.size, samples)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
