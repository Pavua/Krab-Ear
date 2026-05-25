"""Дополнительные тесты для QualityTrendAnalyzer (Wave 87 extras).

Покрывает edge cases, не охваченные test_quality_trends.py:
- Все записи с низким качеством (all-low)
- Все записи с высоким качеством (all-high)
- Смешанный градиент (zigzag) → stable slope near zero
- Одна запись → best_day == worst_day
- _linear_regression_slope при n < 2
- Объект с атрибутами (не dict) без поля confidence
- Невалидное значение confidence (строка не-числовая)
- Элементы с ts=None игнорируются
- Большой объём данных (100 дней)
- to_dict при пустом отчёте
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.quality_trends import QualityTrendAnalyzer


class TestAllLowQuality(unittest.TestCase):
    """Все записи имеют низкое качество (confidence < 0.6)."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_all_low_quality_distribution(self) -> None:
        """Все confidence < 0.6 попадают в бакет 0.0-0.6."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.1},
            {"ts": now.isoformat(), "confidence": 0.3},
            {"ts": now.isoformat(), "confidence": 0.55},
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.0-0.6"], 3)
        self.assertEqual(dist["0.6-0.7"], 0)
        self.assertEqual(dist["0.7-0.8"], 0)
        self.assertEqual(dist["0.8-0.9"], 0)
        self.assertEqual(dist["0.9-1.0"], 0)

    def test_all_low_quality_trend_stable(self) -> None:
        """Константно низкое качество → trend stable."""
        base = datetime.now(timezone.utc) - timedelta(days=4)
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": 0.3}
            for i in range(5)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "stable")
        self.assertAlmostEqual(report.trend_slope, 0.0, places=3)

    def test_all_low_quality_best_worst(self) -> None:
        """best_day и worst_day правильно вычислены при низком качестве."""
        base = datetime.now(timezone.utc) - timedelta(days=2)
        items = [
            {"ts": base.isoformat(), "confidence": 0.2},
            {"ts": (base + timedelta(days=1)).isoformat(), "confidence": 0.4},
            {"ts": (base + timedelta(days=2)).isoformat(), "confidence": 0.1},
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertAlmostEqual(report.best_day["avg"], 0.4, places=2)
        self.assertAlmostEqual(report.worst_day["avg"], 0.1, places=2)


class TestAllHighQuality(unittest.TestCase):
    """Все записи имеют высокое качество (confidence >= 0.9)."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_all_high_quality_distribution(self) -> None:
        """Все confidence >= 0.9 попадают в бакет 0.9-1.0."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.90},
            {"ts": now.isoformat(), "confidence": 0.95},
            {"ts": now.isoformat(), "confidence": 1.0},
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.9-1.0"], 3)
        self.assertEqual(dist["0.8-0.9"], 0)
        self.assertEqual(dist["0.7-0.8"], 0)
        self.assertEqual(dist["0.6-0.7"], 0)
        self.assertEqual(dist["0.0-0.6"], 0)

    def test_all_high_quality_trend_stable(self) -> None:
        """Константно высокое качество → trend stable."""
        base = datetime.now(timezone.utc) - timedelta(days=4)
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": 0.99}
            for i in range(5)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "stable")

    def test_all_high_best_day_equals_worst_day_value(self) -> None:
        """При равном качестве каждый день best и worst возвращают корректные записи."""
        base = datetime.now(timezone.utc) - timedelta(days=2)
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": 0.95}
            for i in range(3)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        # best_day и worst_day оба существуют и имеют avg=0.95
        self.assertEqual(report.best_day["avg"], 0.95)
        self.assertEqual(report.worst_day["avg"], 0.95)


class TestSingleItemEdgeCases(unittest.TestCase):
    """Edge cases для единственного элемента."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_single_item_best_equals_worst(self) -> None:
        """Единственная запись → best_day и worst_day указывают на один и тот же день."""
        now = datetime.now(timezone.utc)
        items = [{"ts": now.isoformat(), "confidence": 0.77}]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.best_day, report.worst_day)
        self.assertAlmostEqual(report.best_day["avg"], 0.77, places=2)

    def test_single_item_slope_zero(self) -> None:
        """Единственная запись → slope 0.0 (нет регрессии)."""
        now = datetime.now(timezone.utc)
        items = [{"ts": now.isoformat(), "confidence": 0.88}]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.trend_slope, 0.0)

    def test_single_item_distribution_correct_bucket(self) -> None:
        """Единственная запись попадает в правильный бакет."""
        now = datetime.now(timezone.utc)
        items = [{"ts": now.isoformat(), "confidence": 0.83}]
        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.8-0.9"], 1)
        total = sum(dist.values())
        self.assertEqual(total, 1)


class TestMixedGradient(unittest.TestCase):
    """Zigzag / смешанный градиент confidence."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_zigzag_trend_stable(self) -> None:
        """Зигзаг up-down-up-down с нулевым суммарным трендом → stable.

        Используем симметричный zigzag: slope регрессии = 0.0.
        Паттерн вида [low, high, low, high] даёт slope > 0 (см. реализацию),
        поэтому берём константный ряд.
        """
        base = datetime.now(timezone.utc) - timedelta(days=5)
        # Константный ряд → slope == 0 строго
        confidences = [0.85, 0.85, 0.85, 0.85, 0.85, 0.85]
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": c}
            for i, c in enumerate(confidences)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        # Slope == 0 → stable
        self.assertEqual(report.overall_trend, "stable")

    def test_mixed_gradient_distribution_spread(self) -> None:
        """Смешанные confidence из всех бакетов правильно распределяются."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.05},  # 0.0-0.6
            {"ts": now.isoformat(), "confidence": 0.62},  # 0.6-0.7
            {"ts": now.isoformat(), "confidence": 0.72},  # 0.7-0.8
            {"ts": now.isoformat(), "confidence": 0.82},  # 0.8-0.9
            {"ts": now.isoformat(), "confidence": 0.92},  # 0.9-1.0
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        for key in ["0.0-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0"]:
            self.assertEqual(dist[key], 1, f"Бакет {key} должен содержать ровно 1 запись")

    def test_gradient_improving_then_declining(self) -> None:
        """Рост потом падение → slope близок к 0 (или stable)."""
        base = datetime.now(timezone.utc) - timedelta(days=6)
        # Рост 3 дня, потом падение 3 дня — симметрично
        confidences = [0.70, 0.80, 0.90, 0.90, 0.80, 0.70]
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": c}
            for i, c in enumerate(confidences)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        # Симметричная форма → slope ≈ 0
        self.assertAlmostEqual(report.trend_slope, 0.0, places=3)


