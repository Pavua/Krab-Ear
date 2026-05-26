"""
test_confidence_calibrator.py — Unit-тесты для ConfidenceCalibrator.
"""

from core.confidence_calibrator import (
    ConfidenceCalibrator,
)
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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
    """Длинная запись (>60s) → -5% (штраф за деградацию/галлюцинации)"""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_long_recording_lowers_confidence(self):
        # W1010 F2: длинные записи деградируют, не буст а штраф
        result = self.cal.calibrate(0.70, duration_sec=90.0, language="ru", model="mlx-whisper-max")
        self.assertAlmostEqual(result, 0.65, places=4)

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
        # long(-0.05) starting from 1.0 → 0.95 → no clamp needed; but raw=5.0 → still clamped
        result = self.cal.calibrate(5.0, duration_sec=120.0, language="ru", model="mlx-max")
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


class TestCalibratorOutOfRangeRaw(unittest.TestCase):
    """raw_confidence вне [0, 1] зажимается корректно."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_raw_above_1_clamped_to_1(self):
        # raw=1.5, long(-0.05) → 1.45 → still clamp to 1.0
        result = self.cal.calibrate(1.5, duration_sec=90.0, language="ru",
                                    model="mlx-max")
        self.assertLessEqual(result, 1.0)

    def test_raw_above_1_no_penalties_clamp_to_1(self):
        # raw=1.2, нормальные условия → clamp 1.0
        result = self.cal.calibrate(1.2, duration_sec=10.0, language="ru",
                                    model="mlx-max")
        self.assertLessEqual(result, 1.0)

    def test_raw_negative_clamped_to_0(self):
        # raw=-0.5 + short_penalty(-0.10) → -0.60 → clamp 0.0
        result = self.cal.calibrate(-0.5, duration_sec=0.5, language="ru",
                                    model="mlx-max")
        self.assertGreaterEqual(result, 0.0)

    def test_raw_zero_with_short_penalty(self):
        # raw=0.0, short(-0.10) → -0.10 → clamp 0.0
        result = self.cal.calibrate(0.0, duration_sec=1.0, language="ru",
                                    model="mlx-max")
        self.assertEqual(result, 0.0)

    def test_raw_exactly_1_no_boost_stays_1(self):
        result = self.cal.calibrate(1.0, duration_sec=10.0, language="ru",
                                    model="mlx-max")
        self.assertAlmostEqual(result, 1.0, places=4)


class TestCalibratorZeroDuration(unittest.TestCase):
    """Нулевая длительность < 2s → получает short_penalty."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_zero_duration_applies_short_penalty(self):
        result = self.cal.calibrate(0.90, duration_sec=0.0, language="ru",
                                    model="mlx-max")
        self.assertAlmostEqual(result, 0.80, places=4)

    def test_zero_duration_adjustment_label(self):
        score = self.cal.calibrate_detailed(0.80, duration_sec=0.0,
                                            language="ru", model="mlx-max")
        self.assertTrue(any("short_recording" in a for a in score.adjustments))


