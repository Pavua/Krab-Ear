"""Unit tests for core.mlx_inter_lock — POSIX flock inter-process MLX lock.

Тест 1: lock acquire/release через tempfile (не /tmp — используем NamedTemporaryFile).
Тест 2: feature flag OFF → no-op context manager (не открывает файл).
Тест 3: timeout degradation — если flock заблокирован, деградирует gracefully (не raises).
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


class TestTimeoutDegradation(unittest.TestCase):
    """Тест 3: timeout → graceful degradation (не raises, только warning)."""

    def test_timeout_does_not_raise(self):
        """При блокировке lock должен дождаться timeout и продолжить без exception."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        acquired_event = threading.Event()
        release_event = threading.Event()

        def hold_lock():
            """Держит flock на lock_path пока не получит release_event."""
            import fcntl
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                acquired_event.set()
                release_event.wait(timeout=5.0)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            # Пробуем захватить с коротким timeout — должен degradate gracefully
            start = time.monotonic()
            with InterProcessMLXLock(lock_path=lock_path, timeout_sec=0.15, retry_interval_sec=0.02):
                pass  # must NOT raise even though lock is held by another thread
            elapsed = time.monotonic() - start

            # Проверяем что timeout примерно соблюдён (0.15s ± 0.1s tolerance)
            self.assertGreaterEqual(elapsed, 0.10, "должен ждать хотя бы ~timeout_sec")
            self.assertLess(elapsed, 1.0, "не должен ждать намного дольше timeout_sec")
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)

    def test_lock_usable_after_timeout_degradation(self):
        """После graceful timeout degradation объект должен корректно выйти из __exit__."""
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            lock_path = Path(tf.name)

        acquired_event = threading.Event()
        release_event = threading.Event()

        def hold_lock():
            import fcntl
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                acquired_event.set()
                release_event.wait(timeout=5.0)
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()
        acquired_event.wait(timeout=2.0)

        try:
            lock = InterProcessMLXLock(lock_path=lock_path, timeout_sec=0.1, retry_interval_sec=0.02)
            # Запускаем __enter__ (будет timeout), потом __exit__ — не должен raises
            lock.__enter__()
            lock.__exit__(None, None, None)  # must not raise
        finally:
            release_event.set()
            holder.join(timeout=2.0)
            lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
