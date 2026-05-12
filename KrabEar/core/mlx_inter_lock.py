"""Inter-process MLX serialization via POSIX flock.

Дополняет intra-process mlx_lock (RLock в core/mlx_lock.py) кросс-процессной
координацией для сценариев, где несколько OS-процессов Krab Ear одновременно
используют MLX GPU (например, будущие subprocess-воркеры GigaAM, mlx_subprocess.py).

ВАЖНО: LM Studio (внешний процесс) не может быть скоординирован через этот lock —
он не знает про наш lockfile. Основная польза: Krab Ear-side self-serialization
поверх нескольких процессов + foundation для будущей координации.

Feature flag: KRAB_EAR_MLX_INTER_PROCESS_LOCK=1 (по умолчанию OFF).
По умолчанию mlx_inter_process_lock() возвращает no-op context manager —
нулевой overhead в production до Wave 49 enabling.

Паттерн использования (Wave 49 wire-in):
    from core.mlx_inter_lock import mlx_inter_process_lock
    from core.mlx_lock import mlx_lock

    with mlx_inter_process_lock():   # outer: cross-process
        with mlx_lock():             # inner: intra-process thread
            result = mlx_whisper.transcribe(audio, ...)
"""
from __future__ import annotations

import fcntl
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger("KrabEar.Core.MLXInterLock")

# Default lock file location — same directory as krabear.sock
_DEFAULT_LOCK_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "mlx_inter_process.lock"
)

# Feature flag env var name
_FEATURE_FLAG_ENV = "KRAB_EAR_MLX_INTER_PROCESS_LOCK"


class _NoOpContext:
    """No-op context manager — zero overhead когда feature flag OFF."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


_NOOP = _NoOpContext()


class InterProcessMLXLock:
    """POSIX flock-based межпроцессная блокировка для MLX GPU операций.

    Использует fcntl.flock(LOCK_EX) с non-blocking retry loop + timeout.
    При истечении timeout деградирует gracefully (логирует warning, НЕ блокирует STT).

    Thread safety: один fd на объект — не использовать один экземпляр из нескольких тредов.
    Для multi-thread создавать отдельный экземпляр на тред или использовать mlx_inter_process_lock().
    """

    def __init__(
        self,
        lock_path: Optional[Path] = None,
        timeout_sec: float = 5.0,
        retry_interval_sec: float = 0.05,
    ):
        self._lock_path = lock_path or _DEFAULT_LOCK_PATH
        self._timeout_sec = timeout_sec
        self._retry_interval_sec = retry_interval_sec
        self._fd: Optional[int] = None

    def _open_lock_file(self) -> int:
        """Открывает lock file (создаёт если нет). Возвращает fd."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        return fd

    def __enter__(self) -> "InterProcessMLXLock":
        self._fd = self._open_lock_file()
        deadline = time.monotonic() + self._timeout_sec
        acquired = False
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    # Graceful degradation: log and proceed without lock
                    # STT must never be permanently blocked by a stale lock.
                    logger.warning(
                        "mlx_inter_lock: flock timeout after %.1fs — proceeding without lock "
                        "(stale holder or high contention). lock_path=%s",
                        self._timeout_sec,
                        self._lock_path,
                    )
                    break
                time.sleep(self._retry_interval_sec)
        if acquired:
            logger.debug("mlx_inter_lock: acquired, lock_path=%s", self._lock_path)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                logger.debug("mlx_inter_lock: released, lock_path=%s", self._lock_path)
            except OSError as e:
                logger.warning("mlx_inter_lock: flock LOCK_UN failed: %s", e)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        return False  # do not suppress exceptions


def mlx_inter_process_lock(
    lock_path: Optional[Path] = None,
    timeout_sec: float = 5.0,
) -> "InterProcessMLXLock | _NoOpContext":
    """Возвращает InterProcessMLXLock если KRAB_EAR_MLX_INTER_PROCESS_LOCK=1, иначе no-op.

    Это основная точка входа для call-site'ов в engine.py (Wave 49).

    Args:
        lock_path: путь к lock-файлу. None → default (~/.../KrabEar/mlx_inter_process.lock)
        timeout_sec: максимальное ожидание acquire в секундах. При истечении — graceful continue.

    Returns:
        Context manager: либо InterProcessMLXLock (active), либо _NoOpContext (no-op).
    """
    if os.environ.get(_FEATURE_FLAG_ENV) == "1":
        return InterProcessMLXLock(lock_path=lock_path, timeout_sec=timeout_sec)
    return _NOOP


def is_inter_process_lock_enabled() -> bool:
    """Вернуть True если feature flag KRAB_EAR_MLX_INTER_PROCESS_LOCK=1."""
    return os.environ.get(_FEATURE_FLAG_ENV) == "1"
