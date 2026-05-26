"""Tests W1107: audio_quality.py uses unified SILENCE_THRESHOLD_AMP from silence_detector.

Covers:
- test_silence_ratio_matches_silence_detector_baseline
- test_snr_quiet_mask_matches_silence_detector
- test_no_internal_silence_constant_drift
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.audio_quality import AudioQualityAnalyzer, _SILENCE_RMS_THRESHOLD
from core.silence_detector import SILENCE_THRESHOLD_AMP


class TestSilenceRatioMatchesSilenceDetectorBaseline(unittest.TestCase):
    """_compute_silence_ratio uses SILENCE_THRESHOLD_AMP (0.01) — not 0.001."""

    def setUp(self):
        self.analyzer = AudioQualityAnalyzer()

    def test_silence_ratio_uses_unified_threshold(self):
        """Frame with RMS between 0.001 and 0.01 is counted as silent under new threshold."""
        # Construct audio whose frame RMS ≈ 0.005 (between old 0.001 and new 0.01)
        sr = 16000
        duration = 1.0
        n_samples = int(sr * duration)
        # Amplitude 0.005 * sqrt(2) ≈ 0.00707 → RMS ≈ 0.005
        audio = np.full(n_samples, 0.005 * (2 ** 0.5), dtype=np.float32) * np.sign(
            np.sin(2 * np.pi * 440 * np.arange(n_samples) / sr)
        )
        # With old threshold (0.001) this would NOT be silent (0.005 > 0.001)
        # With unified threshold (0.01) this IS silent (0.005 < 0.01)
        silence_ratio = self.analyzer._compute_silence_ratio(audio.astype(np.float64))
        # Expect high silence ratio since RMS (~0.005) < SILENCE_THRESHOLD_AMP (0.01)
        self.assertGreater(
            silence_ratio,
            0.5,
            f"Expected high silence_ratio with unified threshold; got {silence_ratio}",
        )

    def test_loud_audio_not_silent(self):
        """Audio with RMS well above SILENCE_THRESHOLD_AMP has low silence ratio."""
        sr = 16000
        audio = np.random.uniform(-0.5, 0.5, 16000).astype(np.float64)
        silence_ratio = self.analyzer._compute_silence_ratio(audio)
        self.assertLess(silence_ratio, 0.1, f"Expected low silence_ratio; got {silence_ratio}")

    def test_truly_silent_audio(self):
        """All-zero audio produces silence_ratio of 1.0."""
        audio = np.zeros(16000, dtype=np.float64)
        self.assertEqual(self.analyzer._compute_silence_ratio(audio), 1.0)


class TestSnrQuietMaskMatchesSilenceDetector(unittest.TestCase):
    """_estimate_snr quiet_mask uses SILENCE_THRESHOLD_AMP directly (no * 10 factor)."""

    def setUp(self):
        self.analyzer = AudioQualityAnalyzer()

    def test_snr_returns_finite_value(self):
        """_estimate_snr returns a finite float for typical audio."""
        sr = 16000
        rng = np.random.default_rng(42)
        signal = rng.uniform(-0.3, 0.3, sr * 2).astype(np.float64)
        snr = self.analyzer._estimate_snr(signal, sr)
        self.assertTrue(np.isfinite(snr), f"SNR should be finite; got {snr}")
        self.assertGreaterEqual(snr, -20.0)
        self.assertLessEqual(snr, 80.0)

    def test_snr_quiet_mask_uses_unified_threshold(self):
        """Frames at 0.005 amplitude (between old*10=0.01 and new 0.01) drive noise floor."""
        sr = 16000
        frame_size = 1024
        # Build signal: first half is noise at amp 0.005, second half is louder signal
        quiet_frames = np.full(frame_size * 8, 0.005 / (2 ** 0.5), dtype=np.float64)
        loud_frames = np.random.default_rng(0).uniform(-0.3, 0.3, frame_size * 8)
        audio = np.concatenate([quiet_frames, loud_frames.astype(np.float64)])

        snr = self.analyzer._estimate_snr(audio, sr)
        # With quiet_mask threshold = 0.01, quiet_frames (RMS ≈ 0.005) are detected as noise
        # → SNR estimate > 0 (signal clearly louder than noise)
        self.assertGreater(snr, 0, f"Expected SNR > 0 with proper quiet mask; got {snr}")


class TestNoInternalSilenceConstantDrift(unittest.TestCase):
    """AST audit: audio_quality.py must not define its own silence RMS literal."""

    def test_no_raw_0001_literal_for_silence(self):
        """The old magic number 0.001 must not appear as a silence threshold assignment."""
        src_path = PROJECT_ROOT / "core" / "audio_quality.py"
        source = src_path.read_text(encoding="utf-8")
        # 0.001 should not appear as _SILENCE_RMS_THRESHOLD value assignment
        self.assertNotIn(
            "_SILENCE_RMS_THRESHOLD = 0.001",
            source,
            "Found old hard-coded silence threshold 0.001 — should be replaced by SILENCE_THRESHOLD_AMP",
        )

    def test_silence_rms_threshold_equals_silence_threshold_amp(self):
        """_SILENCE_RMS_THRESHOLD in audio_quality == SILENCE_THRESHOLD_AMP at runtime."""
        self.assertAlmostEqual(
            _SILENCE_RMS_THRESHOLD,
            SILENCE_THRESHOLD_AMP,
            places=10,
            msg="_SILENCE_RMS_THRESHOLD must equal SILENCE_THRESHOLD_AMP",
        )

    def test_silence_threshold_amp_value_is_0_01(self):
        """SILENCE_THRESHOLD_AMP canonical value is 0.01 (-40 dB)."""
        self.assertAlmostEqual(SILENCE_THRESHOLD_AMP, 0.01, places=10)

    def test_no_multiplied_threshold_in_estimate_snr(self):
        """AST: _estimate_snr must NOT use _SILENCE_RMS_THRESHOLD * 10."""
        src_path = PROJECT_ROOT / "core" / "audio_quality.py"
        source = src_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        class MultiplierVisitor(ast.NodeVisitor):
            def __init__(self):
                self.found = False

            def visit_BinOp(self, node):
                # Looking for _SILENCE_RMS_THRESHOLD * 10
                if isinstance(node.op, ast.Mult):
                    names = {
                        n.id for n in ast.walk(node) if isinstance(n, ast.Name)
                    }
                    numbers = {
                        n.value for n in ast.walk(node)
                        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                    }
                    if "_SILENCE_RMS_THRESHOLD" in names and 10 in numbers:
                        self.found = True
                self.generic_visit(node)

        visitor = MultiplierVisitor()
        visitor.visit(tree)
        self.assertFalse(
            visitor.found,
            "_estimate_snr still uses _SILENCE_RMS_THRESHOLD * 10 — should use threshold directly",
        )

    def test_import_silence_threshold_amp_present(self):
        """audio_quality.py imports SILENCE_THRESHOLD_AMP from core.silence_detector."""
        src_path = PROJECT_ROOT / "core" / "audio_quality.py"
        source = src_path.read_text(encoding="utf-8")
        self.assertIn(
            "SILENCE_THRESHOLD_AMP",
            source,
            "audio_quality.py must import SILENCE_THRESHOLD_AMP from core.silence_detector",
        )


if __name__ == "__main__":
    unittest.main()
