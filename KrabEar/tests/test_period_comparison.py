"""Тесты compare_periods — сравнение периодов использования Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.period_comparison import compare_periods, ComparisonReport, PeriodStats

from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class PeriodComparisonTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def _period(self, offset_weeks: int) -> tuple[str, str]:
        """Возвращает (start, end) строки для недели, сдвинутой на offset_weeks."""
        end = date.today() - timedelta(weeks=offset_weeks)
        start = end - timedelta(days=6)
        return start.isoformat(), end.isoformat()

    def test_empty_store_zero_counts(self) -> None:
        """Пустое хранилище → обоим периодам 0 записей."""
        p1 = self._period(2)
        p2 = self._period(1)
        report = compare_periods(self.store, p1[0], p1[1], p2[0], p2[1])
        self.assertIsInstance(report, ComparisonReport)
        self.assertEqual(report.period1.recordings, 0)
        self.assertEqual(report.period2.recordings, 0)

    def test_returns_comparison_report(self) -> None:
        """compare_periods возвращает ComparisonReport с обязательными полями."""
        p1 = self._period(2)
        p2 = self._period(1)
        report = compare_periods(self.store, p1[0], p1[1], p2[0], p2[1])
        self.assertIsInstance(report.period1, PeriodStats)
        self.assertIsInstance(report.period2, PeriodStats)
        self.assertIsInstance(report.summary, str)
        self.assertIsInstance(report.new_languages, list)
        self.assertIsInstance(report.recordings_change_pct, float)
        self.assertIsInstance(report.duration_change_pct, float)

    def test_pct_change_zero_baseline(self) -> None:
        """Если period1 пустой, pct_change == 0 (деление на 0 защищено)."""
        p1 = self._period(4)
        p2 = self._period(1)
        report = compare_periods(self.store, p1[0], p1[1], p2[0], p2[1])
        self.assertEqual(report.recordings_change_pct, 0.0)

    def test_summary_is_non_empty_string(self) -> None:
        """summary содержит человекочитаемый текст."""
        p1 = self._period(2)
        p2 = self._period(1)
        report = compare_periods(self.store, p1[0], p1[1], p2[0], p2[1])
        self.assertGreater(len(report.summary), 0)

    def test_accepts_date_objects(self) -> None:
        """compare_periods принимает объекты date, а не только строки."""
        today = date.today()
        p1_start = today - timedelta(weeks=2)
        p1_end = today - timedelta(weeks=1, days=1)
        p2_start = today - timedelta(weeks=1)
        p2_end = today
        report = compare_periods(self.store, p1_start, p1_end, p2_start, p2_end)
        self.assertIsInstance(report, ComparisonReport)


class PeriodComparisonIPCTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер compare_periods."""

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

    def test_compare_periods_handler(self) -> None:
        """IPC-хэндлер compare_periods возвращает корректную структуру."""
        resp = self.svc.handle_request({
            "id": "1",
            "method": "compare_periods",
            "params": {
                "period1_start": "2024-01-01",
                "period1_end": "2024-01-07",
                "period2_start": "2024-01-08",
                "period2_end": "2024-01-14",
            },
        })
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("period1", result)
        self.assertIn("period2", result)
        self.assertIn("summary", result)
        self.assertIn("recordings_change_pct", result)

    def test_compare_periods_missing_params(self) -> None:
        """Отсутствие обязательных параметров возвращает ошибку."""
        resp = self.svc.handle_request({
            "id": "2",
            "method": "compare_periods",
            "params": {"period1_start": "2024-01-01"},
        })
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
