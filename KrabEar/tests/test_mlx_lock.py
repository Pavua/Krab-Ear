"""Unit tests for core.mlx_lock — global RLock MLX serializer.

Wave 175: первый coverage для mlx_lock.py (intra-process RLock) и
  re-export mlx_inter_process_lock.

Design constraints:
  - NEVER import real mlx / mlx_whisper (not installed in test env).
  - All tests are deterministic и не зависят от Metal GPU.
"""
import sys
import threading
import time
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.mlx_lock import mlx_lock, mlx_inter_process_lock  # noqa: E402


class TestMlxLockBasic(unittest.TestCase):
    """mlx_lock() returns a working context manager (RLock)."""

    def test_returns_rlock(self):
        lock = mlx_lock()
        self.assertIsInstance(lock, type(threading.RLock()))

    def test_acquired_in_with_statement(self):
        lock = mlx_lock()
        entered = []
        with lock:
            # If we could enter the block the lock was acquired.
            entered.append(True)
        self.assertEqual(entered, [True])

    def test_module_level_singleton(self):
        """Two calls to mlx_lock() must return the exact same object."""
        a = mlx_lock()
        b = mlx_lock()
        self.assertIs(a, b)

    def test_lock_is_reentrant_rlock(self):
        """RLock: same thread can acquire the lock multiple times without deadlock."""
        lock = mlx_lock()
        with lock:
            with lock:
                with lock:
                    pass  # No deadlock = RLock confirmed

    def test_reentrant_three_levels(self):
        """Deeply nested re-entrant acquire succeeds for realistic fallback chains."""
        lock = mlx_lock()
        depths = []
        with lock:
            depths.append(1)
            with lock:
                depths.append(2)
                with lock:
                    depths.append(3)
        self.assertEqual(depths, [1, 2, 3])

    def test_lock_released_after_exception(self):
        """Lock must be released even when an exception occurs inside the with-block."""
        lock = mlx_lock()
        try:
            with lock:
                raise ValueError("test error")
        except ValueError:
            pass
        # After exception the lock should be acquirable again from a different thread.
        result = []

        def _try_acquire():
            acquired = lock.acquire(blocking=False)
            result.append(acquired)
            if acquired:
                lock.release()

        t = threading.Thread(target=_try_acquire)
        t.start()
        t.join(timeout=2.0)
        self.assertEqual(result, [True], "Lock was not released after exception")

    def test_context_manager_protocol(self):
        """__enter__ and __exit__ are callable on the returned lock."""
        lock = mlx_lock()
        self.assertTrue(hasattr(lock, "__enter__"))
        self.assertTrue(hasattr(lock, "__exit__"))


class TestMlxLockConcurrency(unittest.TestCase):
    """Concurrent threads serialise correctly through mlx_lock()."""

    def test_concurrent_threads_serialize(self):
        """Two threads must not overlap inside the critical section."""
        lock = mlx_lock()
        inside = []
        errors = []

        def _worker(idx: int):
            with lock:
                inside.append(idx)
                # Simulate work; verify nobody else entered simultaneously.
                if len(inside) > 1:
                    errors.append(f"overlap: inside={list(inside)}")
                time.sleep(0.01)
                inside.pop()

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Serialisation violated: {errors}")

    def test_second_thread_blocked_until_release(self):
        """Thread 2 must wait while Thread 1 holds the lock."""
        lock = mlx_lock()
        timeline = []

        def _thread1():
            with lock:
                timeline.append("t1_enter")
                time.sleep(0.05)
                timeline.append("t1_exit")

        def _thread2():
            # Give T1 time to enter first.
            time.sleep(0.01)
            with lock:
                timeline.append("t2_enter")

        t1 = threading.Thread(target=_thread1)
        t2 = threading.Thread(target=_thread2)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        # t2_enter must come AFTER t1_exit
        self.assertIn("t1_enter", timeline)
        self.assertIn("t1_exit", timeline)
        self.assertIn("t2_enter", timeline)
        idx_exit = timeline.index("t1_exit")
        idx_t2 = timeline.index("t2_enter")
        self.assertGreater(idx_t2, idx_exit, f"t2 entered before t1 released: {timeline}")


class TestMlxInterProcessLockReExport(unittest.TestCase):
    """mlx_inter_process_lock re-export from core.mlx_lock works correctly."""

    def test_reexport_callable(self):
        """mlx_inter_process_lock imported from core.mlx_lock is callable."""
        self.assertTrue(callable(mlx_inter_process_lock))

    def test_reexport_noop_when_flag_off(self):
        """When feature flag is OFF the re-exported helper returns a no-op ctx."""
        import os
        original = os.environ.pop("KRAB_EAR_MLX_INTER_PROCESS_LOCK", None)
        try:
            ctx = mlx_inter_process_lock()
            # No-op context manager: __enter__/__exit__ callable and no exception
            with ctx:
                pass
        finally:
            if original is not None:
                os.environ["KRAB_EAR_MLX_INTER_PROCESS_LOCK"] = original


if __name__ == "__main__":
    unittest.main()
