"""Tests for W1015 F1 HIGH fix: NaN/Inf audio inputs must produce JSON-safe output.

RFC 8259 forbids literal NaN and Infinity tokens in JSON. json.dumps() in
Python emits them by default when float('nan') / float('inf') appear in a
dict, causing Swift JSONDecoder to crash on the receiving end.
"""

import json
import math
import sys
import os
import unittest

import numpy as np

# ---------------------------------------------------------------------------
# Path setup — allow running from repo root or directly
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_KRAB_EAR_ROOT = os.path.dirname(_HERE)  # KrabEar/
if _KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, _KRAB_EAR_ROOT)

from core.audio_quality import AudioQualityAnalyzer, _safe_float


class SafeFloatHelperTest(unittest.TestCase):
    """Unit tests for the _safe_float helper itself."""

    def test_finite_value_unchanged(self):
        self.assertAlmostEqual(_safe_float(3.14), 3.14)

    def test_nan_replaced_by_default(self):
        result = _safe_float(float("nan"))
        self.assertEqual(result, 0.0)
        self.assertTrue(math.isfinite(result))

    def test_pos_inf_replaced_by_default(self):
        result = _safe_float(float("inf"))
        self.assertEqual(result, 0.0)

    def test_neg_inf_replaced_by_default(self):
        result = _safe_float(float("-inf"))
        self.assertEqual(result, 0.0)

    def test_custom_default(self):
        self.assertEqual(_safe_float(float("nan"), default=42.0), 42.0)

    def test_zero_is_finite(self):
        self.assertEqual(_safe_float(0.0), 0.0)

    def test_negative_finite(self):
        self.assertAlmostEqual(_safe_float(-5.5), -5.5)

    def test_integer_input(self):
        self.assertEqual(_safe_float(7), 7)


class NaNAudioInputTest(unittest.TestCase):
    """Verify that NaN-filled audio produces JSON-safe output (W1015 F1)."""

    def setUp(self):
        self.analyzer = AudioQualityAnalyzer()

    def test_nan_audio_produces_json_safe_output(self):
        """json.dumps(result) must not raise and must not contain NaN tokens."""
        nan_audio = np.array([np.nan] * 16000, dtype=np.float32)
        report = self.analyzer.analyze(nan_audio, sample_rate=16000)
        result_dict = report.to_dict()

        # Must serialise without ValueError
        serialised = json.dumps(result_dict, ensure_ascii=False)

        # RFC 8259: literal NaN / Infinity must not appear in the JSON string
        self.assertNotIn("NaN", serialised)
        self.assertNotIn("Infinity", serialised)
        self.assertNotIn("nan", serialised)
        self.assertNotIn("inf", serialised.lower().replace('"', ""))

        # All numeric fields must be finite Python floats
        numeric_keys = [
            "rms_level", "peak_level", "snr_estimate_db",
            "clipping_ratio", "silence_ratio", "duration_sec",
        ]
        for key in numeric_keys:
            val = result_dict[key]
            self.assertIsInstance(val, (int, float), msg=f"{key} must be numeric")
            self.assertTrue(
                math.isfinite(val),
                msg=f"{key}={val!r} is not finite — would cause Swift JSONDecoder crash",
            )

    def test_inf_audio_clamped(self):
        """Infinity-filled audio must also produce finite, JSON-safe output."""
        inf_audio = np.array([np.inf, -np.inf, np.inf, -np.inf] * 4000, dtype=np.float64)
        report = self.analyzer.analyze(inf_audio, sample_rate=16000)
        result_dict = report.to_dict()

        serialised = json.dumps(result_dict, ensure_ascii=False)
        self.assertNotIn("Infinity", serialised)
        self.assertNotIn("NaN", serialised)

        numeric_keys = [
            "rms_level", "peak_level", "snr_estimate_db",
            "clipping_ratio", "silence_ratio", "duration_sec",
        ]
        for key in numeric_keys:
            val = result_dict[key]
            self.assertTrue(
                math.isfinite(val),
                msg=f"{key}={val!r} is not finite for Inf input",
            )

    def test_mixed_nan_inf_audio(self):
        """Mixed NaN and Inf values in audio must not produce non-finite outputs."""
        mixed = np.array([np.nan, np.inf, -np.inf, 0.5, np.nan] * 3200, dtype=np.float64)
        report = self.analyzer.analyze(mixed, sample_rate=16000)
        result_dict = report.to_dict()
        serialised = json.dumps(result_dict, ensure_ascii=False)
        self.assertNotIn("NaN", serialised)
        self.assertNotIn("Infinity", serialised)

    def test_normal_audio_still_works(self):
        """Regression: normal clean audio should still produce reasonable metrics."""
        rng = np.random.default_rng(42)
        clean_audio = rng.standard_normal(16000).astype(np.float32) * 0.3
        report = self.analyzer.analyze(clean_audio, sample_rate=16000)

        self.assertIn(report.quality_score, {"excellent", "good", "fair", "poor"})
        self.assertGreater(report.rms_level, 0.0)
        self.assertGreater(report.peak_level, 0.0)
        self.assertTrue(math.isfinite(report.snr_estimate_db))

        # Must still serialise cleanly
        serialised = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertNotIn("NaN", serialised)

    def test_empty_audio_is_json_safe(self):
        """Empty array (n_samples=0) edge case must also be JSON-safe."""
        empty = np.array([], dtype=np.float32)
        report = self.analyzer.analyze(empty, sample_rate=16000)
        serialised = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertNotIn("NaN", serialised)
        self.assertNotIn("Infinity", serialised)


if __name__ == "__main__":
    unittest.main(verbosity=2)
