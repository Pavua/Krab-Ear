"""wave-34 B1/B2/B3: RealtimeSilenceFilter settings clamp + SettingsValidator _RANGE_FIELDS.

Tests:
  B1 (MED) - check_sec=0 -> clamped to 0.5 (no busy-loop from Event.wait(0)).
  B1 (MED) - check_sec=nan/inf -> falls back to default, clamped.
  B2 (MED) - window_sec=-1 -> clamped to 1.0.
  B2 (MED) - window_sec=inf -> falls back to default.
  B3 (LOW)  - threshold_db=400 -> clamped to -10.0.
  B3 (LOW)  - threshold_db=-200 -> clamped to -80.0.
  B3 (LOW)  - threshold_db=nan -> falls back to default, clamped in [-80, -10].
  SettingsValidator: rt_silence_check_sec 0 -> clamped to 0.5.
  SettingsValidator: rt_silence_window_sec -1 -> clamped to 1.0.
  SettingsValidator: realtime_silence_threshold_db 400 -> clamped to -10.0.
  SettingsValidator: rt_partial_interval_sec 0 -> clamped to 0.1.
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from unittest.mock import MagicMock

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _make_filter(settings: dict):
    """Build RealtimeSilenceFilter with a minimal mock recorder."""
    from backend.realtime_silence_filter import RealtimeSilenceFilter
    recorder = MagicMock()
    recorder.is_recording = False
    return RealtimeSilenceFilter(recorder=recorder, settings=settings)


class TestCheckSecClamp(unittest.TestCase):
    """B1 (MED): rt_silence_check_sec unbounded -> CPU spin fix."""

    def test_zero_clamped_to_half_second(self):
        f = _make_filter({"rt_silence_check_sec": 0})
        self.assertGreaterEqual(f._check_sec, 0.5)

    def test_negative_clamped(self):
        f = _make_filter({"rt_silence_check_sec": -100})
        self.assertGreaterEqual(f._check_sec, 0.5)

    def test_nan_uses_default_then_clamps(self):
        f = _make_filter({"rt_silence_check_sec": float("nan")})
        self.assertTrue(math.isfinite(f._check_sec))
        self.assertGreaterEqual(f._check_sec, 0.5)

    def test_inf_uses_default_then_clamps(self):
        f = _make_filter({"rt_silence_check_sec": float("inf")})
        self.assertTrue(math.isfinite(f._check_sec))
        self.assertGreaterEqual(f._check_sec, 0.5)

    def test_normal_value_preserved(self):
        f = _make_filter({"rt_silence_check_sec": 3.0})
        self.assertEqual(f._check_sec, 3.0)

    def test_default_value_used_when_absent(self):
        f = _make_filter({})
        # default is 5.0, above the 0.5 floor
        self.assertGreaterEqual(f._check_sec, 0.5)
        self.assertTrue(math.isfinite(f._check_sec))


class TestWindowSecClamp(unittest.TestCase):
    """B2 (MED): rt_silence_window_sec<=0 -> full-buffer copy every tick fix."""

    def test_negative_clamped_to_one(self):
        f = _make_filter({"rt_silence_window_sec": -1})
        self.assertGreaterEqual(f._window_sec, 1.0)

    def test_zero_clamped_to_one(self):
        f = _make_filter({"rt_silence_window_sec": 0})
        self.assertGreaterEqual(f._window_sec, 1.0)

    def test_nan_uses_default_then_clamps(self):
        f = _make_filter({"rt_silence_window_sec": float("nan")})
        self.assertTrue(math.isfinite(f._window_sec))
        self.assertGreaterEqual(f._window_sec, 1.0)

    def test_inf_uses_default_then_clamps(self):
        f = _make_filter({"rt_silence_window_sec": float("inf")})
        self.assertTrue(math.isfinite(f._window_sec))
        self.assertGreaterEqual(f._window_sec, 1.0)

    def test_normal_value_preserved(self):
        f = _make_filter({"rt_silence_window_sec": 8.0})
        self.assertEqual(f._window_sec, 8.0)


class TestThresholdDbClamp(unittest.TestCase):
    """B3 (LOW): realtime_silence_threshold_db huge positive -> all speech silent fix."""

    def test_huge_positive_clamped_to_minus10(self):
        f = _make_filter({"realtime_silence_threshold_db": 400})
        self.assertEqual(f._threshold_db, -10.0)

    def test_minus_10_allowed(self):
        f = _make_filter({"realtime_silence_threshold_db": -10.0})
        self.assertEqual(f._threshold_db, -10.0)

    def test_too_negative_clamped_to_minus80(self):
        f = _make_filter({"realtime_silence_threshold_db": -200})
        self.assertEqual(f._threshold_db, -80.0)

    def test_minus_80_allowed(self):
        f = _make_filter({"realtime_silence_threshold_db": -80.0})
        self.assertEqual(f._threshold_db, -80.0)

    def test_nan_uses_default_in_range(self):
        f = _make_filter({"realtime_silence_threshold_db": float("nan")})
        self.assertTrue(math.isfinite(f._threshold_db))
        self.assertGreaterEqual(f._threshold_db, -80.0)
        self.assertLessEqual(f._threshold_db, -10.0)

    def test_normal_value_preserved(self):
        f = _make_filter({"realtime_silence_threshold_db": -55.0})
        self.assertEqual(f._threshold_db, -55.0)

    def test_default_in_valid_range(self):
        f = _make_filter({})
        self.assertGreaterEqual(f._threshold_db, -80.0)
        self.assertLessEqual(f._threshold_db, -10.0)


class TestSettingsValidatorRangeFields(unittest.TestCase):
    """SettingsValidator._RANGE_FIELDS covers rt_silence_* keys (wave-34)."""

    def setUp(self):
        from backend.settings_validator import SettingsValidator
        self.v = SettingsValidator()

    def test_check_sec_zero_clamped(self):
        result = self.v.validate({"rt_silence_check_sec": 0})
        self.assertTrue(result.valid)
        self.assertGreaterEqual(result.fixed["rt_silence_check_sec"], 0.5)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("rt_silence_check_sec", result.warnings[0])

    def test_window_sec_negative_clamped(self):
        result = self.v.validate({"rt_silence_window_sec": -1})
        self.assertTrue(result.valid)
        self.assertGreaterEqual(result.fixed["rt_silence_window_sec"], 1.0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("rt_silence_window_sec", result.warnings[0])

    def test_threshold_db_huge_positive_clamped(self):
        result = self.v.validate({"realtime_silence_threshold_db": 400})
        self.assertTrue(result.valid)
        self.assertLessEqual(result.fixed["realtime_silence_threshold_db"], -10.0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("realtime_silence_threshold_db", result.warnings[0])

    def test_threshold_db_too_negative_clamped(self):
        result = self.v.validate({"realtime_silence_threshold_db": -200})
        self.assertTrue(result.valid)
        self.assertGreaterEqual(result.fixed["realtime_silence_threshold_db"], -80.0)

    def test_partial_interval_sec_zero_clamped(self):
        result = self.v.validate({"rt_partial_interval_sec": 0})
        self.assertTrue(result.valid)
        self.assertGreaterEqual(result.fixed["rt_partial_interval_sec"], 0.1)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("rt_partial_interval_sec", result.warnings[0])

    def test_valid_values_pass_through(self):
        settings = {
            "rt_silence_check_sec": 2.0,
            "rt_silence_window_sec": 10.0,
            "realtime_silence_threshold_db": -55.0,
            "rt_partial_interval_sec": 1.0,
        }
        result = self.v.validate(settings)
        self.assertTrue(result.valid)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.fixed["rt_silence_check_sec"], 2.0)
        self.assertEqual(result.fixed["rt_silence_window_sec"], 10.0)
        self.assertEqual(result.fixed["realtime_silence_threshold_db"], -55.0)
        self.assertEqual(result.fixed["rt_partial_interval_sec"], 1.0)

    def test_nan_check_sec_uses_default(self):
        result = self.v.validate({"rt_silence_check_sec": float("nan")})
        self.assertTrue(result.valid)
        val = result.fixed["rt_silence_check_sec"]
        self.assertTrue(math.isfinite(val))
        self.assertGreaterEqual(val, 0.5)

    def test_nan_threshold_db_uses_default(self):
        result = self.v.validate({"realtime_silence_threshold_db": float("nan")})
        self.assertTrue(result.valid)
        val = result.fixed["realtime_silence_threshold_db"]
        self.assertTrue(math.isfinite(val))
        self.assertGreaterEqual(val, -80.0)
        self.assertLessEqual(val, -10.0)


if __name__ == "__main__":
    unittest.main()
