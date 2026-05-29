"""Unit tests for core.mlx_inter_lock — POSIX flock inter-process MLX lock.

Тест 1: lock acquire/release через tempfile (не /tmp — используем NamedTemporaryFile).
Тест 2: feature flag OFF → no-op context manager (не открывает файл).
Тест 3: timeout behaviour — W1636 fix:
    - default: raises MLXInterLockTimeout (safe contract).
    - degrade_on_timeout=True: logs warning, proceeds, acquired=False (opt-in).
Тест 4: acquired property — True on normal acquisition, False after degraded timeout.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Ensure core/ is importable
_TESTS_DIR = Path(__file__).parent
_PROJECT_ROOT = _TESTS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.mlx_inter_lock import (
    InterProcessMLXLock,
    MLXInterLockTimeout,
    _FEATURE_FLAG_ENV,
    _NoOpContext,
    mlx_inter_process_lock,
    is_inter_process_lock_enabled,
)


class TestInterProcessMLXLockAcquireRelease(unittest.TestCase):
    """Тест 1: базовый acquire / release через NamedTemporaryFile."""

    def test_acquire_and_release_succeed(self):
        """Lock должен успешно захватываться и освобождаться."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        try:
            lock = InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0)
            # Должен войти без ошибок
            with lock:
                self.assertIsNotNone(lock._fd, "fd должен быть открыт внутри with-блока")
            # После выхода fd должен быть освобождён
            self.assertIsNone(lock._fd, "fd должен быть None после __exit__")
        finally:
            lock_path.unlink(missing_ok=True)

    def test_lock_file_is_created_if_missing(self):
        """Lock должен создавать файл если он не существует."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "mlx_inter_process.lock"
            self.assertFalse(lock_path.exists(), "файл не должен существовать до first use")

            with InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0):
                self.assertTrue(lock_path.exists(), "lock файл должен быть создан")

    def test_sequential_acquires_succeed(self):
        """Последовательные acquire (без overlap) должны оба успешно завершаться."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        try:
            for i in range(2):
                with InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0):
                    pass  # must not raise
        finally:
            lock_path.unlink(missing_ok=True)


class TestFeatureFlagNoOp(unittest.TestCase):
    """Тест 2: feature flag OFF → no-op, feature flag ON → реальный lock."""

    def setUp(self):
        # Очищаем флаг перед каждым тестом
        os.environ.pop(_FEATURE_FLAG_ENV, None)

    def tearDown(self):
        os.environ.pop(_FEATURE_FLAG_ENV, None)

    def test_flag_off_returns_noop(self):
        """Без флага mlx_inter_process_lock() должен вернуть _NoOpContext."""
        result = mlx_inter_process_lock()
        self.assertIsInstance(
            result, _NoOpContext,
            "Без флага должен вернуть _NoOpContext (no-op), не InterProcessMLXLock"
        )

    def test_flag_off_noop_is_safe_context_manager(self):
        """No-op context manager должен входить/выходить без ошибок."""
        ctx = mlx_inter_process_lock()
        with ctx:
            pass  # must not raise

    def test_flag_on_returns_real_lock(self):
        """При KRAB_EAR_MLX_INTER_PROCESS_LOCK=1 должен вернуть InterProcessMLXLock."""
        os.environ[_FEATURE_FLAG_ENV] = "1"
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)
        try:
            result = mlx_inter_process_lock(lock_path=lock_path)
            self.assertIsInstance(
                result, InterProcessMLXLock,
                "При флаге=1 должен вернуть InterProcessMLXLock"
            )
        finally:
            lock_path.unlink(missing_ok=True)

    def test_is_enabled_helper(self):
        """is_inter_process_lock_enabled() должен корректно отражать env var."""
        self.assertFalse(is_inter_process_lock_enabled(), "без флага — False")
        os.environ[_FEATURE_FLAG_ENV] = "1"
        self.assertTrue(is_inter_process_lock_enabled(), "с флагом=1 — True")
        os.environ[_FEATURE_FLAG_ENV] = "0"
        self.assertFalse(is_inter_process_lock_enabled(), "с флагом=0 — False")


def _make_holder_thread(lock_path: Path):
    """Helper: returns (thread, acquired_event, release_event) that holds flock on lock_path."""
    import fcntl as _fcntl

    acquired_event = threading.Event()
    release_event = threading.Event()

    def hold_lock():
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            acquired_event.set()
            release_event.wait(timeout=5.0)
            _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)

    thread = threading.Thread(target=hold_lock, daemon=True)
    return thread, acquired_event, release_event


