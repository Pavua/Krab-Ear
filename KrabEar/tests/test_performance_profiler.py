"""Тесты для PerformanceProfiler."""

import sys
import os
import time
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.performance_profiler import PerformanceProfiler, SpanContext


class TestPerformanceProfilerBasic(unittest.TestCase):
    def setUp(self):
        self.p = PerformanceProfiler(window_size=100)

    def test_start_span_records_timing(self):
        """start_span должен записывать ненулевое время выполнения."""
        with self.p.start_span("test_op"):
            time.sleep(0.01)
        report = self.p.get_profile_report()
        self.assertIn("test_op", report["methods"])
        self.assertGreater(report["methods"]["test_op"]["avg_ms"], 0)

    def test_profile_decorator_records_timing(self):
        """Декоратор @profile должен автоматически записывать время."""
        @self.p.profile
        def slow_fn():
            time.sleep(0.01)
            return 42

        result = slow_fn()
        self.assertEqual(result, 42)
        report = self.p.get_profile_report()
        # qualname будет что-то типа test_profile_decorator_records_timing.<locals>.slow_fn
        keys = list(report["methods"].keys())
        self.assertTrue(any("slow_fn" in k for k in keys))

    def test_profile_decorator_returns_correct_value(self):
        """Декоратор не должен ломать возвращаемое значение функции."""
        @self.p.profile
        def fn(x, y):
            return x + y

        self.assertEqual(fn(3, 4), 7)

    def test_multiple_calls_accumulate(self):
        """Несколько вызовов должны накапливаться в скользящем окне."""
        for _ in range(5):
            with self.p.start_span("op"):
                time.sleep(0.002)
        report = self.p.get_profile_report()
        self.assertEqual(report["methods"]["op"]["calls"], 5)

    def test_sliding_window_max(self):
        """Скользящее окно не должно превышать window_size."""
        p = PerformanceProfiler(window_size=10)
        for _ in range(25):
            with p.start_span("op"):
                pass
        report = p.get_profile_report()
        self.assertEqual(report["methods"]["op"]["calls"], 10)

    def test_get_profile_report_structure(self):
        """Отчёт должен содержать ожидаемые ключи верхнего уровня."""
        with self.p.start_span("x"):
            pass
        report = self.p.get_profile_report()
        self.assertIn("methods", report)
        self.assertIn("slowest_methods", report)
        self.assertIn("total_profiled_time_sec", report)

    def test_method_stats_keys(self):
        """Каждый метод в отчёте должен содержать calls/avg_ms/p50_ms/p95_ms/max_ms."""
        with self.p.start_span("a"):
            time.sleep(0.005)
        stats = self.p.get_profile_report()["methods"]["a"]
        for key in ("calls", "avg_ms", "p50_ms", "p95_ms", "max_ms"):
            self.assertIn(key, stats)

    def test_slowest_methods_top10(self):
        """slowest_methods должен содержать не более 10 элементов."""
        for i in range(15):
            with self.p.start_span(f"method_{i}"):
                time.sleep(0.001)
        report = self.p.get_profile_report()
        self.assertLessEqual(len(report["slowest_methods"]), 10)

    def test_slowest_methods_sorted_by_avg(self):
        """slowest_methods должны быть отсортированы по убыванию avg_ms."""
        @self.p.profile
        def fast():
            time.sleep(0.001)

        @self.p.profile
        def slow():
            time.sleep(0.02)

        for _ in range(3):
            fast()
            slow()

        report = self.p.get_profile_report()
        methods = report["methods"]
        slowest = report["slowest_methods"]
        if len(slowest) >= 2:
            avgs = [methods[n]["avg_ms"] for n in slowest]
            self.assertEqual(avgs, sorted(avgs, reverse=True))

    def test_reset_clears_data(self):
        """reset() должен очищать все накопленные данные."""
        with self.p.start_span("op"):
            pass
        self.p.reset()
        report = self.p.get_profile_report()
        self.assertEqual(report["methods"], {})
        self.assertEqual(report["total_profiled_time_sec"], 0.0)

    def test_empty_report(self):
        """Пустой профайлер должен возвращать корректный пустой отчёт."""
        report = self.p.get_profile_report()
        self.assertEqual(report["methods"], {})
        self.assertEqual(report["slowest_methods"], [])
        self.assertEqual(report["total_profiled_time_sec"], 0.0)

    def test_thread_safety(self):
        """Параллельные записи не должны вызывать ошибок."""
        errors = []

        def worker(name):
            try:
                for _ in range(50):
                    with self.p.start_span(name):
                        pass
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        report = self.p.get_profile_report()
        for i in range(5):
            self.assertIn(f"t{i}", report["methods"])

    def test_span_context_is_context_manager(self):
        """SpanContext должен реализовывать протокол контекстного менеджера."""
        span = self.p.start_span("ctx")
        self.assertIsInstance(span, SpanContext)
        self.assertTrue(hasattr(span, "__enter__") and hasattr(span, "__exit__"))

    def test_total_profiled_time_accumulates(self):
        """total_profiled_time_sec должен быть > 0 после нескольких вызовов."""
        for _ in range(3):
            with self.p.start_span("work"):
                time.sleep(0.005)
        report = self.p.get_profile_report()
        self.assertGreater(report["total_profiled_time_sec"], 0)


if __name__ == "__main__":
    unittest.main()
