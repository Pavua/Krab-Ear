"""
test_confidence_calibrator.py — Unit-тесты для ConfidenceCalibrator.
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.confidence_calibrator import (
    ConfidenceCalibrator,
    CalibratedScore,
    PRIMARY_LANGUAGES,
)


class TestCalibratorShortRecording(unittest.TestCase):
    """Короткая запись (<2s) → -10%"""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_short_recording_reduces_confidence(self):
        result = self.cal.calibrate(0.90, duration_sec=1.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.80, places=4)

    def test_short_recording_adjustment_label(self):
        score = self.cal.calibrate_detailed(0.80, duration_sec=0.5, language="ru", model="mlx-whisper-max")
        self.assertTrue(any("short_recording" in a for a in score.adjustments))

    def test_exactly_2s_is_not_short(self):
        # Граница: 2.0s НЕ считается коротким
        result = self.cal.calibrate(0.90, duration_sec=2.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.90, places=4)


class TestCalibratorLongRecording(unittest.TestCase):
    """Длинная запись (>60s) → +5%"""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_long_recording_boosts_confidence(self):
        result = self.cal.calibrate(0.70, duration_sec=90.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.75, places=4)

    def test_exactly_60s_is_not_long(self):
        # Граница: 60.0s НЕ считается длинным
        result = self.cal.calibrate(0.70, duration_sec=60.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.70, places=4)


class TestCalibratorLanguage(unittest.TestCase):
    """Нецелевой язык → -5%"""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_non_primary_language_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="en", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.75, places=4)

    def test_russian_no_language_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.80, places=4)

    def test_spanish_no_language_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="es", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.80, places=4)

    def test_full_name_russian_no_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="russian", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.80, places=4)


class TestCalibratorModel(unittest.TestCase):
    """balanced-модель → -3%; max-модель → без изменений"""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_balanced_model_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="ru", model="mlx-whisper-balanced")
        self.assertAlmostEqual(result, 0.77, places=4)

    def test_max_model_no_penalty(self):
        result = self.cal.calibrate(0.80, duration_sec=10.0, language="ru", model="mlx-whisper-large-v3-mlx")
        self.assertAlmostEqual(result, 0.80, places=4)


class TestCalibratorCombined(unittest.TestCase):
    """Несколько поправок суммируются, результат зажат в [0, 1]."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_multiple_penalties_stack(self):
        # short (-0.10) + non-primary (-0.05) + balanced (-0.03) = -0.18
        result = self.cal.calibrate(0.70, duration_sec=1.0, language="en", model="balanced-model")
        self.assertAlmostEqual(result, 0.52, places=4)

    def test_result_clamped_above_zero(self):
        # raw=0.05, short(-0.10) → would be -0.05 → clamp to 0.0
        result = self.cal.calibrate(0.05, duration_sec=0.3, language="en", model="balanced-model")
        self.assertGreaterEqual(result, 0.0)

    def test_result_clamped_below_one(self):
        # long(+0.05) starting from 0.98 → 1.03 → clamp to 1.0
        result = self.cal.calibrate(0.98, duration_sec=120.0, language="ru", model="mlx-max")
        self.assertLessEqual(result, 1.0)

    def test_adjustments_list_populated(self):
        score = self.cal.calibrate_detailed(0.70, duration_sec=1.0, language="en", model="balanced")
        self.assertGreater(len(score.adjustments), 0)


class TestCalibratedScoreDataclass(unittest.TestCase):
    """CalibratedScore корректно хранит raw/calibrated/adjustments."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_raw_preserved(self):
        score = self.cal.calibrate_detailed(0.75, duration_sec=5.0, language="ru", model="max-model")
        self.assertAlmostEqual(score.raw, 0.75, places=4)

    def test_calibrated_field(self):
        score = self.cal.calibrate_detailed(0.75, duration_sec=5.0, language="ru", model="max-model")
        self.assertIsInstance(score.calibrated, float)

    def test_no_adjustments_when_ideal(self):
        # 5s, russian, max → no adjustments
        score = self.cal.calibrate_detailed(0.80, duration_sec=5.0, language="ru", model="mlx-max")
        self.assertEqual(score.adjustments, [])


class TestCalibratorStats(unittest.TestCase):
    """get_calibration_stats() корректно считает вызовы и типы поправок."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_total_calibrations_incremented(self):
        self.cal.calibrate(0.8, 5.0, "ru", "max")
        self.cal.calibrate(0.8, 5.0, "ru", "max")
        stats = self.cal.get_calibration_stats()
        self.assertEqual(stats["total_calibrations"], 2)

    def test_adjustment_counts_tracked(self):
        self.cal.calibrate(0.8, 1.0, "ru", "max")  # short_recording penalty
        stats = self.cal.get_calibration_stats()
        self.assertIn("short_recording", stats["adjustment_counts"])
        self.assertEqual(stats["adjustment_counts"]["short_recording"], 1)

    def test_reset_stats(self):
        self.cal.calibrate(0.8, 1.0, "ru", "max")
        self.cal.reset_stats()
        stats = self.cal.get_calibration_stats()
        self.assertEqual(stats["total_calibrations"], 0)
        self.assertEqual(stats["adjustment_counts"], {})

    def test_stats_keys_present(self):
        stats = self.cal.get_calibration_stats()
        self.assertIn("total_calibrations", stats)
        self.assertIn("adjustment_counts", stats)


class TestCalibratorEdgeCases(unittest.TestCase):
    """Граничные случаи: нулевой confidence, пустой язык, пустая модель."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_zero_confidence_no_crash(self):
        result = self.cal.calibrate(0.0, duration_sec=5.0, language="ru", model="max")
        self.assertGreaterEqual(result, 0.0)

    def test_empty_language_no_crash(self):
        # Пустая строка языка → нет language-поправки
        result = self.cal.calibrate(0.8, duration_sec=5.0, language="", model="max")
        self.assertAlmostEqual(result, 0.8, places=4)

    def test_empty_model_no_crash(self):
        result = self.cal.calibrate(0.8, duration_sec=5.0, language="ru", model="")
        self.assertAlmostEqual(result, 0.8, places=4)

    def test_thread_safety(self):
        import threading
        results = []
        def worker():
            for _ in range(50):
                results.append(self.cal.calibrate(0.75, 1.0, "en", "balanced"))
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = self.cal.get_calibration_stats()
        self.assertEqual(stats["total_calibrations"], 200)


if __name__ == "__main__":
    unittest.main()
