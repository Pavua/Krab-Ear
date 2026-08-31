"""Tests for wave-20 MED fix: bounded thread.join() in MLXWatchdog.

Finding: on a GPU hang the watchdog did an UNBOUNDED thread.join() (W1358 race-guard),
stalling the entire backend indefinitely if the Metal GPU truly seized.

Fix: thread.join(timeout=MLX_HANG_HARD_KILL_SEC).  If the join times out:
  - log an error
  - raise MLXTimeoutError (caller recovers via fallback chain)
  - daemon thread is "orphaned" (Python threads cannot be forcibly killed), but
    the process exit is guaranteed because it's a daemon thread.

Design constraints:
  - No real MLX/mlx_whisper — all hangs are mocked via threading.Event barriers.
  - Wall-clock tests use a tiny patched MLX_HANG_HARD_KILL_SEC so they run fast.
  - Never weaken assertions to make tests pass.
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import core.mlx_subprocess as _mod  # noqa: E402
from tests.timing_budgets import NONBLOCKING_BUDGET_SEC
from core.mlx_subprocess import (  # noqa: E402
    MLXTimeoutError,
    MLXWatchdog,
    MLX_HANG_HARD_KILL_SEC,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Short timeouts for tests so they complete quickly.
_INFERENCE_TIMEOUT = 0.04   # first join(): initial inference timeout (40 ms)
_HARD_KILL_SHORT = 0.15     # patched MLX_HANG_HARD_KILL_SEC for "still alive" tests
# Второй раунд гонки (2026-07-23): исходные Timer(0.06-0.08) давали запас
# всего 20-40мс над _INFERENCE_TIMEOUT — не хватало на загруженном CI-раннере
# (daemon успевал завершиться ДО первого join(), MLXTimeoutError не наступал).
# release срабатывает через wait(timeout=...) самого потока — без Timer,
# без гонки старта — с запасом 300мс.
_RELEASE_DELAY = _INFERENCE_TIMEOUT + 0.3


class TestMLXHangHardKillConstant(unittest.TestCase):
    """MLX_HANG_HARD_KILL_SEC module constant sanity checks."""

    def test_constant_is_positive_float(self):
        self.assertIsInstance(MLX_HANG_HARD_KILL_SEC, float)
        self.assertGreater(MLX_HANG_HARD_KILL_SEC, 0.0)

    def test_default_value_is_10(self):
        """KRAB-EAR-BACKEND-1V: дефолт 10с, не прежние 120с (IPC backstop 180с)."""
        self.assertEqual(MLX_HANG_HARD_KILL_SEC, 10.0)

    def test_constant_exported(self):
        """MLX_HANG_HARD_KILL_SEC must be importable from the module."""
        from core.mlx_subprocess import MLX_HANG_HARD_KILL_SEC as hk
        self.assertGreater(hk, 0.0)


class TestBoundedJoinNormalTimeout(unittest.TestCase):
    """Join completes within MLX_HANG_HARD_KILL_SEC — normal hang recovery path.

    Simulates: GPU hang recovers on its own within the hard-kill window.
    Expected: MLXTimeoutError raised, no infinite stall.
    """

    def setUp(self):
        self.watchdog = MLXWatchdog()

    def test_raises_mlx_timeout_error_after_bounded_join(self):
        """When daemon finishes before hard-kill, MLXTimeoutError is still raised."""
        allow_finish = threading.Event()

        def _hang():
            allow_finish.wait(timeout=_RELEASE_DELAY)

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(
                fn=_hang,
                timeout_sec=_INFERENCE_TIMEOUT,
                model_name="bounded-join-test",
            )

    def test_timeout_error_carries_correct_timeout_sec(self):
        allow_finish = threading.Event()

        def _hang():
            allow_finish.wait(timeout=_RELEASE_DELAY)

        try:
            self.watchdog.run_with_timeout(
                fn=_hang,
                timeout_sec=_INFERENCE_TIMEOUT,
                model_name="timeout-sec-test",
            )
        except MLXTimeoutError as exc:
            self.assertAlmostEqual(exc.timeout_sec, _INFERENCE_TIMEOUT, places=5)
        else:
            self.fail("MLXTimeoutError not raised")

    def test_crashes_count_incremented(self):
        allow_finish = threading.Event()

        def _hang():
            allow_finish.wait(timeout=_RELEASE_DELAY)

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=_INFERENCE_TIMEOUT)

        self.assertEqual(self.watchdog.crashes_count, 1)

    def test_daemon_finished_before_mlxtimeouterror_propagates(self):
        """W1358 race-guard still holds: daemon must exit before error propagates
        when daemon exits within the hard-kill window."""
        thread_finished = threading.Event()
        allow_finish = threading.Event()

        def _hang():
            allow_finish.wait(timeout=_RELEASE_DELAY)
            thread_finished.set()  # set AFTER blocking op completes

        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(
                fn=_hang,
                timeout_sec=_INFERENCE_TIMEOUT,
                model_name="race-guard-check",
            )

        self.assertTrue(
            thread_finished.is_set(),
            "Daemon thread was still running when MLXTimeoutError propagated "
            "(W1358 race-guard violated for recoverable hang path).",
        )

    def test_returns_within_reasonable_time(self):
        """run_with_timeout() must return within inference_timeout + hard_kill + slack."""
        allow_finish = threading.Event()

        def _hang():
            allow_finish.wait(timeout=_RELEASE_DELAY)

        # Тот же структурный принцип: регресс — неограниченное ожидание (~10с),
        # а не лишние сотые доли секунды на загруженном раннере.
        deadline = NONBLOCKING_BUDGET_SEC
        t0 = time.monotonic()
        with self.assertRaises(MLXTimeoutError):
            self.watchdog.run_with_timeout(fn=_hang, timeout_sec=_INFERENCE_TIMEOUT)
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, deadline, f"run_with_timeout took {elapsed:.3f}s > {deadline}s")


class TestBoundedJoinHardKillExceeded(unittest.TestCase):
    """Daemon is STILL ALIVE after MLX_HANG_HARD_KILL_SEC — absolute worst case.

    Simulates: Metal GPU completely frozen, daemon thread never returns.
    Fix: bounded join times out, raises MLXTimeoutError (backend can recover).
    Verification: assert call returns (doesn't block) and raises MLXTimeoutError.
    """

    def test_raises_mlx_timeout_error_when_hard_kill_exceeded(self):
        """Daemon still alive after hard-kill — MLXTimeoutError must still be raised."""
        # Daemon will NEVER finish during the test (blocked forever).
        stuck_forever = threading.Event()  # never set

        def _truly_stuck():
            stuck_forever.wait()  # blocks indefinitely

        # Patch MLX_HANG_HARD_KILL_SEC to a tiny value so test runs fast
        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", _HARD_KILL_SHORT):
            watchdog = MLXWatchdog()
            with self.assertRaises(MLXTimeoutError):
                watchdog.run_with_timeout(
                    fn=_truly_stuck,
                    timeout_sec=_INFERENCE_TIMEOUT,
                    model_name="hard-kill-exceeded",
                )
        # Daemon still running as daemon thread — will be reaped on process exit.
        # No assertion on thread state here (we don't have a handle to it).

    def test_call_returns_within_bounded_time(self):
        """The call MUST return within inference_timeout + hard_kill + slack.

        This is the core regression test: before the fix, an infinite hang meant
        this assertion would never complete.
        """
        stuck_forever = threading.Event()  # never set

        def _truly_stuck():
            stuck_forever.wait()

        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", _HARD_KILL_SHORT):
            watchdog = MLXWatchdog()
            # 🔴 Порог структурный, а не хронометрический. Регресс здесь —
            # НЕОГРАНИЧЕННОЕ ожидание: непатченная MLX_HANG_HARD_KILL_SEC = 10.0
            # дала бы ~10с. Прежний бюджет (timeout+hard_kill+0.3) отличал не
            # «починено/сломано», а «раннер свободен/занят» — падал на 0.585с
            # при лимите 0.490с (31.08.2026).
            deadline = NONBLOCKING_BUDGET_SEC
            t0 = time.monotonic()
            try:
                watchdog.run_with_timeout(
                    fn=_truly_stuck,
                    timeout_sec=_INFERENCE_TIMEOUT,
                    model_name="bounded-wall-clock",
                )
            except MLXTimeoutError:
                pass
            except Exception as exc:
                self.fail(f"Unexpected exception: {exc}")
            elapsed = time.monotonic() - t0

        self.assertLess(
            elapsed,
            deadline,
            f"run_with_timeout hung for {elapsed:.3f}s — "
            f"bounded join did not fire within {deadline:.3f}s. "
            "The infinite-stall MED bug (wave-20) may still be present.",
        )

    def test_crashes_count_incremented_on_hard_kill(self):
        stuck_forever = threading.Event()

        def _truly_stuck():
            stuck_forever.wait()

        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", _HARD_KILL_SHORT):
            watchdog = MLXWatchdog()
            with self.assertRaises(MLXTimeoutError):
                watchdog.run_with_timeout(
                    fn=_truly_stuck,
                    timeout_sec=_INFERENCE_TIMEOUT,
                )
            self.assertEqual(watchdog.crashes_count, 1)

    def test_error_logged_when_hard_kill_exceeded(self):
        """An error-level log must be emitted when daemon outlives hard-kill timeout."""
        stuck_forever = threading.Event()

        def _truly_stuck():
            stuck_forever.wait()

        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", _HARD_KILL_SHORT):
            with patch.object(_mod.logger, "error") as mock_error:
                watchdog = MLXWatchdog()
                with self.assertRaises(MLXTimeoutError):
                    watchdog.run_with_timeout(
                        fn=_truly_stuck,
                        timeout_sec=_INFERENCE_TIMEOUT,
                        model_name="hard-kill-log-test",
                    )
                # At least one error-level log must mention the hard-kill timeout
                error_calls = [str(c) for c in mock_error.call_args_list]
                self.assertTrue(
                    any("STILL ALIVE" in c or "hard-kill" in c.lower() for c in error_calls),
                    f"Expected 'STILL ALIVE' error log, got: {error_calls}",
                )

    def test_total_calls_incremented_on_hard_kill(self):
        stuck_forever = threading.Event()

        def _truly_stuck():
            stuck_forever.wait()

        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", _HARD_KILL_SHORT):
            watchdog = MLXWatchdog()
            with self.assertRaises(MLXTimeoutError):
                watchdog.run_with_timeout(fn=_truly_stuck, timeout_sec=_INFERENCE_TIMEOUT)
        self.assertEqual(watchdog.total_calls, 1)


class TestBoundedJoinFastPathUnaffected(unittest.TestCase):
    """Fast fn() (completes before timeout_sec) must be unaffected by the fix.

    The bounded-join path is only taken when thread.is_alive() after the initial
    join.  A fast fn() exits the normal path — no regression.
    """

    def setUp(self):
        self.watchdog = MLXWatchdog()

    def test_fast_fn_returns_result(self):
        result = self.watchdog.run_with_timeout(
            fn=lambda: {"segments": ["hello"]},
            timeout_sec=5.0,
            model_name="fast-path",
        )
        self.assertEqual(result, {"segments": ["hello"]})

    def test_fast_fn_no_crash_counted(self):
        self.watchdog.run_with_timeout(fn=lambda: 42, timeout_sec=5.0)
        self.assertEqual(self.watchdog.crashes_count, 0)

    def test_fast_fn_success_count_incremented(self):
        self.watchdog.run_with_timeout(fn=lambda: "ok", timeout_sec=5.0)
        self.assertEqual(self.watchdog.get_stats()["success_count"], 1)

    def test_fast_fn_exception_reraised(self):
        def _boom():
            raise ValueError("test error")

        with self.assertRaises(ValueError):
            self.watchdog.run_with_timeout(fn=_boom, timeout_sec=5.0)


class TestEnvOverrideHardKillSec(unittest.TestCase):
    """MLX_HANG_HARD_KILL_SEC is overridable at module import via env var.

    We can't easily re-import the module to test env-based override in-process
    (module is already loaded), so we verify the patch.object path works and
    that the module reads os.environ correctly via direct attribute check.
    """

    def test_patched_value_is_used_by_watchdog(self):
        """When MLX_HANG_HARD_KILL_SEC is patched, run_with_timeout uses the patched value."""
        stuck_forever = threading.Event()

        def _truly_stuck():
            stuck_forever.wait()

        tiny_timeout = 0.08
        with patch.object(_mod, "MLX_HANG_HARD_KILL_SEC", tiny_timeout):
            watchdog = MLXWatchdog()
            # With tiny_timeout, the call must return quickly even on infinite hang
            # Патч обязан УЧИТЫВАТЬСЯ: дефолтные 10.0с не уложились бы в бюджет.
            deadline = NONBLOCKING_BUDGET_SEC
            t0 = time.monotonic()
            try:
                watchdog.run_with_timeout(
                    fn=_truly_stuck,
                    timeout_sec=_INFERENCE_TIMEOUT,
                )
            except MLXTimeoutError:
                pass
            elapsed = time.monotonic() - t0

        self.assertLess(
            elapsed,
            deadline,
            f"Patched MLX_HANG_HARD_KILL_SEC={tiny_timeout} not respected; "
            f"call took {elapsed:.3f}s > {deadline:.3f}s",
        )


if __name__ == "__main__":
    unittest.main()
