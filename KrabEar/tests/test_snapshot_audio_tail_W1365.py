"""Tests for W1359 F2 MED fix: snapshot_audio tail-only concatenation.

Verifies that snapshot_audio:
  1. Returns only the tail (max_duration_sec) of the recording.
  2. Does NOT allocate the full concatenation on long recordings.
  3. Returns the correct audio samples from the tail even when a chunk is partially included.
"""

from __future__ import annotations

import sys
import os
import time
import unittest
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.recorder import AudioRecorder  # noqa: E402


def _make_recorder_with_chunks(
    chunks: list[np.ndarray],
    sample_rate: int = 16000,
) -> AudioRecorder:
    """Return a recorder pre-loaded with the given chunk list (not actually recording)."""
    rec = AudioRecorder(sample_rate=sample_rate, channels=1)
    rec._is_recording = True  # noqa: SLF001
    rec._started_at = time.monotonic() - 5.0  # noqa: SLF001
    rec._chunks = list(chunks)  # noqa: SLF001
    rec._thread = None  # noqa: SLF001
    return rec


class TestSnapshotReturnsTailOnly(unittest.TestCase):
    """snapshot_audio must return only the last max_duration_sec of audio."""

    def test_snapshot_returns_only_tail_seconds(self) -> None:
        """With 4 s of audio and max_duration_sec=2, must return exactly 2 s."""
        sample_rate = 16000
        # 4 chunks × 1 s each = 4 s total
        chunks = [
            np.full((sample_rate, 1), float(i), dtype=np.float32)
            for i in range(4)  # values 0.0, 1.0, 2.0, 3.0
        ]
        rec = _make_recorder_with_chunks(chunks, sample_rate=sample_rate)

        audio, _ = rec.snapshot_audio(max_duration_sec=2.0)

        # Should have exactly 2 × 16000 = 32000 samples
        self.assertEqual(audio.size, 2 * sample_rate)
        # The tail contains chunks with values 2.0 and 3.0
        self.assertTrue(np.all(audio[:sample_rate] == 2.0), "First half must be chunk 2 (value=2.0)")
        self.assertTrue(np.all(audio[sample_rate:] == 3.0), "Second half must be chunk 3 (value=3.0)")

    def test_snapshot_returns_full_when_shorter_than_max(self) -> None:
        """If recording is shorter than max_duration_sec, return everything."""
        sample_rate = 16000
        chunks = [np.ones((sample_rate, 1), dtype=np.float32)]  # 1 s
        rec = _make_recorder_with_chunks(chunks, sample_rate=sample_rate)

        audio, _ = rec.snapshot_audio(max_duration_sec=12.0)

        self.assertEqual(audio.size, sample_rate)

    def test_snapshot_empty_chunks_returns_empty(self) -> None:
        rec = _make_recorder_with_chunks([], sample_rate=16000)
        audio, _ = rec.snapshot_audio(max_duration_sec=3.0)
        self.assertEqual(audio.size, 0)

    def test_snapshot_zero_max_duration_returns_all(self) -> None:
        """max_duration_sec=0 means no cap — return all audio (legacy behaviour)."""
        sample_rate = 16000
        chunks = [np.ones((sample_rate * 2, 1), dtype=np.float32)]
        rec = _make_recorder_with_chunks(chunks, sample_rate=sample_rate)

        audio, _ = rec.snapshot_audio(max_duration_sec=0)

        self.assertEqual(audio.size, sample_rate * 2)


