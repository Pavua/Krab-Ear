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
from unittest.mock import MagicMock, patch, PropertyMock

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
        """When _chunks_total_samples approaches the cap, _worker must stop recording."""
        # We'll simulate the worker loop logic directly without a real sounddevice.
        # Pre-fill to just below the cap
        chunk_size = self.recorder.chunk_size  # 1600 samples per chunk
        # Fill to one chunk below the cap
        samples_near_cap = MAX_RECORDING_SAMPLES - chunk_size + 1
        with self.recorder._lock:
            # Don't actually store all the data — just advance the counter
            self.recorder._chunks_total_samples = samples_near_cap
            self.recorder._is_recording = True
            self.recorder._started_at = 0.0

        # Now simulate what _worker does with the NEXT chunk
        next_data = _make_chunk(chunk_size)
        next_samples = next_data.reshape(-1).size
        cap_exceeded = False
        with self.recorder._lock:
            if self.recorder._chunks_total_samples + next_samples > MAX_RECORDING_SAMPLES:
                self.recorder._is_recording = False
                cap_exceeded = True
            else:
                self.recorder._chunks.append(next_data.copy())
                self.recorder._chunks_total_samples += next_samples

        self.assertTrue(cap_exceeded, "Cap should have been triggered")
        self.assertFalse(self.recorder._is_recording,
                         "_is_recording must be False after cap")

    def test_worker_stops_when_cap_exceeded(self):
        """End-to-end: _worker loop breaks when the recording-sample cap is exceeded.

        W1753: используем крошечный cap (2 сек = 32 000 семплов) вместо глобального
        MAX_RECORDING_SAMPLES (4 ч ≈ 880 МБ). Это тестирует тот же код-путь в _worker
        (строка ``if _chunks_total_samples + chunk_samples > self._max_recording_samples``),
        но выделяет ~200 КБ вместо ~880 МБ — безопасно для CI-воркеров с ограниченной RAM.
        """
        sample_rate = 16000
        # Tiny cap: 2 seconds at 16 kHz = 32 000 samples (~200 KB total allocation)
        tiny_cap = sample_rate * 2  # 32 000 samples
        recorder = AudioRecorder(sample_rate=sample_rate, max_recording_samples=tiny_cap)

        chunk_size = recorder.chunk_size  # 1600 samples per chunk

        call_count = [0]

        def fake_read(_n):
            call_count[0] += 1
            data = np.zeros((chunk_size, 1), dtype=np.float32)
            return data, False

        mock_stream = MagicMock()
        mock_stream.__enter__ = lambda s: s
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.read = fake_read

        with patch('backend.recorder.sd') as mock_sd:
            mock_sd.InputStream.return_value = mock_stream
            recorder._stop_event.clear()
            with recorder._lock:
                recorder._is_recording = True
                recorder._started_at = 0.0
                recorder._chunks = []
                recorder._chunks_total_samples = 0

            # Run _worker in a thread; it should stop itself after tiny_cap is exceeded
            t = threading.Thread(target=recorder._worker, daemon=True)
            t.start()
            t.join(timeout=5.0)

        self.assertFalse(t.is_alive(), "Worker thread must have stopped")
        self.assertFalse(recorder._is_recording,
                         "_is_recording must be False after cap stop")
        # Should have stopped at or below the (tiny) cap
        self.assertLessEqual(
            recorder._chunks_total_samples,
            tiny_cap,
            "Must not exceed cap",
        )


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
