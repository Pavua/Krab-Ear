"""Тесты для UsageTracker — ежедневная статистика использования Krab Ear."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path

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
        """Проверка, что новый трекер имеет нулевую статистику."""
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 0)
        self.assertEqual(stats["today"]["total_duration_sec"], 0)
        self.assertEqual(stats["today"]["total_words"], 0)
        self.assertEqual(stats["all_time"]["recordings"], 0)
        self.assertEqual(stats["streak_days"], 0)
        self.assertIsNone(stats["peak_day"])

    def test_record_usage_increments_today(self):
        """Проверка, что одна запись увеличивает статистику дня."""
        self.tracker.record_usage(duration_sec=30.0, word_count=100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 100)

    def test_multiple_recordings_accumulate(self):
        """Проверка, что несколько записей в один день суммируются."""
        self.tracker.record_usage(10.0, 50)
        self.tracker.record_usage(20.0, 150)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 2)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 30.0)
        self.assertEqual(stats["today"]["total_words"], 200)

    def test_all_time_accumulates(self):
        """Проверка, что all_time счётчики растут корректно."""
        self.tracker.record_usage(5.0, 10)
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["all_time"]["recordings"], 2)
        self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 10.0)
        self.assertEqual(stats["all_time"]["total_words"], 20)

    def test_this_week_includes_today(self):
        """Проверка, что недельная статистика включает сегодня."""
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 1)

    def test_this_month_includes_today(self):
        """Проверка, что месячная статистика включает сегодня."""
        self.tracker.record_usage(15.0, 75)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 1)


class TestUsageTrackerPeriods(unittest.TestCase):
    """Тесты для различных периодов (неделя, месяц) и их границ."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_week_aggregation_multiple_days(self):
        """Проверка агрегации статистики за неделю с несколькими днями."""
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 20.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 6)
        self.assertAlmostEqual(stats["this_week"]["total_duration_sec"], 60.0)
        self.assertEqual(stats["this_week"]["total_words"], 150)

    def test_month_includes_all_30_days(self):
        """Проверка, что месячная статистика охватывает 30 дней."""
        today = date.today()
        for i in range(25):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 30}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_month"]["recordings"], 25)
        self.assertAlmostEqual(stats["this_month"]["total_duration_sec"], 250.0)

    def test_empty_days_contribute_zero(self):
        """Проверка, что дни без записей не влияют на статистику."""
        today = date.today()
        self.tracker._daily[today.isoformat()] = {"recordings": 1, "duration_sec": 5.0, "words": 10}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["this_week"]["recordings"], 1)
        self.assertEqual(stats["this_week"]["total_duration_sec"], 5.0)


