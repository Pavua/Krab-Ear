"""Тесты для модуля анализа трендов качества записей (QualityTrendAnalyzer).

Покрывает:
- Запись quality scores (confidence values) и сохранение
- Расчёт тренда: improving / declining / stable
- Обработка пустой истории
- Дневная агрегация confidence
- Персистентность и сериализация
- Гистограмма распределения оценок качества
- Валидация временных меток (ISO, epoch, datetime)
- Граничные случаи (None confidence, missing ts)
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.quality_trends import QualityTrendAnalyzer


class TestQualityTrendAnalyzerRecordQuality(unittest.TestCase):
    """Тесты записи и сохранения quality scores."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_record_quality_with_dict_items(self) -> None:
        """Запись confidence в виде dict-элементов истории."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95, "text": "hello"},
            {"ts": now.isoformat(), "confidence": 0.87, "text": "world"},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 2)
        self.assertAlmostEqual(daily["avg"], (0.95 + 0.87) / 2, places=2)
        self.assertEqual(daily["min"], 0.87)
        self.assertEqual(daily["max"], 0.95)

    def test_record_quality_with_object_items(self) -> None:
        """Запись confidence из объектов с атрибутами."""
        now = datetime.now(timezone.utc)

        item1 = MagicMock()
        item1.ts = now
        item1.confidence = 0.92

        item2 = MagicMock()
        item2.ts = now
        item2.confidence = 0.88

        report = self.analyzer.analyze_trends([item1, item2], days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 2)
        self.assertAlmostEqual(daily["avg"], (0.92 + 0.88) / 2, places=2)

    def test_ignore_items_with_none_confidence(self) -> None:
        """Игнорируются элементы с None confidence."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95},
            {"ts": now.isoformat(), "confidence": None},
            {"ts": now.isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 2)

    def test_ignore_items_with_missing_confidence(self) -> None:
        """Игнорируются элементы без поля confidence."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95},
            {"ts": now.isoformat()},
            {"ts": now.isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 2)

    def test_coerce_confidence_to_float(self) -> None:
        """Приведение confidence к float (из строки и целых чисел)."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": "0.95"},
            {"ts": now.isoformat(), "confidence": 1},
            {"ts": now.isoformat(), "confidence": 0},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 3)
        self.assertEqual(daily["max"], 1.0)
        self.assertEqual(daily["min"], 0.0)


class TestQualityTrendAnalyzerTrendCalculation(unittest.TestCase):
    """Тесты расчёта тренда (improving/declining/stable)."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_trend_improving(self) -> None:
        """Растущий тренд качества (improving)."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            # Confidence растёт: 0.70 -> 0.74 -> 0.78 -> 0.82 -> 0.86
            items.append({"ts": day.isoformat(), "confidence": 0.70 + i * 0.04})

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "improving")
        self.assertGreater(report.trend_slope, 0.001)

    def test_trend_declining(self) -> None:
        """Падающий тренд качества (declining)."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            # Confidence падает: 0.90 -> 0.86 -> 0.82 -> 0.78 -> 0.74
            items.append({"ts": day.isoformat(), "confidence": 0.90 - i * 0.04})

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "declining")
        self.assertLess(report.trend_slope, -0.001)

    def test_trend_stable(self) -> None:
        """Стабильный тренд качества (stable)."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            # Confidence стабилен: ~0.85 каждый день
            items.append({"ts": day.isoformat(), "confidence": 0.85})

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "stable")
        self.assertAlmostEqual(report.trend_slope, 0.0, places=3)

    def test_trend_near_threshold_improving(self) -> None:
        """Тренд близко к порогу улучшения, но выше."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            # Очень лёгкий рост, но >0.001: slope ≈ 0.005
            items.append({"ts": day.isoformat(), "confidence": 0.85 + i * 0.002})

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "improving")

    def test_trend_near_threshold_declining(self) -> None:
        """Тренд близко к порогу падения, но ниже."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            # Очень лёгкое падение, но <-0.001: slope ≈ -0.005
            items.append({"ts": day.isoformat(), "confidence": 0.85 - i * 0.002})

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.overall_trend, "declining")


class TestQualityTrendAnalyzerEmptyHistory(unittest.TestCase):
    """Тесты обработки пустой истории и граничных случаев."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_empty_items_list(self) -> None:
        """Пустой список элементов."""
        report = self.analyzer.analyze_trends([], days=30)

        self.assertEqual(report.daily_confidence, [])
        self.assertEqual(report.overall_trend, "stable")
        self.assertEqual(report.trend_slope, 0.0)
        self.assertEqual(report.best_day, {})
        self.assertEqual(report.worst_day, {})

    def test_all_items_outside_window(self) -> None:
        """Все элементы за пределами окна анализа (слишком старые)."""
        old = datetime.now(timezone.utc) - timedelta(days=60)
        items = [
            {"ts": old.isoformat(), "confidence": 0.95},
            {"ts": old.isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence, [])
        self.assertEqual(report.overall_trend, "stable")

    def test_all_items_with_none_confidence(self) -> None:
        """Все элементы имеют None confidence."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": None},
            {"ts": now.isoformat(), "confidence": None},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.daily_confidence, [])
        self.assertEqual(report.overall_trend, "stable")

    def test_single_item(self) -> None:
        """Единственный элемент в истории."""
        now = datetime.now(timezone.utc)
        items = [{"ts": now.isoformat(), "confidence": 0.92}]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        self.assertEqual(report.overall_trend, "stable")
        self.assertEqual(report.trend_slope, 0.0)


