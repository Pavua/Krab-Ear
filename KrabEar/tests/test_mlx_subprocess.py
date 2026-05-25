"""Unit tests for core.mlx_subprocess — MLXWatchdog timeout + stats.

Wave 175: первый coverage для mlx_subprocess.py.

Design constraints:
  - NEVER import real mlx / mlx_whisper — все функции-калли мокаются.
  - Тесты стабильны на CI без Metal GPU.
  - Тайминг-зависимые тесты используют достаточно широкие допуски.
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.mlx_subprocess import (  # noqa: E402
    MLXTimeoutError,
    MLXWatchdog,
    get_watchdog,
    _should_report_to_sentry,
    _SENTRY_REPORT_THRESHOLDS,
)


class TestMLXTimeoutError(unittest.TestCase):
    """MLXTimeoutError — правильная инициализация и сообщение."""

    def test_attributes(self):
        exc = MLXTimeoutError(timeout_sec=30.0, model_name="mlx-whisper-large")
        self.assertEqual(exc.timeout_sec, 30.0)
        self.assertEqual(exc.model_name, "mlx-whisper-large")

    def test_message_contains_timeout_and_model(self):
        exc = MLXTimeoutError(timeout_sec=15.0, model_name="mlx-tiny")
        msg = str(exc)
        self.assertIn("15", msg)
        self.assertIn("mlx-tiny", msg)

    def test_is_runtime_error(self):
        exc = MLXTimeoutError(timeout_sec=1.0, model_name="test")
        self.assertIsInstance(exc, RuntimeError)


class TestMLXWatchdogSuccess(unittest.TestCase):
    """MLXWatchdog: успешные вызовы обновляют статистику."""

    def setUp(self):
        self.watchdog = MLXWatchdog()

    def test_returns_fn_result(self):
        result = self.watchdog.run_with_timeout(
            fn=lambda: {"segments": []},
            timeout_sec=5.0,
            model_name="test-model",
        )
        self.assertEqual(result, {"segments": []})

    def test_total_calls_incremented(self):
        self.watchdog.run_with_timeout(fn=lambda: 42, timeout_sec=5.0)
        self.watchdog.run_with_timeout(fn=lambda: 43, timeout_sec=5.0)
        stats = self.watchdog.get_stats()
        self.assertEqual(stats["total_calls"], 2)

    def test_success_count_incremented(self):
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        stats = self.watchdog.get_stats()
        self.assertEqual(stats["success_count"], 1)

    def test_crashes_count_zero_on_success(self):
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        self.assertEqual(self.watchdog.crashes_count, 0)

    def test_avg_response_time_positive_after_success(self):
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        self.assertGreater(self.watchdog.avg_response_time, 0.0)

    def test_avg_response_time_zero_initially(self):
        self.assertEqual(self.watchdog.avg_response_time, 0.0)

    def test_get_stats_structure(self):
        stats = self.watchdog.get_stats()
        for key in ("crashes_count", "total_calls", "success_count", "avg_response_time_sec"):
            self.assertIn(key, stats)

    def test_fn_exception_is_reraised(self):
        def _boom():
            raise ValueError("simulated STT error")

        with self.assertRaises(ValueError):
            self.watchdog.run_with_timeout(fn=_boom, timeout_sec=5.0)

    def test_fn_exception_increments_total_calls(self):
        def _boom():
            raise RuntimeError("oops")

        with self.assertRaises(RuntimeError):
            self.watchdog.run_with_timeout(fn=_boom, timeout_sec=5.0)
        self.assertEqual(self.watchdog.total_calls, 1)
        # success_count should NOT be incremented on exception
        self.assertEqual(self.watchdog.get_stats()["success_count"], 0)


class TestMLXWatchdogTimeout(unittest.TestCase):
    """MLXWatchdog: таймаут вызывает MLXTimeoutError и обновляет счётчик сбоев."""

    def setUp(self):
        self.watchdog = MLXWatchdog()

    def test_raises_mlx_timeout_error(self):
        """Функция, зависающая дольше таймаута, вызывает MLXTimeoutError."""
        barrier = threading.Event()

        def _hang():
            barrier.wait(timeout=10.0)  # will not be set → hangs until test ends

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=0.05, model_name="test-hang")
        barrier.set()  # release daemon thread

    def test_crashes_count_incremented_on_timeout(self):
        barrier = threading.Event()

        def _hang():
            barrier.wait(timeout=10.0)

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=0.05)
        barrier.set()
        self.assertEqual(self.watchdog.crashes_count, 1)

    def test_total_calls_incremented_on_timeout(self):
        barrier = threading.Event()

        def _hang():
            barrier.wait(timeout=10.0)

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=0.05)
        barrier.set()
        self.assertEqual(self.watchdog.total_calls, 1)

    def test_timeout_error_carries_model_name(self):
        barrier = threading.Event()

        def _hang():
            barrier.wait(timeout=10.0)

        try:
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=0.05, model_name="mlx-large")
        except MLXTimeoutError as e:
            self.assertEqual(e.model_name, "mlx-large")
            barrier.set()
        else:
            barrier.set()
            self.fail("MLXTimeoutError was not raised")

    def test_sentry_notification_called_on_timeout(self):
        """_notify_sentry_timeout should be invoked on timeout (no-op if sentry absent)."""
        barrier = threading.Event()

        def _hang():
            barrier.wait(timeout=10.0)

        with patch("core.mlx_subprocess._notify_sentry_timeout") as mock_notify:
            with self.assertRaises(MLXTimeoutError):
                self.watchdog.run_with_timeout(fn=_hang, timeout_sec=0.05, model_name="test")
            barrier.set()
            mock_notify.assert_called_once()


class TestMLXWatchdogConcurrency(unittest.TestCase):
    """Stats обновляются корректно при многопоточном использовании."""

    def test_concurrent_successful_calls(self):
        watchdog = MLXWatchdog()
        errors = []

        def _call():
            try:
                watchdog.run_with_timeout(fn=lambda: 1, timeout_sec=5.0)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_call) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(errors, [])
        stats = watchdog.get_stats()
        self.assertEqual(stats["total_calls"], 8)
        self.assertEqual(stats["success_count"], 8)
        self.assertEqual(stats["crashes_count"], 0)


class TestGetWatchdogSingleton(unittest.TestCase):
    """get_watchdog() returns the module-level singleton."""

    def test_singleton_identity(self):
        a = get_watchdog()
        b = get_watchdog()
        self.assertIs(a, b)

    def test_is_mlx_watchdog_instance(self):
        self.assertIsInstance(get_watchdog(), MLXWatchdog)


class TestShouldReportToSentry(unittest.TestCase):
    """_should_report_to_sentry throttle logic."""

    def test_threshold_values_are_reported(self):
        for threshold in _SENTRY_REPORT_THRESHOLDS:
            self.assertTrue(
                _should_report_to_sentry(threshold),
                f"Threshold {threshold} should be reported",
            )

    def test_non_threshold_values_not_reported(self):
        for count in (2, 3, 4, 6, 10, 50, 100, 200):
            if count not in _SENTRY_REPORT_THRESHOLDS:
                self.assertFalse(
                    _should_report_to_sentry(count),
                    f"Count {count} should NOT be reported",
                )

    def test_first_crash_always_reported(self):
        """crash_count=1 must always be in reporting thresholds."""
        self.assertIn(1, _SENTRY_REPORT_THRESHOLDS)
        self.assertTrue(_should_report_to_sentry(1))

    def test_zero_not_reported(self):
        self.assertFalse(_should_report_to_sentry(0))


if __name__ == "__main__":
    unittest.main()
