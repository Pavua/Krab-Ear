"""Tests for audio_fingerprint edge-case fixes (wave-21).

Covers:
  FINDING 1: empty/single-sample buffer → no ValueError from np.max on zero-size array
  FINDING 2: sample_rate=0 → no ZeroDivisionError in _extract_features
  FINDING 3: NaN/Inf audio must NOT produce same fingerprint as silence
              (false-duplicate data-loss risk)
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402
from core.audio_fingerprint import AudioFingerprinter  # noqa: E402


class TestEmptyAndSingleSampleBuffer(unittest.TestCase):
    """FINDING 1 — empty/short buffers must not crash."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()

    def test_empty_array_no_exception(self) -> None:
        """fingerprint(np.array([]), 16000) must not raise ValueError."""
        result = self.fp.fingerprint(np.array([], dtype=np.float32), 16000)
        # Empty audio is all-silence → returns a valid hash string (not None)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)

    def test_single_sample_no_exception(self) -> None:
        """fingerprint with a single sample must not crash."""
        result = self.fp.fingerprint(np.array([0.5], dtype=np.float32), 16000)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)

    def test_single_zero_sample_no_exception(self) -> None:
        """fingerprint with a single zero sample must not crash."""
        result = self.fp.fingerprint(np.array([0.0], dtype=np.float32), 16000)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)

    def test_two_samples_no_exception(self) -> None:
        """fingerprint with two samples must not crash."""
        result = self.fp.fingerprint(np.array([0.1, -0.1], dtype=np.float32), 16000)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)

    def test_to_mono_empty_returns_zeros(self) -> None:
        """_to_mono_float32 on an empty array returns a 1-element zero array (not None)."""
        arr = AudioFingerprinter._to_mono_float32(np.array([], dtype=np.float32))
        self.assertIsNotNone(arr)
        self.assertEqual(arr.size, 1)
        self.assertAlmostEqual(float(arr[0]), 0.0)


