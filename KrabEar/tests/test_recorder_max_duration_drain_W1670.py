"""Tests for W1670 fix: preserve + return buffered audio on max-duration auto-stop.

W1649 F2 MED: when MAX_RECORDING_SAMPLES is hit, accumulated audio was silently
discarded because _is_recording=False caused stop() to return None.

Fix: worker stores (audio, duration) in _pending_result when auto-stopping;
stop() returns _pending_result instead of None; _chunks cleared immediately to
free ~880 MB; start() resets _pending_result.
"""
from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

# Path setup
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRAB_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_ROOT not in sys.path:
    sys.path.insert(0, KRAB_ROOT)

from backend.recorder import AudioRecorder, MAX_RECORDING_SAMPLES


# ---------------------------------------------------------------------------
# Helper: inject pre-built chunks into recorder state, simulating post-worker state
# ---------------------------------------------------------------------------

def _inject_auto_stopped(recorder: AudioRecorder, n_chunks: int = 3,
                         chunk_samples: int = 1600) -> None:
    """Simulate the worker having hit MAX_RECORDING_SAMPLES.

    Sets _is_recording=False, _pending_result=(audio, duration), _chunks=[].
    This mirrors exactly what _worker does after the W1670 fix.
    """
    chunks = [np.ones((chunk_samples, 1), dtype=np.float32) * 0.5
              for _ in range(n_chunks)]
    audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
    duration = float(chunk_samples * n_chunks) / recorder.sample_rate
    with recorder._lock:
        recorder._is_recording = False
        recorder._pending_result = (audio, duration)
        recorder._chunks = []
        recorder._chunks_total_samples = 0
        recorder._started_at = time.monotonic() - duration


class TestMaxDurationAutostopPreservesAudio(unittest.TestCase):
    """stop() after auto-stop must return (audio, duration), not None."""

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_stop_returns_tuple_not_none(self):
        """stop() must return (ndarray, float) when _pending_result is set."""
        _inject_auto_stopped(self.recorder, n_chunks=3, chunk_samples=1600)
        result = self.recorder.stop()
        self.assertIsNotNone(result, "stop() returned None — audio was silently discarded (W1649 F2)")
        audio, duration = result
        self.assertIsInstance(audio, np.ndarray)
        self.assertIsInstance(duration, float)

    def test_stop_audio_is_1d_float32(self):
        """stop() must return a 1-D float32 array."""
        _inject_auto_stopped(self.recorder, n_chunks=2, chunk_samples=800)
        audio, _ = self.recorder.stop()
        self.assertEqual(audio.ndim, 1, "audio array must be 1-D")
        self.assertEqual(audio.dtype, np.float32, "audio dtype must be float32")

    def test_stop_audio_has_correct_sample_count(self):
        """stop() audio must contain exactly n_chunks * chunk_samples samples."""
        n_chunks, chunk_samples = 4, 1600
        _inject_auto_stopped(self.recorder, n_chunks=n_chunks, chunk_samples=chunk_samples)
        audio, _ = self.recorder.stop()
        expected = n_chunks * chunk_samples
        self.assertEqual(audio.size, expected,
                         f"Expected {expected} samples, got {audio.size}")

    def test_stop_audio_values_correct(self):
        """stop() audio values must match original chunk values (0.5)."""
        _inject_auto_stopped(self.recorder, n_chunks=2, chunk_samples=400)
        audio, _ = self.recorder.stop()
        self.assertTrue(np.allclose(audio, 0.5),
                        "Audio values corrupted during pending_result assembly")

    def test_stop_duration_is_positive(self):
        """stop() duration must be > 0 after auto-stop."""
        _inject_auto_stopped(self.recorder, n_chunks=3, chunk_samples=1600)
        _, duration = self.recorder.stop()
        self.assertGreater(duration, 0.0)

    def test_stop_pending_cleared_after_return(self):
        """After stop() returns _pending_result, it must be cleared (one-shot)."""
        _inject_auto_stopped(self.recorder, n_chunks=2, chunk_samples=800)
        first = self.recorder.stop()
        self.assertIsNotNone(first)
        # Second call must return None (no recording active, no pending)
        second = self.recorder.stop()
        self.assertIsNone(second, "_pending_result must be cleared after first stop()")


class TestMaxDurationAutostopClearsChunks(unittest.TestCase):
    """_chunks must be freed immediately when max-duration auto-stop fires."""

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def test_chunks_empty_in_pending_state(self):
        """After W1670 auto-stop simulation, _chunks must be empty."""
        _inject_auto_stopped(self.recorder, n_chunks=5, chunk_samples=1600)
        with self.recorder._lock:
            chunk_count = len(self.recorder._chunks)
        self.assertEqual(chunk_count, 0, "_chunks must be freed after auto-stop to release ~880 MB")

    def test_chunks_total_samples_zero_in_pending_state(self):
        """_chunks_total_samples must be 0 after auto-stop finalize."""
        _inject_auto_stopped(self.recorder, n_chunks=3, chunk_samples=1600)
        with self.recorder._lock:
            total = self.recorder._chunks_total_samples
        self.assertEqual(total, 0)

    def test_worker_clears_chunks_on_max_duration(self):
        """Integration: real _worker path clears _chunks when MAX_RECORDING_SAMPLES hit."""
        chunk_size = self.recorder.chunk_size
        # Prime counter so next read will trigger the cap
        samples_near_cap = MAX_RECORDING_SAMPLES - chunk_size + 1

        def fake_read(_n):
            data = np.zeros((chunk_size, 1), dtype=np.float32)
            return data, False

        mock_stream = MagicMock()
        mock_stream.__enter__ = lambda s: s
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.read = fake_read

        with patch("backend.recorder.sd") as mock_sd:
            mock_sd.InputStream.return_value = mock_stream
            self.recorder._stop_event.clear()
            with self.recorder._lock:
                self.recorder._is_recording = True
                self.recorder._started_at = time.monotonic()
                self.recorder._chunks = []
                self.recorder._chunks_total_samples = samples_near_cap

            t = threading.Thread(target=self.recorder._worker, daemon=True)
            t.start()
            t.join(timeout=5.0)

        self.assertFalse(t.is_alive(), "Worker must have exited")
        with self.recorder._lock:
            self.assertEqual(len(self.recorder._chunks), 0,
                             "_chunks must be freed after max-duration auto-stop")