class TestQualityTrendAnalyzerDailyAggregation(unittest.TestCase):
    """Тесты дневной агрегации confidence."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_multiple_items_same_day(self) -> None:
        """Несколько элементов в один день агрегируются."""
        # Use a fixed date to ensure they're on the same day
        base_date = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        items = [
            {"ts": base_date.isoformat(), "confidence": 0.90},
            {"ts": (base_date + timedelta(hours=1)).isoformat(), "confidence": 0.85},
            {"ts": (base_date + timedelta(hours=2)).isoformat(), "confidence": 0.95},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)
        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 3)
        self.assertAlmostEqual(daily["avg"], (0.90 + 0.85 + 0.95) / 3, places=2)
        self.assertEqual(daily["min"], 0.85)
        self.assertEqual(daily["max"], 0.95)

    def test_items_across_multiple_days(self) -> None:
        """Элементы распределены по нескольким дням."""
        base = datetime.now(timezone.utc)
        items = [
            {"ts": base.isoformat(), "confidence": 0.90},
            {"ts": (base + timedelta(days=1)).isoformat(), "confidence": 0.85},
            {"ts": (base + timedelta(days=2)).isoformat(), "confidence": 0.95},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 3)
        self.assertEqual(report.daily_confidence[0]["date"], base.date().isoformat())
        self.assertEqual(report.daily_confidence[1]["date"],
                         (base + timedelta(days=1)).date().isoformat())

    def test_daily_sorted_by_date(self) -> None:
        """Дневные агрегаты отсортированы по датам."""
        base = datetime.now(timezone.utc)
        items = [
            {"ts": (base + timedelta(days=2)).isoformat(), "confidence": 0.95},
            {"ts": base.isoformat(), "confidence": 0.90},
            {"ts": (base + timedelta(days=1)).isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        dates = [d["date"] for d in report.daily_confidence]
        self.assertEqual(dates, sorted(dates))

    def test_best_worst_days(self) -> None:
        """Правильно находятся лучший и худший день."""
        base = datetime.now(timezone.utc)
        items = [
            {"ts": base.isoformat(), "confidence": 0.90},
            {"ts": (base + timedelta(days=1)).isoformat(), "confidence": 0.98},  # best
            {"ts": (base + timedelta(days=2)).isoformat(), "confidence": 0.70},  # worst
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(report.best_day["avg"], 0.98)
        self.assertEqual(report.worst_day["avg"], 0.70)


class TestQualityTrendAnalyzerTimestampHandling(unittest.TestCase):
    """Тесты валидации и парсинга временных меток."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_iso_string_timestamp(self) -> None:
        """Обработка ISO-формата строк (YYYY-MM-DDTHH:MM:SS)."""
        items = [
            {"ts": "2026-04-15T10:30:00Z", "confidence": 0.95},
            {"ts": "2026-04-15T14:30:00+00:00", "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)

    def test_epoch_timestamp(self) -> None:
        """Обработка epoch (unix timestamp)."""
        now_ts = datetime.now(timezone.utc).timestamp()
        items = [
            {"ts": now_ts, "confidence": 0.95},
            {"ts": int(now_ts), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)

    def test_datetime_object_timestamp(self) -> None:
        """Обработка datetime объектов."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now, "confidence": 0.95},
            {"ts": now.replace(tzinfo=None), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        self.assertEqual(len(report.daily_confidence), 1)

    def test_invalid_timestamp_ignored(self) -> None:
        """Элементы с невалидными временными метками игнорируются."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95},
            {"ts": "invalid-date", "confidence": 0.85},
            {"ts": None, "confidence": 0.90},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        daily = report.daily_confidence[0]
        self.assertEqual(daily["count"], 1)


class TestQualityTrendAnalyzerDistribution(unittest.TestCase):
    """Тесты гистограммы распределения confidence."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_distribution_buckets(self) -> None:
        """Распределение разбивается по предопределённым бакетам."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95},  # 0.9-1.0
            {"ts": now.isoformat(), "confidence": 0.85},  # 0.8-0.9
            {"ts": now.isoformat(), "confidence": 0.75},  # 0.7-0.8
            {"ts": now.isoformat(), "confidence": 0.65},  # 0.6-0.7
            {"ts": now.isoformat(), "confidence": 0.45},  # 0.0-0.6
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.9-1.0"], 1)
        self.assertEqual(dist["0.8-0.9"], 1)
        self.assertEqual(dist["0.7-0.8"], 1)
        self.assertEqual(dist["0.6-0.7"], 1)
        self.assertEqual(dist["0.0-0.6"], 1)

    def test_distribution_boundary_values(self) -> None:
        """Граничные значения попадают в правильные бакеты."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 1.0},  # 0.9-1.0
            {"ts": now.isoformat(), "confidence": 0.9},  # 0.9-1.0
            {"ts": now.isoformat(), "confidence": 0.8},  # 0.8-0.9
            {"ts": now.isoformat(), "confidence": 0.0},  # 0.0-0.6
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.9-1.0"], 2)
        self.assertEqual(dist["0.8-0.9"], 1)
        self.assertEqual(dist["0.0-0.6"], 1)

    def test_empty_distribution(self) -> None:
        """Пустое распределение при отсутствии данных."""
        report = self.analyzer.analyze_trends([], days=30)

        dist = report.confidence_distribution
        self.assertEqual(dist["0.9-1.0"], 0)
        self.assertEqual(dist["0.8-0.9"], 0)
        self.assertEqual(dist["0.7-0.8"], 0)
        self.assertEqual(dist["0.6-0.7"], 0)
        self.assertEqual(dist["0.0-0.6"], 0)


class TestQualityTrendAnalyzerSerialization(unittest.TestCase):
    """Тесты сериализации и персистентности."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_to_dict_serialization(self) -> None:
        """Сериализация TrendReport в plain dict."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.95},
            {"ts": (now + timedelta(days=1)).isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=30)
        result_dict = self.analyzer.to_dict(report)

        self.assertIn("daily_confidence", result_dict)
        self.assertIn("overall_trend", result_dict)
        self.assertIn("trend_slope", result_dict)
        self.assertIn("best_day", result_dict)
        self.assertIn("worst_day", result_dict)
        self.assertIn("confidence_distribution", result_dict)

    def test_to_dict_json_serializable(self) -> None:
        """Результат to_dict() сериализуется в JSON без ошибок."""
        import json

        now = datetime.now(timezone.utc)
        items = [{"ts": now.isoformat(), "confidence": 0.95}]

        report = self.analyzer.analyze_trends(items, days=30)
        result_dict = self.analyzer.to_dict(report)

        # Должно работать без исключений
        json_str = json.dumps(result_dict)
        parsed = json.loads(json_str)

        self.assertEqual(parsed["overall_trend"], "stable")

    def test_confidence_values_rounded(self) -> None:
        """Значения confidence округлены до 4 знаков."""
        now = datetime.now(timezone.utc)
        items = [
            {"ts": now.isoformat(), "confidence": 0.123456789},
            {"ts": now.isoformat(), "confidence": 0.987654321},
        ]

        report = self.analyzer.analyze_trends(items, days=30)

        daily = report.daily_confidence[0]
        # avg = (0.123456789 + 0.987654321) / 2 = 0.5555555555
        # Должно быть округлено до 0.5556
        self.assertLessEqual(len(str(daily["avg"]).split(".")[-1]), 4)

    def test_trend_slope_rounded(self) -> None:
        """Тренд slope округлён до 6 знаков."""
        base = datetime.now(timezone.utc) - timedelta(days=10)
        items = []
        for i in range(5):
            day = base + timedelta(days=i)
            items.append({"ts": day.isoformat(), "confidence": 0.85 + i * 0.001})

        report = self.analyzer.analyze_trends(items, days=30)

        slope_str = str(report.trend_slope)
        decimal_places = len(slope_str.split(".")[-1]) if "." in slope_str else 0
        self.assertLessEqual(decimal_places, 6)


class TestQualityTrendAnalyzerWindowSize(unittest.TestCase):
    """Тесты параметра window size (days)."""

    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_30_day_window_default(self) -> None:
        """По умолчанию используется 30-дневное окно."""
        base = datetime.now(timezone.utc) - timedelta(days=35)
        items = [
            {"ts": (base + timedelta(days=5)).isoformat(), "confidence": 0.95},
            {"ts": (base + timedelta(days=35)).isoformat(), "confidence": 0.85},
        ]

        # Первый элемент за пределами 30-дневного окна (35 дней назад),
        # второй внутри (0 дней назад)
        report = self.analyzer.analyze_trends(items, days=30)

        # Должен быть включен только второй элемент
        self.assertEqual(len(report.daily_confidence), 1)

    def test_custom_window_size(self) -> None:
        """Использование кастомного размера окна."""
        base = datetime.now(timezone.utc) - timedelta(days=65)
        items = [
            {"ts": (base + timedelta(days=35)).isoformat(), "confidence": 0.95},
            {"ts": (base + timedelta(days=65)).isoformat(), "confidence": 0.85},
        ]

        # Оба элемента внутри 90-дневного окна
        report = self.analyzer.analyze_trends(items, days=90)

        self.assertEqual(len(report.daily_confidence), 2)

    def test_7_day_window(self) -> None:
        """Узкое 7-дневное окно."""
        base = datetime.now(timezone.utc)
        items = [
            {"ts": (base - timedelta(days=10)).isoformat(), "confidence": 0.95},
            {"ts": base.isoformat(), "confidence": 0.85},
        ]

        report = self.analyzer.analyze_trends(items, days=7)

        # Только второй элемент внутри 7-дневного окна
        self.assertEqual(len(report.daily_confidence), 1)


if __name__ == "__main__":
    unittest.main()