class TestSampleRateZero(unittest.TestCase):
    """FINDING 2 — sample_rate=0 must not raise ZeroDivisionError."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()

    def test_sr_zero_no_exception(self) -> None:
        """fingerprint(audio, sample_rate=0) must not raise ZeroDivisionError."""
        audio = np.random.default_rng(42).uniform(-1.0, 1.0, 1024).astype(np.float32)
        result = self.fp.fingerprint(audio, 0)
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)

    def test_sr_zero_deterministic(self) -> None:
        """Same audio with sr=0 must produce the same hash each time."""
        audio = np.ones(512, dtype=np.float32) * 0.5
        fp1 = self.fp.fingerprint(audio, 0)
        fp2 = self.fp.fingerprint(audio, 0)
        self.assertEqual(fp1, fp2)

    def test_sr_negative_no_exception(self) -> None:
        """fingerprint with a negative sample_rate must not raise."""
        audio = np.ones(512, dtype=np.float32) * 0.3
        result = self.fp.fingerprint(audio, -1)
        self.assertIsInstance(result, str)

    def test_extract_features_sr_zero_no_exception(self) -> None:
        """_extract_features with sr=0 must not raise ZeroDivisionError."""
        fp_obj = AudioFingerprinter()
        mono = np.ones(512, dtype=np.float32) * 0.3
        features = fp_obj._extract_features(mono, 0)
        self.assertEqual(len(features), 4)
        for f in features:
            self.assertIsInstance(f, float)


class TestNonFiniteAudioNotFalseDuplicate(unittest.TestCase):
    """FINDING 3 — NaN/Inf audio must NOT produce same fingerprint as silence."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self._rng = np.random.default_rng(1234)

    def _silence(self, n: int = 1024) -> np.ndarray:
        return np.zeros(n, dtype=np.float32)

    def _nan_audio(self, n: int = 1024) -> np.ndarray:
        arr = np.full(n, float("nan"), dtype=np.float32)
        return arr

    def _inf_audio(self, n: int = 1024) -> np.ndarray:
        arr = np.full(n, float("inf"), dtype=np.float32)
        return arr

    def _neg_inf_audio(self, n: int = 1024) -> np.ndarray:
        return np.full(1024, float("-inf"), dtype=np.float32)

    def _mixed_nan_inf(self, n: int = 1024) -> np.ndarray:
        arr = np.zeros(n, dtype=np.float32)
        arr[0] = float("nan")
        arr[100] = float("inf")
        return arr

    def test_nan_fingerprint_is_none(self) -> None:
        """All-NaN audio must return None fingerprint, not a hash string."""
        result = self.fp.fingerprint(self._nan_audio(), 16000)
        self.assertIsNone(result, "NaN audio fingerprint must be None, not a hash")

    def test_inf_fingerprint_is_none(self) -> None:
        """All-Inf audio must return None fingerprint."""
        result = self.fp.fingerprint(self._inf_audio(), 16000)
        self.assertIsNone(result, "Inf audio fingerprint must be None, not a hash")

    def test_neg_inf_fingerprint_is_none(self) -> None:
        """-Inf audio must return None fingerprint."""
        result = self.fp.fingerprint(self._neg_inf_audio(), 16000)
        self.assertIsNone(result)

    def test_mixed_nan_inf_fingerprint_is_none(self) -> None:
        """Audio with any NaN/Inf sample must return None."""
        result = self.fp.fingerprint(self._mixed_nan_inf(), 16000)
        self.assertIsNone(result)

    def test_nan_vs_silence_not_duplicate(self) -> None:
        """NaN audio and silence audio must NOT be considered duplicates."""
        fp_nan = self.fp.fingerprint(self._nan_audio(), 16000)
        fp_silence = self.fp.fingerprint(self._silence(), 16000)
        # fp_nan is None; equals() returns False for None inputs
        self.assertFalse(
            self.fp.equals(fp_nan, fp_silence),
            "NaN audio and silence must NOT be duplicates",
        )

    def test_inf_vs_silence_not_duplicate(self) -> None:
        """Inf audio and silence audio must NOT be considered duplicates."""
        fp_inf = self.fp.fingerprint(self._inf_audio(), 16000)
        fp_silence = self.fp.fingerprint(self._silence(), 16000)
        self.assertFalse(self.fp.equals(fp_inf, fp_silence))

    def test_nan_vs_nan_not_duplicate(self) -> None:
        """Two NaN fingerprints (both None) must NOT equal each other (prevents phantom dedupe)."""
        fp1 = self.fp.fingerprint(self._nan_audio(512), 16000)
        fp2 = self.fp.fingerprint(self._nan_audio(1024), 16000)
        self.assertFalse(
            self.fp.equals(fp1, fp2),
            "Two None fingerprints must not be considered duplicates",
        )

    def test_is_duplicate_audio_nan_vs_silence_false(self) -> None:
        """is_duplicate_audio must return False for NaN vs silence."""
        self.assertFalse(
            self.fp.is_duplicate_audio(self._nan_audio(), self._silence())
        )

    def test_to_mono_nan_returns_none(self) -> None:
        """_to_mono_float32 on NaN array must return None."""
        arr = AudioFingerprinter._to_mono_float32(self._nan_audio())
        self.assertIsNone(arr)

    def test_to_mono_inf_returns_none(self) -> None:
        """_to_mono_float32 on Inf array must return None."""
        arr = AudioFingerprinter._to_mono_float32(self._inf_audio())
        self.assertIsNone(arr)


class TestNormalAudioStillWorks(unittest.TestCase):
    """Regression: normal audio fingerprinting must remain deterministic."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self._rng = np.random.default_rng(99)

    def test_normal_audio_deterministic(self) -> None:
        audio = self._rng.uniform(-1.0, 1.0, 16000).astype(np.float32)
        fp1 = self.fp.fingerprint(audio, 16000)
        fp2 = self.fp.fingerprint(audio, 16000)
        self.assertIsNotNone(fp1)
        self.assertEqual(fp1, fp2)

    def test_two_different_normal_audios_differ(self) -> None:
        """Two genuinely different recordings must not be falsely deduplicated."""
        rng = np.random.default_rng(7)
        a1 = rng.uniform(-1.0, 1.0, 16000).astype(np.float32)
        a2 = rng.uniform(-1.0, 1.0, 16000).astype(np.float32)
        fp1 = self.fp.fingerprint(a1, 16000)
        fp2 = self.fp.fingerprint(a2, 16000)
        # Very unlikely to collide for random audio
        self.assertNotEqual(fp1, fp2)

    def test_silence_fingerprints_deterministically(self) -> None:
        """All-zeros audio must fingerprint to the same hash every time."""
        zeros = np.zeros(8192, dtype=np.float32)
        fp1 = self.fp.fingerprint(zeros, 16000)
        fp2 = self.fp.fingerprint(zeros, 16000)
        self.assertIsNotNone(fp1)
        self.assertEqual(fp1, fp2)

    def test_short_normal_buffer_no_exception(self) -> None:
        """A buffer shorter than window_size must still produce a valid fingerprint."""
        audio = np.sin(np.linspace(0, 2 * np.pi, 100)).astype(np.float32)
        result = self.fp.fingerprint(audio, 16000)
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 64)


if __name__ == "__main__":
    unittest.main()