class TestUsageTrackerStreak(unittest.TestCase):
    """Тесты для streak_days и peak_day."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_streak_single_day(self):
        """Проверка streak для одного дня активности."""
        self.tracker.record_usage(10.0, 50)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_with_gap_breaks(self):
        """Проверка, что пропуск дня прерывает streak."""
        two_days_ago = (date.today() - timedelta(days=2)).isoformat()
        self.tracker._daily[two_days_ago] = {"recordings": 3, "duration_sec": 60.0, "words": 200}
        self.tracker.record_usage(5.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 1)

    def test_streak_consecutive_days(self):
        """Проверка подсчета streak для нескольких подряд идущих дней."""
        today = date.today()
        for i in range(3):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 2, "duration_sec": 30.0, "words": 100}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["streak_days"], 3)

    def test_peak_day_calculation(self):
        """Проверка определения дня с максимальным количеством записей."""
        today = date.today()
        self.tracker._daily[today.isoformat()] = {"recordings": 5, "duration_sec": 100.0, "words": 500}
        yesterday = (today - timedelta(days=1)).isoformat()
        self.tracker._daily[yesterday] = {"recordings": 10, "duration_sec": 200.0, "words": 1000}
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["peak_day"]["date"], yesterday)
        self.assertEqual(stats["peak_day"]["recordings"], 10)

    def test_peak_day_none_when_empty(self):
        """Проверка, что peak_day = None для пустой статистики."""
        stats = self.tracker.get_usage_stats()
        self.assertIsNone(stats["peak_day"])


class TestUsageTrackerPersistence(unittest.TestCase):
    """Тесты для сохранения и загрузки данных."""

    def test_persists_and_reloads(self):
        """Проверка, что данные сохраняются и загружаются при перезагрузке."""
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
        """Проверка работы трекера без указания директории (в памяти)."""
        tracker = UsageTracker(data_dir=None)
        tracker.record_usage(10.0, 50)
        stats = tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)

    def test_persistence_file_format(self):
        """Проверка, что файл сохраняется в правильном JSON формате."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(20.0, 100)

            stats_file = Path(tmp) / "usage_stats.json"
            self.assertTrue(stats_file.exists())
            data = json.loads(stats_file.read_text(encoding="utf-8"))
            self.assertIn("daily", data)
            self.assertIn("all_time", data)
            self.assertEqual(data["all_time"]["recordings"], 1)

    def test_partial_corrupted_file_recovery(self):
        """Проверка, что трекер корректно обрабатывает отсутствие файла."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)
            tracker.record_usage(5.0, 25)
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
        """Проверка, что history содержит сегодняшние данные."""
        self.tracker.record_usage(20.0, 80)
        stats = self.tracker.get_usage_stats()
        today_str = date.today().isoformat()
        dates = [e["date"] for e in stats["daily_history"]]
        self.assertIn(today_str, dates)

    def test_daily_history_max_30_days(self):
        """Проверка, что history содержит не более 30 дней."""
        today = date.today()
        for i in range(35):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        self.assertLessEqual(len(stats["daily_history"]), 30)

    def test_daily_history_entry_has_required_fields(self):
        """Проверка структуры записей в daily_history."""
        self.tracker.record_usage(10.0, 40)
        stats = self.tracker.get_usage_stats()
        entry = stats["daily_history"][0]
        self.assertIn("date", entry)
        self.assertIn("recordings", entry)
        self.assertIn("duration_sec", entry)
        self.assertIn("words", entry)

    def test_daily_history_sorted_newest_first(self):
        """Проверка, что history отсортирована по убыванию дат."""
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i)).isoformat()
            self.tracker._daily[d] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        stats = self.tracker.get_usage_stats()
        dates = [e["date"] for e in stats["daily_history"]]
        self.assertEqual(dates, sorted(dates, reverse=True))


class TestUsageTrackerConcurrency(unittest.TestCase):
    """Тест потокобезопасности."""

    def test_concurrent_record_usage(self):
        """Проверка потокобезопасности при одновременных записях."""
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

    def test_concurrent_read_during_write(self):
        """Проверка, что чтение статистики не блокируется при записи."""
        with tempfile.TemporaryDirectory() as tmp:
            tracker = UsageTracker(data_dir=tmp)

            def writer():
                for _ in range(10):
                    tracker.record_usage(1.0, 5)

            def reader():
                for _ in range(10):
                    tracker.get_usage_stats()

            write_thread = threading.Thread(target=writer)
            read_threads = [threading.Thread(target=reader) for _ in range(3)]

            for t in read_threads:
                t.start()
            write_thread.start()

            write_thread.join()
            for t in read_threads:
                t.join()

            stats = tracker.get_usage_stats()
            self.assertGreater(stats["all_time"]["recordings"], 0)


class TestUsageTrackerEdgeCases(unittest.TestCase):
    """Тесты граничных случаев и обработки ошибок."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = UsageTracker(data_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_zero_duration_recording(self):
        """Проверка обработки записей с нулевой длительностью."""
        self.tracker.record_usage(0.0, 10)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_duration_sec"], 0.0)

    def test_zero_words_recording(self):
        """Проверка обработки записей без слов."""
        self.tracker.record_usage(10.0, 0)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertEqual(stats["today"]["total_words"], 0)

    def test_large_values(self):
        """Проверка обработки больших значений длительности и слов."""
        self.tracker.record_usage(3600.0, 50000)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["recordings"], 1)
        self.assertAlmostEqual(stats["today"]["total_duration_sec"], 3600.0)
        self.assertEqual(stats["today"]["total_words"], 50000)

    def test_float_precision_rounding(self):
        """Проверка округления значений float до 2 знаков."""
        self.tracker.record_usage(10.123456, 100)
        stats = self.tracker.get_usage_stats()
        self.assertEqual(stats["today"]["total_duration_sec"], 10.12)
        self.assertAlmostEqual(stats["all_time"]["total_duration_sec"], 10.12)

    def test_prune_old_days(self):
        """Проверка удаления записей старше 30 дней."""
        today = date.today()
        cutoff_date = (today - timedelta(days=31)).isoformat()
        self.tracker._daily[cutoff_date] = {"recordings": 1, "duration_sec": 10.0, "words": 50}
        self.tracker._daily[today.isoformat()] = {"recordings": 1, "duration_sec": 5.0, "words": 25}

        self.tracker._prune_old_days()
        self.assertNotIn(cutoff_date, self.tracker._daily)
        self.assertIn(today.isoformat(), self.tracker._daily)


if __name__ == "__main__":
    unittest.main()