class TestNormalStopUnchanged(unittest.TestCase):
    """Regular start/stop flow must still work correctly after W1670 changes."""

    def setUp(self):
        self.recorder = AudioRecorder(sample_rate=16000)

    def _make_recorder_recording(self, n_samples: int = 1600) -> None:
        """Inject recording state directly (no actual audio device needed)."""
        self.recorder._is_recording = True
        self.recorder._started_at = time.monotonic() - 0.5
        self.recorder._chunks = [np.ones((n_samples, 1), dtype=np.float32) * 0.3]
        self.recorder._chunks_total_samples = n_samples
        self.recorder._thread = None
        self.recorder._pending_result = None

    def test_normal_stop_returns_tuple(self):
        """Normal stop() path still returns (ndarray, float)."""
        self._make_recorder_recording()
        result = self.recorder.stop()
        self.assertIsNotNone(result)
        audio, duration = result
        self.assertIsInstance(audio, np.ndarray)
        self.assertIsInstance(duration, float)

    def test_normal_stop_is_recording_false_after(self):
        """After normal stop(), is_recording must be False."""
        self._make_recorder_recording()
        self.recorder.stop()
        self.assertFalse(self.recorder.is_recording)

    def test_double_stop_second_returns_none_no_pending(self):
        """Double stop() without pending result returns None on second call."""
        self._make_recorder_recording()
        first = self.recorder.stop()
        self.assertIsNotNone(first)
        second = self.recorder.stop()
        self.assertIsNone(second, "Second stop() with no pending must return None")

    def test_no_pending_result_on_fresh_recorder(self):
        """Fresh AudioRecorder must have _pending_result=None."""
        rec = AudioRecorder()
        self.assertIsNone(rec._pending_result)

    def test_start_clears_pending_result(self):
        """start() must clear any leftover _pending_result from a previous auto-stop."""
        # Simulate leftover pending from previous session
        dummy_audio = np.zeros(100, dtype=np.float32)
        self.recorder._pending_result = (dummy_audio, 1.0)
        self.recorder._is_recording = False

        mock_stream = MagicMock()
        mock_stream.__enter__ = lambda s: s
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.read.return_value = (np.zeros((self.recorder.chunk_size, 1), dtype=np.float32), False)

        with patch("backend.recorder.sd") as mock_sd:
            mock_sd.InputStream.return_value = mock_stream
            self.recorder.start()
            # _pending_result must be cleared immediately on start()
            with self.recorder._lock:
                pending = self.recorder._pending_result
            self.assertIsNone(pending, "start() must clear _pending_result (W1670)")
            self.recorder.stop()

    def test_stop_returns_none_idle_no_pending(self):
        """stop() on idle recorder with no pending result returns None."""
        rec = AudioRecorder()
        self.assertFalse(rec.is_recording)
        self.assertIsNone(rec._pending_result)
        result = rec.stop()
        self.assertIsNone(result)

    def test_pending_result_integration_with_worker(self):
        """Integration: after worker auto-stops, stop() returns the buffered audio.

        Pre-fills _chunks with two real chunks so there is audio to preserve, then
        primes _chunks_total_samples so the very next read triggers the cap.
        """
        chunk_size = self.recorder.chunk_size
        audio_value = 0.42
        # Two real chunks already in buffer before cap is hit
        pre_chunks = [np.full((chunk_size, 1), audio_value, dtype=np.float32) for _ in range(2)]
        pre_samples = chunk_size * 2
        # Set counter so next chunk pushes us over the limit
        samples_near_cap = MAX_RECORDING_SAMPLES - chunk_size + 1

        def fake_read(_n):
            data = np.full((chunk_size, 1), audio_value, dtype=np.float32)
            return data, False

        mock_stream = MagicMock()
        mock_stream.__enter__ = lambda s: s
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.read = fake_read

        with patch("backend.recorder.sd") as mock_sd:
            mock_sd.InputStream.return_value = mock_stream
            self.recorder._stop_event.clear()
            with self.recorder._lock:
                self.recorder._is_recording = True
                self.recorder._started_at = time.monotonic()
                # Pre-fill chunks so there is audio to preserve
                self.recorder._chunks = pre_chunks[:]
                self.recorder._chunks_total_samples = samples_near_cap

            t = threading.Thread(target=self.recorder._worker, daemon=True)
            t.start()
            t.join(timeout=5.0)

        self.assertFalse(t.is_alive(), "Worker must have exited")
        # At this point _is_recording=False, _pending_result should be set
        result = self.recorder.stop()
        self.assertIsNotNone(
            result,
            "stop() must return (audio, duration) after max-duration auto-stop, not None (W1649 F2)",
        )
        audio, duration = result
        # The pre-filled chunks (2 × chunk_size) must be in the result
        self.assertGreaterEqual(audio.size, pre_samples,
                                "Audio must contain at least the pre-filled chunks")
        self.assertGreater(duration, 0.0, "Duration must be positive")


if __name__ == "__main__":
    unittest.main()
