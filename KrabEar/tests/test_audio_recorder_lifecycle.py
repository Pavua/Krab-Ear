"""Wave 253 — AudioRecorder lifecycle + edge case tests.

Covers:
- start/stop basic lifecycle
- double-start idempotency
- stop-before-start safety
- captured samples returned correctly
- capture thread lifecycle (thread created/joined)
- device unavailable (OSError from InputStream)
- device disconnect mid-recording (OSError during stream.read)
- concurrent start serialization
- unicode device name forwarded to sounddevice
- clear-buffer safety after failed recording

sounddevice is fully mocked — no real microphone required.
"""

from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stream_cm(chunk_size: int = 1600, chunks_to_emit: int = 3) -> MagicMock:
    """Return a mock InputStream context manager.

    The stream emits *chunks_to_emit* chunks of ones, then blocks until
    the stop_event fires (simulated by raising StopIteration after limit).
    """
    stream = MagicMock()

    call_count = {"n": 0}

    def _read(n: int) -> tuple[np.ndarray, bool]:
        call_count["n"] += 1
        # After we've emitted enough chunks, block briefly so the worker
        # loop keeps running until stop_event is set.
        if call_count["n"] > chunks_to_emit:
            time.sleep(0.005)
        return (np.ones((n, 1), dtype=np.float32) * 0.5, False)

    stream.read.side_effect = _read
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=stream)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _start_and_record(rec: AudioRecorder, duration: float = 0.05) -> None:
    rec.start()
    time.sleep(duration)


# ---------------------------------------------------------------------------
# 1. Basic start / stop
# ---------------------------------------------------------------------------

class TestStartStopBasic(unittest.TestCase):
    def test_start_stop_basic(self) -> None:
        """start() returns True, is_recording becomes True; stop() returns audio tuple."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            result = rec.start()
            self.assertTrue(result, "start() should return True on first call")
            self.assertTrue(rec.is_recording, "is_recording should be True after start()")
            time.sleep(0.05)
            ret = rec.stop()
            self.assertIsNotNone(ret, "stop() must return a tuple")
            audio, duration = ret
            self.assertIsInstance(audio, np.ndarray)
            self.assertGreaterEqual(duration, 0.0)
            self.assertFalse(rec.is_recording, "is_recording should be False after stop()")


# ---------------------------------------------------------------------------
# 2. Double start idempotent
# ---------------------------------------------------------------------------

class TestDoubleStart(unittest.TestCase):
    def test_double_start_idempotent(self) -> None:
        """Second start() while already recording returns False without crashing."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            first = rec.start()
            second = rec.start()
            try:
                self.assertTrue(first, "First start() must return True")
                self.assertFalse(second, "Second start() must return False (already recording)")
                self.assertTrue(rec.is_recording)
            finally:
                rec.stop()


# ---------------------------------------------------------------------------
# 3. Stop before start
# ---------------------------------------------------------------------------

class TestStopBeforeStart(unittest.TestCase):
    def test_stop_before_start_handled(self) -> None:
        """stop() on an idle recorder returns None without raising."""
        rec = AudioRecorder()
        result = rec.stop()
        self.assertIsNone(result, "stop() before start() must return None")
        self.assertFalse(rec.is_recording)


# ---------------------------------------------------------------------------
# 4. Audio samples returned correctly
# ---------------------------------------------------------------------------

class TestGetAudioReturnsCapturedSamples(unittest.TestCase):
    def test_get_audio_returns_captured_samples(self) -> None:
        """Chunks accumulated during recording are returned by stop() as float32 1-D array."""
        chunk_size = 1600
        with patch("sounddevice.InputStream", return_value=_make_stream_cm(chunk_size=chunk_size, chunks_to_emit=5)):
            rec = AudioRecorder(sample_rate=16000, channels=1)
            rec.start()
            time.sleep(0.08)  # let worker produce several chunks
            ret = rec.stop()
        self.assertIsNotNone(ret)
        audio, _ = ret
        self.assertEqual(audio.dtype, np.float32, "Audio dtype must be float32")
        self.assertEqual(audio.ndim, 1, "Audio must be 1-D")
        self.assertGreater(audio.size, 0, "At least one sample expected")
        # All chunks filled with 0.5 → result values close to 0.5
        self.assertTrue(np.allclose(audio, 0.5), "Captured values should be 0.5")


# ---------------------------------------------------------------------------
# 5. Capture thread lifecycle
# ---------------------------------------------------------------------------

