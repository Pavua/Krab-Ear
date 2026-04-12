"""Тесты для UsageTracker — ежедневная статистика использования Krab Ear."""

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

# Настройка путей для импорта
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.usage_tracker import UsageTracker


class TestUsageTrackerBasic(unittest.TestCase):
    """Базовые тесты записи и чтения статистики."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initial_stats_are_zero(self):
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 0)
        self.assertEqual(stats["today"]["total_duration_sec"], 0)
        self.assertEqual(stats["today"]["total_words"], 0)
        self.assertEqual(stats["all_time"]["recordings"], 0)
        self.assertEqual(stats["streak_days"], 0)
        self.assertIsNone(stats["peak_day"])

    def test_record_usage_increments_today(self):
        self.tracker.record_usage(duration_sec=30.0, word_count=100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 100)

    def test_multiple_recordings_accumulate(self):
        self.tracker.record_usage(10.0, 50)
        self.tracker.record_usage(20.0, 150)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 2)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 200)

    def test_all_time_accumulates(self):
        self.tracker.record_usage(5.0, 10)
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 2)
        self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 10.0)
        self.assertEqual(stats["all_time"]["total_words"], 20)

    def test_this_week_includes_today(self):
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 1)

    def test_this_month_includes_today(self):
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 1)


class TestUsageTrackerStreak(unittest.TestCase):
    """Тесты для streak_days и peak_day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_streak_single_day(self):
        self.tracker.record_usage(10.0, 50)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_with_gap_breaks(self):
        # Inject two days ago but not yesterday → streak from today = 1
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        self.tracker._daily[two_days_ago] = {"recordings": 3, "duration_sec": 60.0, "words": 200}
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        # today has recording, yesterday doesn't → streak = 1
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_consecutive_days(self):
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 30.0, "words": 100}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 3)

    def test_peak_day_calculation(self):
        today = date.today()
        self.tracker._daily[today.isoformat()] = {"recordings": 5, "duration_sec": 100.0, "words": 500}
        yesterday = (today - timedelta(days=1)).isoformat()
        self.tracker._daily[yesterday] = {"recordings": 10, "duration_sec": 200.0, "words": 1000}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["peak_day"]["date"], yesterday)
        self.assertEqual(stats["peak_day"]["recordings"], 10)


class TestUsageTrackerPersistence(unittest.TestCase):
    """Тесты для сохранения и загрузки данных."""

    def test_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            t1 = UsageTracker(data_dir=tmp)
            t1.record_usage(30.0, 200)
            t1.record_usage(15.0, 100)

            t2 = UsageTracker(data_dir=tmp)
            stats = t2.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 2)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 45.0)
            self.assertEqual(stats["all_time"]["total_words"], 300)

    def test_no_data_dir_does_not_crash(self):
        tracker = UsageTracker(data_dir=None)
        tracker.record_usage(10.0, 50)
        stats = tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)


class TestUsageTrackerDailyHistory(unittest.TestCase):
    """Тесты для daily_history."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_daily_history_contains_today(self):
        self.tracker.record_usage(20.0, 80)
        stats = self.tracker.get_usage_stats()
        today_str = date.today().isoformat()
        dates = [e["date"] for e in stats["daily_history"]]
        self.assertIn(today_str, dates)

    def test_daily_history_max_30_days(self):
        today = date.today()
        for i in range(35):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        self.assertLessEqual(len(stats["daily_history"]), 30)

    def test_daily_history_entry_has_required_fields(self):
        self.tracker.record_usage(10.0, 40)
        stats = self.tracker.get_usage_stats()
        entry = stats["daily_history"][0]
        self.assertIn("date", entry)
        self.assertIn("recordings", entry)
        self.assertIn("duration_sec", entry)
        self.assertIn("words", entry)


class TestUsageTrackerConcurrency(unittest.TestCase):
    """Тест потокобезопасности."""

    def test_concurrent_record_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            threads = [
                threading.Thread(target=tracker.record_usage, args=(1.0, 5))
                for _ in range(50)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stats = tracker.get_usage_stats()
            self.assertEqual(stats["all_time"]["recordings"], 50)
            self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 50.0)


if __name__ == "__main__":
    unittest.main()
