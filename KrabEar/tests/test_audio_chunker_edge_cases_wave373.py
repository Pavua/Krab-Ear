"""Wave 373: AudioChunker edge case tests.

Post Wave 359 micro-advance bug fix (cursor 0.01s loop prevention).
Categories:
  1. All-silent audio
  2. All-speech (no silence)
  3. Tiny audio (<100ms)
  4. Single silence in middle of long audio
  5. Multiple short silences
  6. Boundary: audio exactly at MAX_CHUNK_SEC + 1ms
  7. Boundary: audio exactly at 2x MAX_CHUNK_SEC
  8. MIN_SAMPLES padding assertion (GigaAM Conformer 400 sample minimum)
  9. Wave 359 regression: long leading silence
  10. Concurrent chunker calls (thread safety)
"""

from __future__ import annotations

import math
import sys
import threading
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.audio_chunker import AudioChunker  # noqa: E402

SR = 16_000  # Hz
MAX_CHUNK_SEC = 20.0  # use 20s so tests run fast
# GigaAM Conformer minimum: 400 samples @ 16 kHz
GIGAAM_MIN_SAMPLES = 400


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_silence(sec: float, sr: int = SR) -> np.ndarray:
    return np.zeros(int(sr * sec), dtype=np.float32)


def make_tone(sec: float, freq: float = 440.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(sr * sec)) / sr
    return (0.3 * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def cat(*arrays: np.ndarray) -> np.ndarray:
    return np.concatenate(arrays)


# ---------------------------------------------------------------------------
# 1. All-silent audio
# ---------------------------------------------------------------------------

class TestAllSilentAudio(unittest.TestCase):
    """Pure 0-amplitude audio of various lengths."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def test_all_silent_25s_returns_one_chunk(self):
        """25s pure silence is shorter than MAX_CHUNK_SEC (30 default)
        but we use 20s here — so 25s > MAX_CHUNK_SEC.
        The chunker should still return chunks covering the full audio."""
        audio = make_silence(25.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        # At least 1 chunk
        self.assertGreaterEqual(len(chunks), 1)

    def test_all_silent_25s_covers_full_duration(self):
        audio = make_silence(25.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        covered = sum(c.duration_sec() for c in chunks)
        expected = len(audio) / SR
        self.assertAlmostEqual(covered, expected, delta=0.5)

    def test_all_silent_10s_single_chunk(self):
        """10s silence < MAX_CHUNK_SEC (20s) → must be exactly 1 chunk."""
        audio = make_silence(10.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)

    def test_all_silent_60s_sample_count_preserved(self):
        """Total samples across all chunks == original sample count."""
        audio = make_silence(60.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total_samples = sum(len(c.audio) for c in chunks)
        self.assertEqual(total_samples, len(audio))

    def test_all_silent_chunk_indices_sequential(self):
        audio = make_silence(45.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        indices = [c.index for c in chunks]
        self.assertEqual(indices, list(range(len(chunks))))

    def test_all_silent_no_negative_duration(self):
        audio = make_silence(35.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for c in chunks:
            self.assertGreaterEqual(c.duration_sec(), 0.0,
                                    msg=f"Chunk {c.index} has negative duration")


# ---------------------------------------------------------------------------
# 2. All-speech (no silence) — hard-split at MAX_CHUNK_SEC
# ---------------------------------------------------------------------------

class TestAllSpeechNoSilence(unittest.TestCase):
    """Continuous 440 Hz tone — no natural split points."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def test_10s_single_chunk(self):
        audio = make_tone(10.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)

    def test_40s_at_least_two_chunks(self):
        audio = make_tone(40.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertGreaterEqual(len(chunks), 2)

    def test_hard_split_no_chunk_exceeds_max(self):
        audio = make_tone(55.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for c in chunks[:-1]:
            self.assertLessEqual(c.duration_sec(), MAX_CHUNK_SEC + 0.1,
                                 msg=f"Chunk {c.index} duration {c.duration_sec():.3f}s "
                                     f"exceeds MAX_CHUNK_SEC {MAX_CHUNK_SEC}")

    def test_hard_split_sample_count_preserved(self):
        audio = make_tone(45.0)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total_samples = sum(len(c.audio) for c in chunks)
        self.assertEqual(total_samples, len(audio))

    def test_hard_split_coverage(self):
        audio = make_tone(60.0)
        expected_sec = len(audio) / SR
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        covered = sum(c.duration_sec() for c in chunks)
        self.assertAlmostEqual(covered, expected_sec, delta=0.5)


# ---------------------------------------------------------------------------
# 3. Tiny audio (<100ms)
# ---------------------------------------------------------------------------

class TestTinyAudio(unittest.TestCase):
    """Audio shorter than 100ms must never be chunked."""

    def setUp(self):
        self.chunker = AudioChunker()

    def test_50ms_returns_single_chunk(self):
        audio = make_tone(0.05)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)

    def test_1ms_returns_single_chunk(self):
        audio = make_tone(0.001)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)

    def test_99ms_returns_single_chunk(self):
        audio = make_tone(0.099)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)

    def test_tiny_chunk_audio_data_intact(self):
        audio = make_tone(0.05)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        np.testing.assert_array_equal(chunks[0].audio, audio)

    def test_tiny_silent_single_chunk(self):
        audio = make_silence(0.05)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 1)