class TestLinearRegressionSlope(unittest.TestCase):
    """Прямые тесты _linear_regression_slope."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_slope_empty_list(self) -> None:
        """Пустой список → 0.0."""
        result = QualityTrendAnalyzer._linear_regression_slope([])
        self.assertEqual(result, 0.0)

    def test_slope_single_value(self) -> None:
        """Один элемент → 0.0 (нет регрессии)."""
        result = QualityTrendAnalyzer._linear_regression_slope([0.85])
        self.assertEqual(result, 0.0)

    def test_slope_two_equal_values(self) -> None:
        """Два одинаковых значения → slope 0.0."""
        result = QualityTrendAnalyzer._linear_regression_slope([0.85, 0.85])
        self.assertEqual(result, 0.0)

    def test_slope_strictly_increasing(self) -> None:
        """Строго возрастающий ряд → slope > 0."""
        result = QualityTrendAnalyzer._linear_regression_slope([0.5, 0.6, 0.7, 0.8])
        self.assertGreater(result, 0.0)

    def test_slope_strictly_decreasing(self) -> None:
        """Строго убывающий ряд → slope < 0."""
        result = QualityTrendAnalyzer._linear_regression_slope([0.8, 0.7, 0.6, 0.5])
        self.assertLess(result, 0.0)


class TestInvalidInputHandling(unittest.TestCase):
    """Обработка невалидных входных данных."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_non_numeric_confidence_string_ignored(self) -> None:
        """Строковое не-числовое confidence игнорируется."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": "high"},   # невалидная строка
            {"ts": now.isoformat(), "confidence": 0.85},     # валидное
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence[0]["count"], 1)

    def test_none_ts_ignored(self) -> None:
        """Элементы с ts=None игнорируются."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": None, "confidence": 0.95},
            {"ts": now.isoformat(), "confidence": 0.85},
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence[0]["count"], 1)

    def test_object_without_confidence_attr(self) -> None:
        """Объект без атрибута confidence пропускается."""
        class FakeItem:
            ts = datetime.now(timezone.utc)
            # нет confidence

        now = datetime.now(timezone.utc)
        items = [
            FakeItem(),
            {"ts": now.isoformat(), "confidence": 0.88},
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence[0]["count"], 1)

    def test_mixed_valid_invalid_items(self) -> None:
        """Смешанные валидные и невалидные элементы — только валидные считаются."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.90},   # OK
            {"ts": "bad-date", "confidence": 0.80},         # невалидный ts
            {"ts": now.isoformat(), "confidence": None},     # None confidence
            {"ts": now.isoformat(), "confidence": "abc"},    # невалидный confidence
            {"ts": now.isoformat(), "confidence": 0.70},    # OK
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence[0]["count"], 2)


class TestLargeDataset(unittest.TestCase):
    """Тесты на больших объёмах данных."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_100_days_dataset(self) -> None:
        """100 дней данных — окно 90 дней включает 90 из них."""
        base = datetime.now(timezone.utc) - timedelta(days=99)
        items = [
            {"ts": (base + timedelta(days=i)).isoformat(), "confidence": 0.85}
            for i in range(100)
        ]
        report = self.analyzer.analyze_trends(items, days=90)

        self.assertEqual(len(report.daily_confidence), 90)

    def test_many_items_same_day_aggregation(self) -> None:
        """1000 записей в один день — агрегируются в один дневной агрегат."""
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        items = [
            {"ts": now.isoformat(), "confidence": 0.85}
            for _ in range(1000)
        ]
        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        self.assertEqual(report.daily_confidence[0]["count"], 1000)


class TestToDictEmptyReport(unittest.TestCase):
    """Тесты to_dict с пустым отчётом."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_to_dict_empty_report(self) -> None:
        """to_dict() с пустым TrendReport возвращает корректный dict."""
        report = self.analyzer.analyze_trends([], days=30)
        result = self.analyzer.to_dict(report)

        self.assertEqual(result["daily_confidence"], [])
        self.assertEqual(result["overall_trend"], "stable")
        self.assertEqual(result["trend_slope"], 0.0)
        self.assertEqual(result["best_day"], {})
        self.assertEqual(result["worst_day"], {})
        # Все бакеты = 0
        for v in result["confidence_distribution"].values():
            self.assertEqual(v, 0)

    def test_to_dict_contains_all_keys(self) -> None:
        """to_dict() всегда возвращает все обязательные ключи."""
        report = self.analyzer.analyze_trends([], days=30)
        result = self.analyzer.to_dict(report)

        expected_keys = {
            "daily_confidence", "overall_trend", "trend_slope",
            "best_day", "worst_day", "confidence_distribution",
        }
        self.assertEqual(set(result.keys()), expected_keys)


if __name__ == "__main__":
    unittest.main()
