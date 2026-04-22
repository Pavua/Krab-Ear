"""Тесты для PerformanceProfiler."""

from backend.performance_profiler import PerformanceProfiler, SpanContext
import sys
import os
import time
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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


class TestNestedOps(unittest.TestCase):
    """Nested start_span calls must not interfere with each other."""

    def setUp(self):
        self.p = PerformanceProfiler(window_size=100)

    def test_nested_different_ops_both_recorded(self):
        """Outer and inner spans with different names both record independently."""
        with self.p.start_span("outer"):
            with self.p.start_span("inner"):
                time.sleep(0.005)
        report = self.p.get_profile_report()
        self.assertIn("outer", report["methods"])
        self.assertIn("inner", report["methods"])

    def test_nested_ops_do_not_corrupt_each_other(self):
        """Inner span timing must be less than outer span timing."""
        with self.p.start_span("outer"):
            time.sleep(0.005)
            with self.p.start_span("inner"):
                time.sleep(0.005)
        methods = self.p.get_profile_report()["methods"]
        outer_avg = methods["outer"]["avg_ms"]
        inner_avg = methods["inner"]["avg_ms"]
        # outer includes inner sleep + outer sleep so must be >= inner
        self.assertGreaterEqual(outer_avg, inner_avg)

    def test_same_name_nested_accumulates_twice(self):
        """Two spans with same name record two separate entries in window."""
        with self.p.start_span("op"):
            with self.p.start_span("op"):
                time.sleep(0.002)
        report = self.p.get_profile_report()
        self.assertEqual(report["methods"]["op"]["calls"], 2)

    def test_deeply_nested_ops(self):
        """Three levels of nesting all record correctly."""
        with self.p.start_span("l1"):
            with self.p.start_span("l2"):
                with self.p.start_span("l3"):
                    time.sleep(0.001)
        methods = self.p.get_profile_report()["methods"]
        for name in ("l1", "l2", "l3"):
            self.assertIn(name, methods)
            self.assertEqual(methods[name]["calls"], 1)


class TestGetStats(unittest.TestCase):
    """Tests for per-method stats: avg_ms, p50_ms, max_ms."""

    def setUp(self):
        self.p = PerformanceProfiler(window_size=200)

    def test_avg_ms_reasonable_after_known_sleep(self):
        """avg_ms should be >= 10ms after sleeping 10ms."""
        for _ in range(5):
            with self.p.start_span("sleeper"):
                time.sleep(0.010)
        stats = self.p.get_profile_report()["methods"]["sleeper"]
        self.assertGreaterEqual(stats["avg_ms"], 9.0)

    def test_p50_ms_between_min_and_max(self):
        """p50 must be between the shortest and longest recorded times."""
        for _ in range(20):
            with self.p.start_span("varied"):
                pass
        stats = self.p.get_profile_report()["methods"]["varied"]
        self.assertLessEqual(stats["p50_ms"], stats["max_ms"])
        self.assertGreaterEqual(stats["p50_ms"], 0)

    def test_max_ms_is_max(self):
        """max_ms must be >= avg_ms."""
        for _ in range(10):
            with self.p.start_span("m"):
                pass
        stats = self.p.get_profile_report()["methods"]["m"]
        self.assertGreaterEqual(stats["max_ms"], stats["avg_ms"])

    def test_multiple_runs_accumulate_call_count(self):
        """Running many times builds up the call counter correctly."""
        n = 30
        for _ in range(n):
            with self.p.start_span("run"):
                pass
        stats = self.p.get_profile_report()["methods"]["run"]
        self.assertEqual(stats["calls"], n)

    def test_reset_then_re_profile(self):
        """After reset, new timings start fresh with call count = 1."""
        for _ in range(5):
            with self.p.start_span("fresh"):
                pass
        self.p.reset()
        with self.p.start_span("fresh"):
            pass
        stats = self.p.get_profile_report()["methods"]["fresh"]
        self.assertEqual(stats["calls"], 1)

    def test_stats_for_unknown_op_not_in_methods(self):
        """Accessing stats for a never-profiled op: key absent in methods dict."""
        report = self.p.get_profile_report()
        self.assertNotIn("never_ran", report["methods"])

    def test_global_singleton_exists(self):
        """The module-level 'profiler' singleton should be a PerformanceProfiler."""
        from backend.performance_profiler import profiler as global_profiler
        self.assertIsInstance(global_profiler, PerformanceProfiler)


if __name__ == "__main__":
    unittest.main()