class TestCalibratorMonotonicity(unittest.TestCase):
    """Монотонность: выше raw → выше или равное calibrated."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_monotonic_same_conditions(self):
        values = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        calibrated = [
            self.cal.calibrate(v, duration_sec=10.0, language="ru",
                               model="mlx-max")
            for v in values
        ]
        for i in range(len(calibrated) - 1):
            self.assertLessEqual(
                calibrated[i], calibrated[i + 1],
                f"Not monotonic: calibrated[{i}]={calibrated[i]} > "
                f"calibrated[{i+1}]={calibrated[i+1]}",
            )

    def test_monotonic_with_short_penalty(self):
        low = self.cal.calibrate(0.3, duration_sec=1.0, language="ru",
                                 model="mlx-max")
        high = self.cal.calibrate(0.9, duration_sec=1.0, language="ru",
                                  model="mlx-max")
        self.assertLessEqual(low, high)

    def test_monotonic_with_combined_penalties(self):
        low = self.cal.calibrate(0.4, duration_sec=1.0, language="en",
                                 model="balanced")
        high = self.cal.calibrate(0.9, duration_sec=1.0, language="en",
                                  model="balanced")
        self.assertLessEqual(low, high)


class TestCalibratorDetailedRawPreserved(unittest.TestCase):
    """calibrate_detailed сохраняет raw даже если он вне [0, 1]."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_raw_above_1_preserved_in_score(self):
        score = self.cal.calibrate_detailed(1.5, duration_sec=10.0,
                                            language="ru", model="mlx-max")
        self.assertAlmostEqual(score.raw, 1.5, places=4)

    def test_raw_negative_preserved_in_score(self):
        score = self.cal.calibrate_detailed(-0.2, duration_sec=10.0,
                                            language="ru", model="mlx-max")
        self.assertAlmostEqual(score.raw, -0.2, places=4)

    def test_calibrated_is_rounded_to_4_places(self):
        score = self.cal.calibrate_detailed(0.7777777, duration_sec=10.0,
                                            language="ru", model="mlx-max")
        # calibrated should be rounded
        self.assertEqual(score.calibrated, round(score.calibrated, 4))


class TestCalibratorWave116Required(unittest.TestCase):
    """Wave 116 — required named tests for ConfidenceCalibrator."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_min_max_clamping(self):
        """Результат всегда зажат в [0.0, 1.0] при любых входных данных."""
        # well below zero after penalties
        low = self.cal.calibrate(-5.0, duration_sec=0.5, language="en", model="balanced")
        self.assertGreaterEqual(low, 0.0)
        # large raw clamped to 1.0 even after long penalty
        high = self.cal.calibrate(5.0, duration_sec=120.0, language="ru", model="max")
        self.assertLessEqual(high, 1.0)

    def test_negative_value_clamped_to_zero(self):
        """raw_confidence отрицательный → calibrated == 0.0."""
        result = self.cal.calibrate(-0.3, duration_sec=5.0, language="ru", model="max")
        self.assertEqual(result, 0.0)

    def test_value_above_one_clamped_to_one(self):
        """raw_confidence > 1.0 → calibrated == 1.0 (long penalty still within clamp)."""
        result = self.cal.calibrate(1.2, duration_sec=10.0, language="ru", model="max")
        self.assertEqual(result, 1.0)

    def test_linear_mapping(self):
        """При нейтральных условиях (нет поправок) calibrated == raw."""
        # 10s duration, primary language, non-balanced model → нет ни одной поправки
        for raw in [0.0, 0.25, 0.5, 0.75, 1.0]:
            result = self.cal.calibrate(raw, duration_sec=10.0, language="ru", model="max")
            self.assertAlmostEqual(result, raw, places=4,
                                   msg=f"linear mapping failed for raw={raw}")

    def test_unicode_metadata_preserved(self):
        """Языковые строки с unicode символами не вызывают ошибок и не применяют penalty."""
        # "russian" входит в PRIMARY_LANGUAGES → нет penalty
        result = self.cal.calibrate(0.8, duration_sec=10.0, language="русский", model="max")
        # "русский" не в PRIMARY_LANGUAGES → применяется penalty -0.05
        self.assertAlmostEqual(result, 0.75, places=4)
        # Убеждаемся, что нет исключений при unicode language
        score = self.cal.calibrate_detailed(0.8, duration_sec=10.0, language="中文", model="max")
        self.assertIsInstance(score.calibrated, float)
        self.assertGreaterEqual(score.calibrated, 0.0)

    def test_concurrent_calibrate(self):
        """Параллельные вызовы calibrate не вызывают data race и возвращают корректные значения."""
        import threading
        results = []
        errors = []

        def worker():
            try:
                for _ in range(100):
                    val = self.cal.calibrate(0.8, duration_sec=10.0, language="ru", model="max")
                    results.append(val)
                    assert 0.0 <= val <= 1.0, f"Out of range: {val}"
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 500)
        stats = self.cal.get_calibration_stats()
        self.assertEqual(stats["total_calibrations"], 500)


class TestCalibratorW1025NaNGuard(unittest.TestCase):
    """W1010 F1: NaN/Inf/None raw_confidence → 0.0, not 1.0 or TypeError."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_nan_raw_returns_zero_not_one(self):
        """F1: float('nan') silently became 1.0 via min(1.0, nan); now → 0.0."""
        import math
        result = self.cal.calibrate(float("nan"), duration_sec=10.0, language="ru", model="max")
        self.assertEqual(result, 0.0)
        self.assertFalse(math.isnan(result))

    def test_inf_raw_returns_zero(self):
        """F1: +Inf input → 0.0, not exception."""
        result = self.cal.calibrate(float("inf"), duration_sec=10.0, language="ru", model="max")
        self.assertEqual(result, 0.0)

    def test_neg_inf_raw_returns_zero(self):
        """-Inf input → 0.0."""
        result = self.cal.calibrate(float("-inf"), duration_sec=10.0, language="ru", model="max")
        self.assertEqual(result, 0.0)

    def test_none_raw_returns_zero(self):
        """F1: None input → 0.0, no TypeError."""
        result = self.cal.calibrate(None, duration_sec=10.0, language="ru", model="max")
        self.assertEqual(result, 0.0)

    def test_nan_detailed_returns_invalid_adjustment(self):
        """calibrate_detailed reports invalid_raw adjustment for NaN input."""
        score = self.cal.calibrate_detailed(float("nan"), duration_sec=10.0, language="ru", model="max")
        self.assertEqual(score.calibrated, 0.0)
        self.assertTrue(any("invalid_raw" in a for a in score.adjustments))

    def test_none_detailed_returns_invalid_adjustment(self):
        """calibrate_detailed reports invalid_raw adjustment for None input."""
        score = self.cal.calibrate_detailed(None, duration_sec=10.0, language="ru", model="max")
        self.assertEqual(score.calibrated, 0.0)
        self.assertTrue(any("invalid_raw" in a for a in score.adjustments))