class TestTimeoutRaises(unittest.TestCase):
    """Тест 3 (W1636): default behaviour — raises MLXInterLockTimeout on timeout.

    Closes W1630 F2 HIGH: previously the timeout path silently continued without
    the lock, meaning ALL contenders ran MLX unguarded — exactly the GPU-corruption
    SIGSEGV the lock was designed to prevent.
    """

    def test_flock_timeout_raises_mlx_inter_lock_timeout_by_default(self):
        """W1636: default timeout MUST raise MLXInterLockTimeout, not silently continue."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            with self.assertRaises(MLXInterLockTimeout) as ctx:
                with InterProcessMLXLock(
                    lock_path=lock_path,
                    timeout_sec=0.12,
                    retry_interval_sec=0.02,
                    # degrade_on_timeout=False  (default)
                ):
                    pass  # should never be reached

            exc = ctx.exception
            self.assertAlmostEqual(exc.timeout_sec, 0.12, places=5)
            self.assertEqual(exc.lock_path, lock_path)
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)

    def test_flock_timeout_with_degrade_logs_critical_and_proceeds(self):
        """W1636 opt-in: degrade_on_timeout=True must NOT raise, must log warning,
        and the with-block must execute (acquired=False)."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        block_executed = []
        try:
            start = time.monotonic()
            lock = InterProcessMLXLock(
                lock_path=lock_path,
                timeout_sec=0.12,
                retry_interval_sec=0.02,
                degrade_on_timeout=True,
            )
            with lock:
                block_executed.append(True)
                self.assertFalse(
                    lock.acquired,
                    "acquired must be False inside a degraded-timeout with-block",
                )
            elapsed = time.monotonic() - start

            self.assertTrue(block_executed, "with-block must execute in degrade mode")
            self.assertGreaterEqual(elapsed, 0.08, "должен ждать хотя бы ~timeout_sec")
            self.assertLess(elapsed, 1.0, "не должен ждать намного дольше timeout_sec")
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)

    def test_timeout_raises_closes_fd_cleanly(self):
        """W1636: after MLXInterLockTimeout is raised, fd must be closed (no leak)."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            lock = InterProcessMLXLock(
                lock_path=lock_path, timeout_sec=0.10, retry_interval_sec=0.02
            )
            with self.assertRaises(MLXInterLockTimeout):
                lock.__enter__()
            # fd must be None after the exception — no leaking fd
            self.assertIsNone(lock._fd, "fd must be closed after MLXInterLockTimeout")
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)


class TestAcquiredProperty(unittest.TestCase):
    """Тест 4: acquired property — True on normal acquisition, False after degraded timeout."""

    def test_acquired_property_true_on_normal_acquisition(self):
        """acquired must be True inside a with-block when flock was obtained."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            lock = InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0)

            self.assertFalse(lock.acquired, "acquired must be False before __enter__")
            with lock:
                self.assertTrue(lock.acquired, "acquired must be True inside with-block")
            # After exit, fd is closed but acquired stays True (acquisition did happen)
            self.assertTrue(lock.acquired, "acquired stays True after __exit__")

    def test_acquired_property_false_after_degraded_timeout(self):
        """acquired must be False when degrade_on_timeout=True and flock timed out."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            lock = InterProcessMLXLock(
                lock_path=lock_path,
                timeout_sec=0.10,
                retry_interval_sec=0.02,
                degrade_on_timeout=True,
            )
            self.assertFalse(lock.acquired, "acquired must be False before __enter__")
            with lock:
                self.assertFalse(lock.acquired, "acquired must be False in degraded with-block")
            self.assertFalse(lock.acquired, "acquired stays False after __exit__ in degrade mode")
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)

    def test_acquired_property_false_before_enter(self):
        """acquired must always be False before entering the context manager."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "never_entered.lock"
            lock = InterProcessMLXLock(lock_path=lock_path)
            self.assertFalse(lock.acquired)

    def test_acquired_resets_to_false_on_new_instance(self):
        """Each InterProcessMLXLock instance starts with acquired=False."""
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "test.lock"
            lock1 = InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0)
            with lock1:
                self.assertTrue(lock1.acquired)

            # A brand-new instance on same path starts fresh
            lock2 = InterProcessMLXLock(lock_path=lock_path, timeout_sec=2.0)
            self.assertFalse(lock2.acquired, "new instance must start with acquired=False")
            with lock2:
                self.assertTrue(lock2.acquired)


class TestTimeoutDegradationBackwardCompat(unittest.TestCase):
    """Backward-compat checks: degrade mode still works for opt-in paths."""

    def test_degrade_mode_with_block_executes_fully(self):
        """Full with-block runs in degrade mode even after timeout."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        results = []
        try:
            with InterProcessMLXLock(
                lock_path=lock_path,
                timeout_sec=0.10,
                retry_interval_sec=0.02,
                degrade_on_timeout=True,
            ):
                results.append("body_ran")
            self.assertEqual(results, ["body_ran"])
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)

    def test_degrade_exit_does_not_raise(self):
        """__exit__ in degrade mode must not raise even though lock was not held."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        holder, acquired_event, release_event = _make_holder_thread(lock_path)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            lock = InterProcessMLXLock(
                lock_path=lock_path,
                timeout_sec=0.10,
                retry_interval_sec=0.02,
                degrade_on_timeout=True,
            )
            lock.__enter__()
            # __exit__ must not raise — no lock was held, LOCK_UN on unowned fd is benign
            lock.__exit__(None, None, None)
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)


class TestMLXInterLockTimeoutException(unittest.TestCase):
    """Tests for the MLXInterLockTimeout exception class."""

    def test_exception_attributes(self):
        """MLXInterLockTimeout must expose timeout_sec and lock_path attributes."""
        path = Path("/tmp/test.lock")
        exc = MLXInterLockTimeout(timeout_sec=3.7, lock_path=path)
        self.assertEqual(exc.timeout_sec, 3.7)
        self.assertEqual(exc.lock_path, path)

    def test_exception_message_contains_timeout_and_path(self):
        """Exception str must mention the timeout value and lock path."""
        path = Path("/some/path/mlx.lock")
        exc = MLXInterLockTimeout(timeout_sec=5.0, lock_path=path)
        msg = str(exc)
        self.assertIn("5.0", msg)
        self.assertIn(str(path), msg)

    def test_exception_is_exception_subclass(self):
        """MLXInterLockTimeout must be a proper Exception subclass."""
        exc = MLXInterLockTimeout(timeout_sec=1.0, lock_path=Path("/tmp/x"))
        self.assertIsInstance(exc, Exception)


if __name__ == "__main__":
    unittest.main()
