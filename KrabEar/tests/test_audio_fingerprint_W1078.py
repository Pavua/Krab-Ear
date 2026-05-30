"""Tests for W1078 fix: AudioFingerprinter restricted to exact-match (W1063 CRITICAL).

Covers:
- equals(): True for identical input, False for different input
- compare(): emits DeprecationWarning; returns 1.0 for equal, 0.0 for different
"""

from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.audio_fingerprint import AudioFingerprinter


def _sine(freq: float = 440.0, duration: float = 0.5, sr: int = 16000) -> np.ndarray:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


class TestAudioFingerprinterW1078(unittest.TestCase):
    """W1078 — equals() + deprecated compare() shim."""

    def setUp(self) -> None:
        self.fp = AudioFingerprinter()
        self.sr = 16000

    # ── equals() ─────────────────────────────────────────────────────────────

    def test_equals_identical_input_returns_true(self) -> None:
        """equals() returns True when both fingerprints come from the same audio."""
        audio = _sine(440.0, duration=0.5, sr=self.sr)
        h = self.fp.fingerprint(audio, self.sr)
        self.assertTrue(self.fp.equals(h, h))

    def test_equals_identical_audio_copy_returns_true(self) -> None:
        """equals() returns True for two fingerprints of identical audio arrays."""
        audio = _sine(440.0, duration=0.5, sr=self.sr)
        h1 = self.fp.fingerprint(audio, self.sr)
        h2 = self.fp.fingerprint(audio.copy(), self.sr)
        self.assertTrue(self.fp.equals(h1, h2))

    def test_equals_different_input_returns_false(self) -> None:
        """equals() returns False for fingerprints of different audio signals."""
        h1 = self.fp.fingerprint(_sine(440.0), self.sr)
        h2 = self.fp.fingerprint(_sine(880.0), self.sr)
        self.assertFalse(self.fp.equals(h1, h2))

    def test_equals_empty_string_returns_false(self) -> None:
        """equals() returns False when either fingerprint is empty."""
        h = self.fp.fingerprint(_sine(440.0), self.sr)
        self.assertFalse(self.fp.equals("", ""))
        self.assertFalse(self.fp.equals(h, ""))
        self.assertFalse(self.fp.equals("", h))

    def test_equals_returns_bool(self) -> None:
        """equals() return type is bool."""
        h = self.fp.fingerprint(_sine(440.0), self.sr)
        result = self.fp.equals(h, h)
        self.assertIsInstance(result, bool)

    # ── compare() deprecated shim ─────────────────────────────────────────────

    def test_compare_emits_deprecation_warning(self) -> None:
        """compare() emits DeprecationWarning on every call."""
        h1 = self.fp.fingerprint(_sine(440.0), self.sr)
        h2 = self.fp.fingerprint(_sine(880.0), self.sr)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.fp.compare(h1, h2)
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in caught),
            msg="compare() must emit DeprecationWarning",
        )

    def test_compare_deprecation_warning_mentions_equals(self) -> None:
        """DeprecationWarning message recommends equals() as replacement."""
        h = self.fp.fingerprint(_sine(440.0), self.sr)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.fp.compare(h, h)
        messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertTrue(
            any("equals" in msg for msg in messages),
            msg=f"Warning should mention 'equals'. Got: {messages}",
        )

    def test_compare_identical_returns_1_0(self) -> None:
        """compare() shim returns 1.0 for identical fingerprints."""
        audio = _sine(440.0)
        h = self.fp.fingerprint(audio, self.sr)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = self.fp.compare(h, h)
        self.assertEqual(result, 1.0)

    def test_compare_different_returns_0_0(self) -> None:
        """compare() shim returns 0.0 for different fingerprints (not a gradient)."""
        h1 = self.fp.fingerprint(_sine(440.0), self.sr)
        h2 = self.fp.fingerprint(_sine(880.0), self.sr)
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = self.fp.compare(h1, h2)
        self.assertEqual(result, 0.0)

    def test_compare_empty_returns_0_0(self) -> None:
        """compare() shim returns 0.0 for empty inputs."""
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            self.assertEqual(self.fp.compare("", ""), 0.0)

    def test_compare_no_intermediate_float_values(self) -> None:
        """compare() shim never returns values strictly between 0.0 and 1.0."""
        import numpy.random as npr
        rng = npr.default_rng(42)
        signals = [
            _sine(freq, duration=0.3, sr=self.sr)
            for freq in [200.0, 440.0, 880.0, 1200.0]
        ] + [rng.standard_normal(4800).astype(np.float32) for _ in range(3)]

        hashes = [self.fp.fingerprint(s, self.sr) for s in signals]
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            for i, h1 in enumerate(hashes):
                for j, h2 in enumerate(hashes):
                    result = self.fp.compare(h1, h2)
                    self.assertIn(
                        result, (0.0, 1.0),
                        msg=f"compare() returned {result!r} for pair ({i},{j}) — "
                            f"only 0.0 and 1.0 are valid",
                    )

    # ── is_duplicate_audio() uses equals() internally ────────────────────────

    def test_is_duplicate_uses_exact_match(self) -> None:
        """is_duplicate_audio() uses exact match (equals), not Hamming distance."""
        audio = _sine(440.0)
        # Identical audio is always a duplicate
        self.assertTrue(self.fp.is_duplicate_audio(audio, audio, sample_rate=self.sr))
        # Different audio is never a duplicate
        other = _sine(880.0)
        self.assertFalse(
            self.fp.is_duplicate_audio(audio, other, sample_rate=self.sr, threshold=0.5)
        )

    def test_is_duplicate_threshold_zero_ignored(self) -> None:
        """W1063: threshold parameter is ignored by is_duplicate_audio(); exact-match only.

        Different audio is never a duplicate regardless of threshold value.
        """
        a1 = _sine(440.0)
        a2 = _sine(880.0)
        # threshold=0.0 is a no-op — exact SHA-256 match required
        self.assertFalse(
            self.fp.is_duplicate_audio(a1, a2, sample_rate=self.sr, threshold=0.0)
        )


if __name__ == "__main__":
    unittest.main()
