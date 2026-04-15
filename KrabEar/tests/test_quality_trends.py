"""Тесты QualityTrendAnalyzer — анализ трендов качества Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.quality_trends import QualityTrendAnalyzer, TrendReport

from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_item(days_ago: int, confidence: float):
    """Создаёт fake-элемент истории с заданным confidence и датой."""
    ts = (datetime.now(tz=timezone.utc) - timedelta(days=days_ago)).isoformat()

    class FakeItem:
        pass

    item = FakeItem()
    item.ts = ts
    item.confidence = confidence
    return item


class QualityTrendAnalyzerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = QualityTrendAnalyzer()

    def test_empty_items_returns_stable(self) -> None:
        """Пустой список элементов → тренд 'stable'."""
        report = self.analyzer.analyze_trends([])
        self.assertIsInstance(report, TrendReport)
        self.assertEqual(report.overall_trend, "stable")
        self.assertEqual(report.daily_confidence, [])

    def test_single_item_no_trend(self) -> None:
        """Один элемент → stable тренд."""
        item = _make_item(days_ago=0, confidence=0.9)
        report = self.analyzer.analyze_trends([item])
        self.assertIn(report.overall_trend, ("stable", "improving", "declining"))

    def test_improving_trend(self) -> None:
        """Постоянно растущий confidence → тренд 'improving'."""
        items = []
        for i in range(10, 0, -1):
            items.append(_make_item(days_ago=i, confidence=0.5 + (10 - i) * 0.04))
        report = self.analyzer.analyze_trends(items, days=15)
        self.assertEqual(report.overall_trend, "improving")

    def test_declining_trend(self) -> None:
        """Постоянно падающий confidence → тренд 'declining'."""
        items = []
        for i in range(10, 0, -1):
            items.append(_make_item(days_ago=i, confidence=0.95 - (10 - i) * 0.04))
        report = self.analyzer.analyze_trends(items, days=15)
        self.assertEqual(report.overall_trend, "declining")

    def test_best_and_worst_day(self) -> None:
        """best_day и worst_day определяются корректно."""
        items = [
            _make_item(days_ago=2, confidence=0.95),
            _make_item(days_ago=2, confidence=0.90),
            _make_item(days_ago=1, confidence=0.60),
            _make_item(days_ago=1, confidence=0.55),
        ]
        report = self.analyzer.analyze_trends(items, days=5)
        self.assertIsInstance(report.best_day, dict)
        self.assertIsInstance(report.worst_day, dict)
        if report.best_day:
            self.assertGreater(report.best_day.get("avg", 0), report.worst_day.get("avg", 1))

    def test_confidence_distribution_keys(self) -> None:
        """confidence_distribution содержит ожидаемые бакеты."""
        items = [_make_item(days_ago=1, confidence=0.85)]
        report = self.analyzer.analyze_trends(items, days=7)
        expected_buckets = {"0.9-1.0", "0.8-0.9", "0.7-0.8", "0.6-0.7", "0.0-0.6"}
        self.assertEqual(set(report.confidence_distribution.keys()), expected_buckets)

    def test_items_outside_window_excluded(self) -> None:
        """Элементы вне окна days не учитываются."""
        items = [
            _make_item(days_ago=5, confidence=0.9),
            _make_item(days_ago=50, confidence=0.1),  # вне окна 30 дней
        ]
        report = self.analyzer.analyze_trends(items, days=30)
        # Убеждаемся что старый элемент не включён в распределение
        total_in_dist = sum(report.confidence_distribution.values())
        self.assertEqual(total_in_dist, 1)


class QualityTrendsIPCTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер analyze_quality_trends."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        from unittest.mock import MagicMock
        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

    def test_analyze_quality_trends_handler(self) -> None:
        """IPC-хэндлер analyze_quality_trends возвращает корректные поля."""
        resp = self.svc.handle_request(
            {"id": "1", "method": "analyze_quality_trends", "params": {"days": 7}}
        )
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("overall_trend", result)
        self.assertIn("daily_confidence", result)
        self.assertIn("confidence_distribution", result)


if __name__ == "__main__":
    unittest.main()