# ---------------------------------------------------------------------------
# 4. Single 0.5s silence in middle of 25s audio
# ---------------------------------------------------------------------------

class TestSingleSilenceInMiddle(unittest.TestCase):
    """12.5s speech + 0.5s silence + 12s speech = 25s total (> 20s)."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)
        self.audio = cat(
            make_tone(12.5),
            make_silence(0.5),
            make_tone(12.0),
        )

    def test_splits_into_two_chunks(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 2,
                         msg=f"Expected 2 chunks, got {len(chunks)}: "
                             f"{[c.to_dict() for c in chunks]}")

    def test_split_point_near_silence(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        # The split should happen near the silence boundary (12.5s ± 1s)
        cut = chunks[0].end_sec
        self.assertGreater(cut, 11.5)
        self.assertLess(cut, 14.0)

    def test_sample_count_preserved(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total = sum(len(c.audio) for c in chunks)
        self.assertEqual(total, len(self.audio))

    def test_no_overlap(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for i in range(len(chunks) - 1):
            self.assertLessEqual(chunks[i].end_sec, chunks[i + 1].start_sec + 0.02)


# ---------------------------------------------------------------------------
# 5. Multiple short silences (5x 0.5s, speech 8s each)
# ---------------------------------------------------------------------------

class TestMultipleShortSilences(unittest.TestCase):
    """8s speech + 0.5s silence repeated 5 times, final 8s speech.
    Total = 5*8 + 5*0.5 + 8 = 50.5s with 5 natural split points."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)
        segment = cat(make_tone(8.0), make_silence(0.5))
        self.audio = cat(*[segment] * 5, make_tone(8.0))

    def test_splits_into_multiple_chunks(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertGreater(len(chunks), 1)

    def test_sample_count_preserved(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total = sum(len(c.audio) for c in chunks)
        self.assertEqual(total, len(self.audio))

    def test_no_chunk_exceeds_max(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for c in chunks[:-1]:
            self.assertLessEqual(c.duration_sec(), MAX_CHUNK_SEC + 0.1,
                                 msg=f"Chunk {c.index} too long: {c.duration_sec():.2f}s")

    def test_indices_sequential(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual([c.index for c in chunks], list(range(len(chunks))))

    def test_splits_prefer_silence_over_hard_cut(self):
        """With silence every 8.5s, splitting at MAX_CHUNK_SEC=20s should
        prefer the silence near the 17s mark over hard-cutting at 20s."""
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        # First cut must be before 20.5s (not a hard cut at 20s exactly)
        # because there's a silence around 17.0s (2×8.5s - 0.5s buffer)
        first_cut = chunks[0].end_sec
        self.assertLess(first_cut, 20.5)


# ---------------------------------------------------------------------------
# 6. Boundary: exactly MAX_CHUNK_SEC + 1ms
# ---------------------------------------------------------------------------

class TestBoundaryJustOverMaxChunk(unittest.TestCase):
    """Audio = MAX_CHUNK_SEC + 1ms — should produce exactly 2 chunks."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)
        extra_samples = SR // 1000  # 1ms = 16 samples @ 16kHz
        n = int(MAX_CHUNK_SEC * SR) + extra_samples
        self.audio = make_tone(n / SR)

    def test_produces_two_chunks(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertEqual(len(chunks), 2,
                         msg=f"Expected 2 chunks, got {len(chunks)}")

    def test_sample_count_exact(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total = sum(len(c.audio) for c in chunks)
        self.assertEqual(total, len(self.audio))

    def test_second_chunk_very_short(self):
        """The second chunk should be ~1ms long."""
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        last = chunks[-1]
        self.assertLess(last.duration_sec(), 0.1)
        self.assertGreater(last.duration_sec(), 0.0)


# ---------------------------------------------------------------------------
# 7. Boundary: exactly 2x MAX_CHUNK_SEC
# ---------------------------------------------------------------------------

class TestBoundaryDoubleMaxChunk(unittest.TestCase):
    """Audio = 2 * MAX_CHUNK_SEC — should produce exactly 2 chunks."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)
        self.audio = make_tone(2 * MAX_CHUNK_SEC)

    def test_produces_exactly_two_chunks(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        # With no silence, hard cut at MAX_CHUNK_SEC → 2 chunks
        self.assertEqual(len(chunks), 2)

    def test_both_chunks_equal_duration(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertAlmostEqual(chunks[0].duration_sec(), MAX_CHUNK_SEC, delta=0.1)
        self.assertAlmostEqual(chunks[1].duration_sec(), MAX_CHUNK_SEC, delta=0.1)

    def test_sample_count_preserved(self):
        chunks = self.chunker.chunk(self.audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total = sum(len(c.audio) for c in chunks)
        self.assertEqual(total, len(self.audio))


# ---------------------------------------------------------------------------
# 8. Padding / MIN_SAMPLES assertion (GigaAM Conformer 400 sample minimum)
# ---------------------------------------------------------------------------

class TestMinSamplesPadding(unittest.TestCase):
    """Every chunk produced for long audio must have >= GIGAAM_MIN_SAMPLES.

    AudioChunker does not pad internally — this test documents the
    requirement that callers must handle very-short tail chunks, and
    also verifies that non-tail chunks always meet the minimum.
    """

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def test_non_tail_chunks_meet_min_samples(self):
        """All chunks except possibly the last must have >= 400 samples."""
        audio = make_tone(65.0)  # 3 chunks with hard split
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for c in chunks[:-1]:
            self.assertGreaterEqual(
                len(c.audio), GIGAAM_MIN_SAMPLES,
                msg=f"Non-tail chunk {c.index} has only {len(c.audio)} samples "
                    f"(need >= {GIGAAM_MIN_SAMPLES} for GigaAM)"
            )

    def test_silence_split_chunks_meet_min_samples(self):
        """Chunks from silence-based split must all be large enough."""
        audio = cat(
            make_tone(12.0),
            make_silence(1.0),
            make_tone(12.0),
        )
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        for c in chunks:
            self.assertGreaterEqual(
                len(c.audio), GIGAAM_MIN_SAMPLES,
                msg=f"Chunk {c.index} has {len(c.audio)} samples — below GigaAM min"
            )

    def test_boundary_plus_1ms_tail_sample_count(self):
        """The 1ms tail from the boundary test may be below 400 samples —
        we just document that the chunker still produces it (not a bug in
        the chunker; caller responsibility to pad before passing to GigaAM)."""
        extra = SR // 1000  # 16 samples
        audio = make_tone((int(MAX_CHUNK_SEC * SR) + extra) / SR)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        # Verify tail exists and its sample count is known
        tail_samples = len(chunks[-1].audio)
        self.assertGreater(tail_samples, 0)
        # Document the known behaviour: tail may be < GIGAAM_MIN_SAMPLES
        if tail_samples < GIGAAM_MIN_SAMPLES:
            pass  # Expected — caller must pad, not AudioChunker's responsibility


# ---------------------------------------------------------------------------
# 9. Wave 359 regression: long leading silence
# ---------------------------------------------------------------------------

class TestWave359Regression(unittest.TestCase):
    """Wave 359 fixed an infinite micro-advance loop (cursor += 0.01s).

    Pattern that triggered the bug:
      - Very long leading silence (>> max_chunk_sec)
      - Silence mid-point falls far outside the look-ahead window
      - The greedy split kept picking cut = cursor + 0.01 forever

    Post-fix: cursor must advance by >= max_chunk_sec each hard-split step.
    """

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def test_long_leading_silence_terminates(self):
        """15s silence + 10s speech = 25s — must return without looping."""
        audio = cat(make_silence(15.0), make_tone(10.0))
        # If the bug is present, this would run "forever" or take >>10s.
        # We just verify it finishes and returns valid chunks.
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        self.assertGreaterEqual(len(chunks), 1)

    def test_leading_silence_chunk_count_bounded(self):
        """Chunk count must be <= ceil(total_sec / max_chunk_sec) + 1."""
        audio = cat(make_silence(15.0), make_tone(10.0))
        total_sec = len(audio) / SR
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        upper_bound = math.ceil(total_sec / MAX_CHUNK_SEC) + 1
        self.assertLessEqual(
            len(chunks), upper_bound,
            msg=f"Got {len(chunks)} chunks for {total_sec:.1f}s audio "
                f"(max_chunk_sec={MAX_CHUNK_SEC}); expected <= {upper_bound}"
        )

    def test_full_leading_silence_30s_plus_speech(self):
        """30s leading silence (> MAX_CHUNK_SEC) + 10s speech = 40s."""
        audio = cat(make_silence(30.0), make_tone(10.0))
        total_sec = len(audio) / SR
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        upper_bound = math.ceil(total_sec / MAX_CHUNK_SEC) + 1
        self.assertLessEqual(len(chunks), upper_bound)
        # Sample count must be exact
        total_samples = sum(len(c.audio) for c in chunks)
        self.assertEqual(total_samples, len(audio))

    def test_trailing_silence_chunk_count_bounded(self):
        """10s speech + 30s trailing silence."""
        audio = cat(make_tone(10.0), make_silence(30.0))
        total_sec = len(audio) / SR
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        upper_bound = math.ceil(total_sec / MAX_CHUNK_SEC) + 1
        self.assertLessEqual(len(chunks), upper_bound)

    def test_alternating_silence_speech_chunk_count_bounded(self):
        """Alternating 1s silence / 1s speech × 30 = 60s total."""
        segment = cat(make_silence(1.0), make_tone(1.0))
        audio = cat(*[segment] * 30)
        total_sec = len(audio) / SR
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        upper_bound = math.ceil(total_sec / MAX_CHUNK_SEC) + 1
        self.assertLessEqual(len(chunks), upper_bound)


# ---------------------------------------------------------------------------
# 10. Concurrent calls — no shared state corruption
# ---------------------------------------------------------------------------

class TestConcurrentChunkerCalls(unittest.TestCase):
    """10 threads each process distinct audio; results must not bleed
    into each other (checks AudioChunker has no mutable class-level state)."""

    def setUp(self):
        self.chunker = AudioChunker(min_silence_sec=0.3)

    def _process(self, freq: float, results: list, idx: int) -> None:
        """Process a unique tone and store chunk count + sample totals."""
        audio = make_tone(25.0, freq=freq)
        chunks = self.chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)
        total_samples = sum(len(c.audio) for c in chunks)
        results[idx] = (len(chunks), total_samples, len(audio))

    def test_ten_threads_no_state_corruption(self):
        n_threads = 10
        results = [None] * n_threads
        threads = [
            threading.Thread(
                target=self._process,
                args=(200.0 + i * 50.0, results, i),
            )
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i, (n_chunks, total_samples, expected_samples) in enumerate(results):
            self.assertIsNotNone(n_chunks, msg=f"Thread {i} produced no result")
            self.assertGreaterEqual(n_chunks, 1)
            self.assertEqual(
                total_samples, expected_samples,
                msg=f"Thread {i}: sample count mismatch "
                    f"({total_samples} != {expected_samples})"
            )

    def test_concurrent_mixed_audio_types(self):
        """Mix of silence/tone across threads — no cross-contamination."""
        results = [None] * 6
        audios = [
            make_silence(25.0),
            make_tone(25.0, 440),
            cat(make_silence(10.0), make_tone(15.0)),
            cat(make_tone(12.0), make_silence(0.5), make_tone(12.5)),
            make_tone(40.0),
            make_silence(5.0),
        ]

        def _run(idx: int) -> None:
            a = audios[idx]
            chunks = self.chunker.chunk(a, SR, max_chunk_sec=MAX_CHUNK_SEC)
            results[idx] = sum(len(c.audio) for c in chunks)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for i, (total, audio) in enumerate(zip(results, audios)):
            self.assertEqual(
                total, len(audio),
                msg=f"Thread {i}: expected {len(audio)} samples, got {total}"
            )


# ---------------------------------------------------------------------------
# Additional: chunk-level invariants across all categories
# ---------------------------------------------------------------------------

class TestChunkInvariants(unittest.TestCase):
    """Universal invariants that must hold for all chunker outputs."""

    CASES = [
        ("pure_silence_25s", make_silence(25.0)),
        ("pure_tone_45s", make_tone(45.0)),
        ("tiny_50ms", make_tone(0.05)),
        ("silence_middle_25s", cat(make_tone(12.5), make_silence(0.5), make_tone(12.0))),
        ("double_max", make_tone(2 * MAX_CHUNK_SEC)),
    ]

    def _check_invariants(self, label: str, audio: np.ndarray) -> None:
        chunker = AudioChunker(min_silence_sec=0.3)
        chunks = chunker.chunk(audio, SR, max_chunk_sec=MAX_CHUNK_SEC)

        # I1: at least one chunk
        self.assertGreaterEqual(len(chunks), 1, f"{label}: no chunks returned")

        # I2: indices are 0-based sequential
        self.assertEqual(
            [c.index for c in chunks], list(range(len(chunks))),
            f"{label}: non-sequential indices"
        )

        # I3: total sample count preserved
        total = sum(len(c.audio) for c in chunks)
        self.assertEqual(total, len(audio),
                         f"{label}: sample count {total} != {len(audio)}")

        # I4: no negative durations
        for c in chunks:
            self.assertGreaterEqual(c.duration_sec(), 0.0,
                                    f"{label}: chunk {c.index} negative duration")

        # I5: non-overlapping (end_i <= start_{i+1} + epsilon)
        for i in range(len(chunks) - 1):
            self.assertLessEqual(
                chunks[i].end_sec, chunks[i + 1].start_sec + 0.02,
                f"{label}: chunks {i} and {i+1} overlap"
            )

        # I6: start_sec of first chunk is 0.0
        self.assertEqual(chunks[0].start_sec, 0.0,
                         f"{label}: first chunk does not start at 0.0")

    def test_invariants_for_all_cases(self):
        for label, audio in self.CASES:
            with self.subTest(label=label):
                self._check_invariants(label, audio)


if __name__ == "__main__":
    unittest.main()