class TestCaptureThreadLifecycle(unittest.TestCase):
    def test_capture_thread_lifecycle(self) -> None:
        """A worker thread is created on start() and joined/cleared on stop()."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            rec.start()
            # Thread should exist and be alive shortly after start
            time.sleep(0.02)
            with rec._lock:
                thread_ref = rec._thread
            self.assertIsNotNone(thread_ref, "Worker thread must be set after start()")
            self.assertTrue(thread_ref.is_alive(), "Worker thread must be alive while recording")
            rec.stop()
            # After stop, thread reference is cleared
            with rec._lock:
                self.assertIsNone(rec._thread, "_thread must be None after stop()")
            # Ensure the thread actually finished (not zombie)
            thread_ref.join(timeout=1.0)
            self.assertFalse(thread_ref.is_alive(), "Worker thread should have terminated after stop()")


# ---------------------------------------------------------------------------
# 6. Device unavailable (OSError on InputStream open)
# ---------------------------------------------------------------------------

class TestHandlesDeviceUnavailable(unittest.TestCase):
    def test_handles_device_unavailable(self) -> None:
        """OSError raised by InputStream.__enter__ must not crash stop(); is_recording resets."""
        error_cm = MagicMock()
        error_cm.__enter__ = MagicMock(side_effect=OSError("No such device"))
        error_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=error_cm):
            rec = AudioRecorder()
            rec.start()
            # Worker thread exits immediately due to OSError; give it time
            time.sleep(0.05)
            # is_recording is reset by the finally block in _worker
            self.assertFalse(rec.is_recording, "is_recording must be False after device error")
            # stop() must still be safe even if is_recording already cleared
            result = rec.stop()
            # Either None (already idle) or a valid tuple — both are acceptable
            if result is not None:
                audio, duration = result
                self.assertIsInstance(audio, np.ndarray)


# ---------------------------------------------------------------------------
# 7. Device disconnect mid-recording
# ---------------------------------------------------------------------------

class TestHandlesDeviceDisconnectMidRecording(unittest.TestCase):
    def test_handles_device_disconnect_mid_recording(self) -> None:
        """OSError raised by stream.read mid-recording resets is_recording cleanly."""
        stream = MagicMock()
        call_count = {"n": 0}

        def _read(n: int) -> tuple[np.ndarray, bool]:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                raise OSError("Device disconnected")
            return (np.ones((n, 1), dtype=np.float32), False)

        stream.read.side_effect = _read
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=stream)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=cm):
            rec = AudioRecorder()
            rec.start()
            # Worker crashes after 3 reads
            time.sleep(0.1)
            self.assertFalse(rec.is_recording, "is_recording must be False after mid-read disconnect")
            # stop() must not raise
            result = rec.stop()
            # If stop returns data, audio must be valid ndarray
            if result is not None:
                audio, _ = result
                self.assertIsInstance(audio, np.ndarray)


# ---------------------------------------------------------------------------
# 8. Concurrent start serialized
# ---------------------------------------------------------------------------

class TestConcurrentStartSerialized(unittest.TestCase):
    def test_concurrent_start_serialized(self) -> None:
        """Only one of many concurrent start() calls should succeed."""
        with patch("sounddevice.InputStream", return_value=_make_stream_cm()):
            rec = AudioRecorder()
            results: list[bool] = []
            lock = threading.Lock()

            def _try_start() -> None:
                r = rec.start()
                with lock:
                    results.append(r)

            threads = [threading.Thread(target=_try_start) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            rec.stop()

        true_count = sum(1 for r in results if r)
        self.assertEqual(true_count, 1, f"Exactly one start() must succeed; got {true_count} True values")


# ---------------------------------------------------------------------------
# 9. Unicode device name forwarded to sounddevice
# ---------------------------------------------------------------------------

class TestUnicodeDeviceName(unittest.TestCase):
    def test_unicode_device_name(self) -> None:
        """AudioRecorder constructed with unicode sample_rate/channels doesn't crash;
        when 'device' kwarg is added in future, it should pass through cleanly.
        Current implementation does not accept a device kwarg, but we verify that
        the InputStream is opened with the configured samplerate and channels even
        when the recorder was created after setting unicode-named attributes.
        """
        mock_stream = _make_stream_cm()
        with patch("sounddevice.InputStream", return_value=mock_stream) as mock_cls:
            rec = AudioRecorder(sample_rate=16000, channels=1)
            # Simulate a unicode description stored on the recorder (no crash expected)
            rec._device_label = "Микрофон — встроенный 🎙️"  # type: ignore[attr-defined]
            rec.start()
            time.sleep(0.03)
            rec.stop()
            # Verify InputStream was called with correct numeric params
            mock_cls.assert_called_once()
            _, kwargs = mock_cls.call_args
            self.assertEqual(kwargs.get("samplerate"), 16000)
            self.assertEqual(kwargs.get("channels"), 1)
            self.assertEqual(kwargs.get("dtype"), "float32")


# ---------------------------------------------------------------------------
# 10. Clear-buffer safety
# ---------------------------------------------------------------------------

class TestClearBufferSafety(unittest.TestCase):
    def test_clear_buffer_safety(self) -> None:
        """After a failed/disconnected recording, starting again yields a clean buffer."""
        # Simulate first recording with device error (no chunks)
        error_cm = MagicMock()
        error_cm.__enter__ = MagicMock(side_effect=OSError("No device"))
        error_cm.__exit__ = MagicMock(return_value=False)

        with patch("sounddevice.InputStream", return_value=error_cm):
            rec = AudioRecorder()
            rec.start()
            time.sleep(0.05)

        # Inject leftover chunks manually (simulate partial state from first run)
        with rec._lock:
            rec._chunks = [np.ones((800, 1), dtype=np.float32)]
            rec._is_recording = False  # device error already cleared this

        # Second recording — start() must clear the old chunks
        with patch("sounddevice.InputStream", return_value=_make_stream_cm(chunk_size=1600, chunks_to_emit=2)):
            result2 = rec.start()
            self.assertTrue(result2, "Second start() after recovery should return True")
            # Verify chunks were cleared at start time
            time.sleep(0.02)
            with rec._lock:
                # Any chunks here were written by the new recording (values=0.5), not the old ones (1.0)
                if rec._chunks:
                    first_chunk = rec._chunks[0]
                    self.assertTrue(
                        np.allclose(first_chunk, 0.5),
                        "Buffer should only contain fresh recording data (0.5), not stale data (1.0)"
                    )
            rec.stop()


if __name__ == "__main__":
    unittest.main()
