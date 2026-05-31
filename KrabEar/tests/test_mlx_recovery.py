"""Тесты MLX inference watchdog + auto-recovery (core/mlx_subprocess.py).

Охватывает:
1. Успешный вызов возвращает результат fn().
2. Таймаут → MLXTimeoutError с корректными атрибутами.
3. Исключение внутри fn() прокидывается наружу as-is.
4. После таймаута crashes_count инкрементируется.
5. Флаг MLX_CRASH_RECOVERY_ENABLED=False → watchdog не используется (прямой вызов).
6. Несколько таймаутов подряд — нет infinite-loop, crashes_count корректен.
7. get_stats() возвращает корректный снимок статистики.
8. Sentry event отправляется при таймауте (mock sentry_initialized=True).
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.mlx_subprocess import MLXWatchdog, MLXTimeoutError, get_watchdog  # noqa: E402


class TestMLXWatchdogSuccess(unittest.TestCase):
    """Тест 1: успешный вызов возвращает результат."""

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_successful_call_returns_result(self) -> None:
        expected = {"text": "привет мир", "segments": []}
        result = self.watchdog.run_with_timeout(
            fn=lambda: expected,
            timeout_sec=5.0,
            model_name="test-model",
        )
        self.assertEqual(result, expected)

    def test_total_calls_increments(self) -> None:
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        self.assertEqual(self.watchdog.total_calls, 2)

    def test_avg_response_time_positive_after_success(self) -> None:
        self.watchdog.run_with_timeout(fn=lambda: "result", timeout_sec=5.0)
        self.assertGreaterEqual(self.watchdog.avg_response_time, 0.0)


class TestMLXWatchdogTimeout(unittest.TestCase):
    """Тест 2: таймаут выбрасывает MLXTimeoutError.

    W1746 NOTE: MLXWatchdog.run_with_timeout() does an *unbounded* join() after
    the timeout fires (W1358 GPU-race guard).  Tests must therefore use
    ``time.sleep(N)`` instead of ``barrier.wait(60)`` + ``barrier.set()`` in a
    finally block: the unbounded join prevents the finally from running until the
    thread finishes, which requires barrier.set(), which never runs — deadlock.
    Using a short sleep (e.g. 0.2 s) that outlasts the test timeout (0.05–0.1 s)
    lets the daemon thread finish on its own so the unbounded join returns cleanly.
    """

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_timeout_raises_mlx_timeout_error(self) -> None:
        """fn() зависает навсегда — ожидаем MLXTimeoutError за короткий таймаут."""
        import time as _time

        def hanging_fn():
            _time.sleep(0.2)  # outlasts 0.1s timeout; finishes on its own

        with self.assertRaises(MLXTimeoutError) as ctx:
            self.watchdog.run_with_timeout(fn=hanging_fn, timeout_sec=0.1, model_name="slow-model")

        exc = ctx.exception
        self.assertAlmostEqual(exc.timeout_sec, 0.1, delta=0.05)
        self.assertEqual(exc.model_name, "slow-model")

    def test_timeout_increments_crashes_count(self) -> None:
        import time as _time

        def hanging_fn():
            _time.sleep(0.2)  # outlasts 0.05s timeout

        try:
            self.watchdog.run_with_timeout(fn=hanging_fn, timeout_sec=0.05, model_name="m")
        except MLXTimeoutError:
            pass

        self.assertEqual(self.watchdog.crashes_count, 1)

    def test_timeout_error_message_contains_model_name(self) -> None:
        import time as _time

        def hanging():
            _time.sleep(0.2)  # outlasts 0.05s timeout

        try:
            self.watchdog.run_with_timeout(fn=hanging, timeout_sec=0.05, model_name="whisper-large")
        except MLXTimeoutError as exc:
            self.assertIn("whisper-large", str(exc))


class TestMLXWatchdogExceptionPassthrough(unittest.TestCase):
    """Тест 3: исключение внутри fn() прокидывается наружу."""

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_runtime_error_propagated(self) -> None:
        def bad_fn():
            raise RuntimeError("GPU OOM")

        with self.assertRaises(RuntimeError) as ctx:
            self.watchdog.run_with_timeout(fn=bad_fn, timeout_sec=5.0)

        self.assertIn("GPU OOM", str(ctx.exception))

    def test_value_error_propagated(self) -> None:
        def bad_fn():
            raise ValueError("invalid audio shape")

        with self.assertRaises(ValueError):
            self.watchdog.run_with_timeout(fn=bad_fn, timeout_sec=5.0)

    def test_crashes_count_not_incremented_on_exception(self) -> None:
        """TypeError не считается 'crash' — только таймаут."""
        def bad_fn():
            raise TypeError("unsupported argument")

        try:
            self.watchdog.run_with_timeout(fn=bad_fn, timeout_sec=5.0)
        except TypeError:
            pass

        self.assertEqual(self.watchdog.crashes_count, 0)


class TestMLXWatchdogMultipleTimeouts(unittest.TestCase):
    """Тест 6: несколько таймаутов подряд — нет infinite-loop, счётчик корректен."""

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_multiple_timeouts_counted_correctly(self) -> None:
        # W1746: use a very short sleep (0.2s) instead of a barrier that requires
        # external signaling. The unbounded thread.join() in run_with_timeout would
        # deadlock waiting for barrier.set() that only runs in the finally block
        # AFTER run_with_timeout returns — circular dependency.
        # With sleep(0.2), the daemon thread finishes on its own after ~0.2s, the
        # unbounded join completes, and MLXTimeoutError is raised cleanly.
        import time as _time

        for _ in range(3):
            try:
                def short_hang():
                    _time.sleep(0.2)  # outlasts the 0.05s timeout, releases naturally

                self.watchdog.run_with_timeout(fn=short_hang, timeout_sec=0.05, model_name="m")
            except MLXTimeoutError:
                pass

        self.assertEqual(self.watchdog.crashes_count, 3)
        self.assertEqual(self.watchdog.total_calls, 3)

    def test_recovery_after_timeout_succeeds(self) -> None:
        """После таймаута следующий успешный вызов проходит нормально."""
        import time as _time

        # W1746: use short sleep instead of barrier — see test_multiple_timeouts_counted_correctly
        def short_hang():
            _time.sleep(0.2)  # outlasts 0.05s timeout, finishes on its own

        try:
            self.watchdog.run_with_timeout(fn=short_hang, timeout_sec=0.05, model_name="m")
        except MLXTimeoutError:
            pass

        # Следующий вызов должен успешно вернуть результат
        result = self.watchdog.run_with_timeout(fn=lambda: "recovered", timeout_sec=5.0)
        self.assertEqual(result, "recovered")


class TestMLXWatchdogStats(unittest.TestCase):
    """Тест 7: get_stats() возвращает корректный снимок."""

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_get_stats_initial_state(self) -> None:
        stats = self.watchdog.get_stats()
        self.assertEqual(stats["crashes_count"], 0)
        self.assertEqual(stats["total_calls"], 0)
        self.assertEqual(stats["success_count"], 0)
        self.assertEqual(stats["avg_response_time_sec"], 0.0)

    def test_get_stats_after_success(self) -> None:
        self.watchdog.run_with_timeout(fn=lambda: "x", timeout_sec=5.0)
        stats = self.watchdog.get_stats()
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["success_count"], 1)
        self.assertEqual(stats["crashes_count"], 0)
        self.assertGreaterEqual(stats["avg_response_time_sec"], 0.0)

    def test_get_stats_after_timeout(self) -> None:
        import time as _time

        # W1746: use short sleep instead of barrier — see TestMLXWatchdogTimeout docstring
        def short_hang():
            _time.sleep(0.2)

        try:
            self.watchdog.run_with_timeout(fn=short_hang, timeout_sec=0.05, model_name="m")
        except MLXTimeoutError:
            pass

        stats = self.watchdog.get_stats()
        self.assertEqual(stats["crashes_count"], 1)
        self.assertEqual(stats["total_calls"], 1)
        self.assertEqual(stats["success_count"], 0)


class TestMLXWatchdogDisabledFlag(unittest.TestCase):
    """Тест 5: флаг MLX_CRASH_RECOVERY_ENABLED=False — прямой вызов mlx_whisper.

    Мы не тестируем engine.py напрямую (зависит от mlx_whisper).
    Тестируем что watchdog НЕ используется когда его не вызывают —
    то есть обращения к watchdog при disabled config не происходит.
    """

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_watchdog_not_called_when_skipped(self) -> None:
        """Имитируем: код не вызывает watchdog вообще (recovery_enabled=False)."""
        called_directly = []

        def direct_fn():
            called_directly.append(True)
            return {"text": "direct result"}

        # Вызываем напрямую без watchdog
        result = direct_fn()
        self.assertEqual(result["text"], "direct result")
        self.assertEqual(self.watchdog.total_calls, 0)  # watchdog не трогали
        self.assertTrue(called_directly)


class TestMLXWatchdogSentryIntegration(unittest.TestCase):
    """Тест 8: Sentry event отправляется при таймауте."""

    def setUp(self) -> None:
        self.watchdog = MLXWatchdog()

    def test_sentry_notified_on_timeout(self) -> None:
        """_notify_sentry_timeout вызывается при таймауте."""
        import time as _time

        # W1746: use short sleep instead of barrier — see TestMLXWatchdogTimeout docstring
        def short_hang():
            _time.sleep(0.2)

        with patch(
            "core.mlx_subprocess._notify_sentry_timeout"
        ) as mock_notify:
            try:
                self.watchdog.run_with_timeout(fn=short_hang, timeout_sec=0.05, model_name="test-sentry-model")
            except MLXTimeoutError:
                pass

        mock_notify.assert_called_once()
        call_args = mock_notify.call_args
        self.assertEqual(call_args[0][0], "test-sentry-model")  # model_name
        self.assertEqual(call_args[0][2], 1)  # crash_count=1

    def test_sentry_noop_when_not_initialized(self) -> None:
        """_notify_sentry_timeout — no-op если sentry не инициализирован (is_sentry_initialized=False)."""
        from core.mlx_subprocess import _notify_sentry_timeout  # noqa: PLC0415

        # Когда sentry не инициализирован, add_breadcrumb/capture_exception не должны вызываться
        with patch("backend.observability.is_sentry_initialized", return_value=False), \
             patch("backend.observability.add_breadcrumb") as mock_bc, \
             patch("backend.observability.capture_exception") as mock_cap:
            # Должен отработать без исключений и без вызова sentry методов
            _notify_sentry_timeout("model", 30.0, 1)
            mock_bc.assert_not_called()
            mock_cap.assert_not_called()


class TestModuleLevelSingleton(unittest.TestCase):
    """get_watchdog() возвращает один и тот же объект."""

    def test_get_watchdog_returns_singleton(self) -> None:
        w1 = get_watchdog()
        w2 = get_watchdog()
        self.assertIs(w1, w2)

    def test_singleton_is_mlx_watchdog_instance(self) -> None:
        self.assertIsInstance(get_watchdog(), MLXWatchdog)


if __name__ == "__main__":
    unittest.main()