class TestCalibratorW1025LongPenalty(unittest.TestCase):
    """W1010 F2: long recordings should LOWER confidence (hallucination), not boost it."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_long_recording_lowers_confidence(self):
        """F2: >60s recording applies -5% penalty (was incorrectly +5%)."""
        result = self.cal.calibrate(0.80, duration_sec=90.0, language="ru", model="mlx-max")
        self.assertAlmostEqual(result, 0.75, places=4)  # 0.80 - 0.05

    def test_long_recording_adjustment_label(self):
        """F2: adjustment label says 'long_recording' and shows negative value."""
        score = self.cal.calibrate_detailed(0.80, duration_sec=120.0, language="ru", model="mlx-max")
        long_adj = [a for a in score.adjustments if "long_recording" in a]
        self.assertTrue(long_adj, "Expected long_recording adjustment")
        # The label should contain '-5%' not '+5%'
        self.assertIn("-5%", long_adj[0])

    def test_long_recording_strictly_lower_than_normal(self):
        """Long recording calibrated < same recording at normal duration (10s)."""
        normal = self.cal.calibrate(0.80, duration_sec=10.0, language="ru", model="mlx-max")
        long_ = self.cal.calibrate(0.80, duration_sec=90.0, language="ru", model="mlx-max")
        self.assertLess(long_, normal)

    def test_long_penalty_combined_with_language(self):
        """long(-0.05) + non_primary(-0.05) = -0.10 total."""
        result = self.cal.calibrate(0.80, duration_sec=90.0, language="en", model="mlx-max")
        self.assertAlmostEqual(result, 0.70, places=4)  # 0.80 - 0.05 - 0.05


if __name__ == "__main__":
    unittest.main()