class TestSnapshotAvoidsFullConcatenateOnLongRecording(unittest.TestCase):
    """Verify that snapshot_audio does not concatenate the entire chunk list.

    Strategy: monkeypatch np.concatenate to count how many total samples are
    passed in. If the fix is correct, the concatenation input must be bounded
    by max_duration_sec, not the full recording length.
    """

    def test_snapshot_avoids_full_concatenate_on_long_recording(self) -> None:
        sample_rate = 16000
        total_seconds = 30          # simulated 30-minute-ish recording
        max_tail_sec = 3.0          # snapshot should only need 3 s

        chunks = [
            np.ones((sample_rate, 1), dtype=np.float32) * float(i)
            for i in range(total_seconds)
        ]
        rec = _make_recorder_with_chunks(chunks, sample_rate=sample_rate)

        concat_input_sizes: list[int] = []
        real_concatenate = np.concatenate

        def counting_concatenate(arrays, *args, **kwargs):
            total = sum(a.size for a in arrays)
            concat_input_sizes.append(total)
            return real_concatenate(arrays, *args, **kwargs)

        with patch("numpy.concatenate", side_effect=counting_concatenate):
            audio, _ = rec.snapshot_audio(max_duration_sec=max_tail_sec)

        # The fix should result in at most one concatenate call
        # and that call should cover at most max_tail_sec * sample_rate samples
        # (plus at most one extra chunk boundary).
        max_allowed_samples = int(max_tail_sec * sample_rate) + sample_rate  # +1 chunk tolerance
        for call_size in concat_input_sizes:
            self.assertLessEqual(
                call_size,
                max_allowed_samples,
                f"np.concatenate received {call_size} samples — expected at most "
                f"{max_allowed_samples} (tail + 1 chunk boundary). "
                "Full buffer concatenation was NOT avoided.",
            )

        # Correctness: result must still be trimmed to max_tail_sec
        self.assertLessEqual(audio.size, int(max_tail_sec * sample_rate))


class TestSnapshotTailCorrectnessWithPartialLastChunk(unittest.TestCase):
    """Partial last-chunk scenario: the tail cut falls in the middle of a chunk."""

    def test_snapshot_tail_correctness_with_partial_last_chunk(self) -> None:
        """max_duration_sec cut that lands inside the final tail chunk.

        Setup: 3 chunks each 1 s long; max_duration_sec=1.5 s.
        Expected result: last 1.5 s = 24000 samples, values from chunk 2 (second half)
        and all of chunk 3 (third chunk).
        """
        sample_rate = 16000
        chunk_a = np.full((sample_rate, 1), 1.0, dtype=np.float32)  # 1 s, value=1
        chunk_b = np.full((sample_rate, 1), 2.0, dtype=np.float32)  # 1 s, value=2
        chunk_c = np.full((sample_rate, 1), 3.0, dtype=np.float32)  # 1 s, value=3
        rec = _make_recorder_with_chunks([chunk_a, chunk_b, chunk_c], sample_rate=sample_rate)

        tail_sec = 1.5
        audio, _ = rec.snapshot_audio(max_duration_sec=tail_sec)

        expected_samples = int(sample_rate * tail_sec)  # 24000
        self.assertEqual(audio.size, expected_samples)

        # Last 16000 samples = chunk_c (value=3)
        self.assertTrue(
            np.all(audio[-sample_rate:] == 3.0),
            "Last chunk must be all 3.0",
        )
        # First 8000 samples = second half of chunk_b (value=2)
        self.assertTrue(
            np.all(audio[:sample_rate // 2] == 2.0),
            "Earlier part must be 2.0 (tail of chunk_b)",
        )

    def test_snapshot_single_chunk_larger_than_max(self) -> None:
        """Single chunk bigger than max_duration_sec — must truncate correctly."""
        sample_rate = 16000
        # 2 s chunk, all value=7.0; request only 1 s
        chunk = np.full((sample_rate * 2, 1), 7.0, dtype=np.float32)
        rec = _make_recorder_with_chunks([chunk], sample_rate=sample_rate)

        audio, _ = rec.snapshot_audio(max_duration_sec=1.0)

        self.assertEqual(audio.size, sample_rate)
        self.assertTrue(np.all(audio == 7.0))

    def test_snapshot_audio_is_1d_float32(self) -> None:
        """Output must always be 1-D float32 regardless of chunk shape."""
        sample_rate = 16000
        chunks = [
            np.ones((sample_rate, 1), dtype=np.float32),
            np.ones((sample_rate, 1), dtype=np.float32),
        ]
        rec = _make_recorder_with_chunks(chunks, sample_rate=sample_rate)
        audio, _ = rec.snapshot_audio(max_duration_sec=1.0)
        self.assertEqual(audio.ndim, 1)
        self.assertEqual(audio.dtype, np.float32)


if __name__ == "__main__":
    unittest.main()
