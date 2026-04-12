"""Тесты для CostEstimator — оценка вычислительных затрат Krab Ear."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.cost_estimator import CostEstimate, CostEstimator


class TestCostEstimateDataclass(unittest.TestCase):
    """Проверка полей и типов CostEstimate."""

    def test_fields_exist(self):
        est = CostEstimate(
            compute_time_sec=1.5,
            memory_mb=900.0,
            disk_mb=0.05,
            features_cost={"stt": 1.5, "diarization": 0.0, "llm": 0.0, "translation": 0.0},
            total_relative_cost=0.01,
        )
        self.assertEqual(est.compute_time_sec, 1.5)
        self.assertEqual(est.memory_mb, 900.0)
        self.assertAlmostEqual(est.disk_mb, 0.05)
        self.assertEqual(est.total_relative_cost, 0.01)
        self.assertIn("stt", est.features_cost)


class TestEstimateCostBasic(unittest.TestCase):
    """Базовые тесты estimate_cost."""

    def setUp(self):
        self.estimator = CostEstimator()

    def test_zero_duration_returns_zero_compute(self):
        est = self.estimator.estimate_cost(duration_sec=0.0, quality="balanced")
        self.assertEqual(est.compute_time_sec, 0.0)
        self.assertEqual(est.disk_mb, 0.0)
        self.assertEqual(est.total_relative_cost, 0.0)

    def test_balanced_stt_rate(self):
        """STT balanced = 0.3 s/s; 10 s audio → 3 s compute."""
        est = self.estimator.estimate_cost(duration_sec=10.0, quality="balanced")
        self.assertAlmostEqual(est.features_cost["stt"], 3.0, places=4)
        self.assertAlmostEqual(est.compute_time_sec, 3.0, places=4)

    def test_max_stt_rate(self):
        """STT max = 0.5 s/s; 10 s audio → 5 s compute."""
        est = self.estimator.estimate_cost(duration_sec=10.0, quality="max")
        self.assertAlmostEqual(est.features_cost["stt"], 5.0, places=4)

    def test_max_quality_higher_than_balanced(self):
        est_bal = self.estimator.estimate_cost(duration_sec=60.0, quality="balanced")
        est_max = self.estimator.estimate_cost(duration_sec=60.0, quality="max")
        self.assertGreater(est_max.compute_time_sec, est_bal.compute_time_sec)

    def test_unknown_quality_falls_back_to_balanced(self):
        est = self.estimator.estimate_cost(duration_sec=10.0, quality="ultramax_unknown")
        est_bal = self.estimator.estimate_cost(duration_sec=10.0, quality="balanced")
        self.assertEqual(est.compute_time_sec, est_bal.compute_time_sec)

    def test_negative_duration_clamped_to_zero(self):
        est = self.estimator.estimate_cost(duration_sec=-5.0)
        self.assertEqual(est.compute_time_sec, 0.0)


class TestEstimateCostFeatures(unittest.TestCase):
    """Тесты влияния дополнительных функций на стоимость."""

    def setUp(self):
        self.estimator = CostEstimator()

    def test_diarization_doubles_stt_cost(self):
        """Diarization добавляет ещё STT×1 к compute."""
        est_no_diar = self.estimator.estimate_cost(10.0, "balanced", {})
        est_diar = self.estimator.estimate_cost(10.0, "balanced", {"diarization": True})
        # balanced STT = 0.3×10 = 3. diarization добавляет ещё 3 → итого 6.
        self.assertAlmostEqual(est_diar.features_cost["diarization"], est_no_diar.features_cost["stt"], places=4)
        self.assertGreater(est_diar.compute_time_sec, est_no_diar.compute_time_sec)

    def test_llm_adds_flat_cost(self):
        est_no_llm = self.estimator.estimate_cost(10.0, "balanced", {})
        est_llm = self.estimator.estimate_cost(10.0, "balanced", {"llm": True})
        self.assertAlmostEqual(est_llm.features_cost["llm"], 0.5, places=4)
        self.assertAlmostEqual(est_llm.compute_time_sec - est_no_llm.compute_time_sec, 0.5, places=4)

    def test_translation_adds_flat_cost(self):
        est_no_tr = self.estimator.estimate_cost(10.0, "balanced", {})
        est_tr = self.estimator.estimate_cost(10.0, "balanced", {"translation": True})
        self.assertAlmostEqual(est_tr.features_cost["translation"], 0.2, places=4)
        self.assertAlmostEqual(est_tr.compute_time_sec - est_no_tr.compute_time_sec, 0.2, places=4)

    def test_all_features_enabled(self):
        est = self.estimator.estimate_cost(
            10.0, "max",
            {"diarization": True, "llm": True, "translation": True},
        )
        # stt = 0.5×10=5, diarization = 5, llm=0.5, translation=0.2 → 10.7
        self.assertAlmostEqual(est.compute_time_sec, 10.7, places=4)
        self.assertIn("diarization", est.features_cost)
        self.assertIn("llm", est.features_cost)
        self.assertIn("translation", est.features_cost)

    def test_memory_increases_with_features(self):
        est_bare = self.estimator.estimate_cost(10.0, "balanced", {})
        est_full = self.estimator.estimate_cost(
            10.0, "balanced",
            {"diarization": True, "llm": True, "translation": True},
        )
        self.assertGreater(est_full.memory_mb, est_bare.memory_mb)

    def test_disk_proportional_to_duration(self):
        est_short = self.estimator.estimate_cost(60.0)
        est_long = self.estimator.estimate_cost(300.0)
        # 5× longer audio → 5× more disk
        self.assertAlmostEqual(est_long.disk_mb / est_short.disk_mb, 5.0, places=4)


class TestEstimateCostRelative(unittest.TestCase):
    """Тесты нормализованной относительной стоимости."""

    def setUp(self):
        self.estimator = CostEstimator()

    def test_relative_cost_between_0_and_1(self):
        for duration in [0, 10, 60, 600, 3600]:
            est = self.estimator.estimate_cost(
                duration,
                "max",
                {"diarization": True, "llm": True, "translation": True},
            )
            self.assertGreaterEqual(est.total_relative_cost, 0.0)
            self.assertLessEqual(est.total_relative_cost, 1.0)

    def test_more_features_higher_relative_cost(self):
        est_bare = self.estimator.estimate_cost(60.0, "balanced", {})
        est_full = self.estimator.estimate_cost(
            60.0, "max",
            {"diarization": True, "llm": True, "translation": True},
        )
        self.assertGreater(est_full.total_relative_cost, est_bare.total_relative_cost)

    def test_max_config_60min_close_to_1(self):
        """Максимальная конфигурация на 60 минут → ~1.0."""
        est = self.estimator.estimate_cost(
            3600.0, "max",
            {"diarization": True, "llm": True, "translation": True},
        )
        self.assertAlmostEqual(est.total_relative_cost, 1.0, places=1)


class TestEstimateBatchCost(unittest.TestCase):
    """Тесты estimate_batch_cost."""

    def setUp(self):
        self.estimator = CostEstimator()

    def test_empty_batch(self):
        result = self.estimator.estimate_batch_cost([])
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["total_compute_time_sec"], 0.0)
        self.assertEqual(result["total_memory_mb"], 0.0)
        self.assertEqual(result["total_disk_mb"], 0.0)
        self.assertEqual(result["estimates"], [])

    def test_batch_totals_are_sums(self):
        files = [
            {"duration_sec": 10.0, "quality": "balanced"},
            {"duration_sec": 20.0, "quality": "balanced"},
        ]
        result = self.estimator.estimate_batch_cost(files)
        est1 = self.estimator.estimate_cost(10.0, "balanced")
        est2 = self.estimator.estimate_cost(20.0, "balanced")
        expected_compute = est1.compute_time_sec + est2.compute_time_sec
        self.assertAlmostEqual(result["total_compute_time_sec"], expected_compute, places=4)
        self.assertEqual(result["file_count"], 2)

    def test_batch_memory_is_peak(self):
        """Peak memory = max across files (not sum)."""
        files = [
            {"duration_sec": 10.0, "quality": "balanced"},
            {"duration_sec": 10.0, "quality": "max"},
        ]
        result = self.estimator.estimate_batch_cost(files)
        est_max = self.estimator.estimate_cost(10.0, "max")
        self.assertAlmostEqual(result["total_memory_mb"], est_max.memory_mb, places=2)

    def test_batch_estimates_list_has_required_keys(self):
        files = [{"duration_sec": 5.0}]
        result = self.estimator.estimate_batch_cost(files)
        entry = result["estimates"][0]
        for key in ("compute_time_sec", "memory_mb", "disk_mb", "features_cost", "total_relative_cost"):
            self.assertIn(key, entry)

    def test_batch_disk_is_sum(self):
        files = [
            {"duration_sec": 60.0},
            {"duration_sec": 60.0},
        ]
        result = self.estimator.estimate_batch_cost(files)
        single = self.estimator.estimate_cost(60.0)
        self.assertAlmostEqual(result["total_disk_mb"], single.disk_mb * 2, places=6)

    def test_batch_missing_fields_use_defaults(self):
        """Отсутствующие поля не вызывают ошибок."""
        files = [{}]  # нет duration_sec / quality / features
        result = self.estimator.estimate_batch_cost(files)
        self.assertEqual(result["file_count"], 1)
        self.assertGreaterEqual(result["total_compute_time_sec"], 0.0)


class TestGetDailyCostSummary(unittest.TestCase):
    """Тесты get_daily_cost_summary."""

    def setUp(self):
        self.estimator = CostEstimator()

    def _make_tracker(self, recordings=3, duration=90.0):
        tracker = MagicMock()
        tracker.get_usage_stats.return_value = {
            "today": {
                "recordings": recordings,
                "total_duration_sec": duration,
                "total_words": 500,
            }
        }
        return tracker

    def test_summary_has_required_keys(self):
        tracker = self._make_tracker()
        summary = self.estimator.get_daily_cost_summary(tracker)
        for key in (
            "date", "recordings_today", "total_duration_sec",
            "estimated_compute_sec", "estimated_memory_mb",
            "estimated_disk_mb", "relative_cost",
        ):
            self.assertIn(key, summary, f"Missing key: {key}")

    def test_summary_recordings_count(self):
        tracker = self._make_tracker(recordings=7, duration=300.0)
        summary = self.estimator.get_daily_cost_summary(tracker)
        self.assertEqual(summary["recordings_today"], 7)
        self.assertAlmostEqual(summary["total_duration_sec"], 300.0, places=2)

    def test_summary_compute_matches_estimate(self):
        tracker = self._make_tracker(recordings=1, duration=60.0)
        summary = self.estimator.get_daily_cost_summary(tracker)
        expected = self.estimator.estimate_cost(60.0, "balanced")
        self.assertAlmostEqual(summary["estimated_compute_sec"], expected.compute_time_sec, places=4)
        self.assertAlmostEqual(summary["estimated_memory_mb"], expected.memory_mb, places=2)

    def test_summary_zero_recordings(self):
        tracker = self._make_tracker(recordings=0, duration=0.0)
        summary = self.estimator.get_daily_cost_summary(tracker)
        self.assertEqual(summary["recordings_today"], 0)
        self.assertEqual(summary["estimated_compute_sec"], 0.0)
        self.assertEqual(summary["relative_cost"], 0.0)

    def test_summary_tracker_error_returns_gracefully(self):
        """Ошибка usage_tracker не вызывает исключение."""
        bad_tracker = MagicMock()
        bad_tracker.get_usage_stats.side_effect = RuntimeError("db gone")
        summary = self.estimator.get_daily_cost_summary(bad_tracker)
        self.assertIn("date", summary)
        self.assertEqual(summary["recordings_today"], 0)
        self.assertEqual(summary["estimated_compute_sec"], 0.0)

    def test_summary_relative_cost_between_0_and_1(self):
        tracker = self._make_tracker(recordings=100, duration=7200.0)
        summary = self.estimator.get_daily_cost_summary(tracker)
        self.assertGreaterEqual(summary["relative_cost"], 0.0)
        self.assertLessEqual(summary["relative_cost"], 1.0)


if __name__ == "__main__":
    unittest.main()
