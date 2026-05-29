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
    from core.mlx_inter_lock import mlx_inter_process_lock, MLXInterLockTimeout
    from core.mlx_lock import mlx_lock

    try:
        with mlx_inter_process_lock():   # outer: cross-process (raises on timeout by default)
            with mlx_lock():             # inner: intra-process thread
                result = mlx_whisper.transcribe(audio, ...)
    except MLXInterLockTimeout:
        logger.error("mlx_inter_lock: could not acquire cross-process lock — GPU contention")
        raise

Timeout policy (W1636 fix — W1630 F2 HIGH silent TOCTOU):
    По умолчанию __enter__ RAISES MLXInterLockTimeout при истечении timeout.
    Это безопаснее старого silent-degrade поведения, при котором все contenders
    после timeout молча выполняли MLX-секцию без защиты — точно тот GPU-corruption
    SIGSEGV, от которого lock должен был защищать.

    Для opt-in деградации без exception (backward-compat или non-critical paths):
        InterProcessMLXLock(degrade_on_timeout=True)
    В режиме degrade: acquired=False, WARNING в лог, CRITICAL в error_bus,
    with-блок всё равно выполняется — но caller знает через lock.acquired.
"""
from __future__ import annotations

import fcntl
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("KrabEar.Core.MLXInterLock")

# Default lock file location — same directory as krabear.sock
_DEFAULT_LOCK_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "mlx_inter_process.lock"
)

# Feature flag env var name
_FEATURE_FLAG_ENV = "KRAB_EAR_MLX_INTER_PROCESS_LOCK"


class MLXInterLockTimeout(Exception):
    """Raised by InterProcessMLXLock.__enter__ when flock cannot be acquired within timeout.

    W1636 fix (W1630 F2 HIGH): previously, timeout silently allowed unguarded MLX
    execution — exactly the GPU-corruption SIGSEGV the lock was designed to prevent.
    Now the default contract is to raise so callers can decide how to handle contention.

    Attributes:
        timeout_sec: the timeout value that was exceeded.
        lock_path: path to the lock file.
    """

    def __init__(self, timeout_sec: float, lock_path: Path) -> None:
        self.timeout_sec = timeout_sec
        self.lock_path = lock_path
        super().__init__(
            f"mlx_inter_lock: flock not acquired after {timeout_sec:.1f}s "
            f"(stale holder or high contention). lock_path={lock_path}"
        )


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

    Timeout policy (W1636 — W1630 F2 HIGH silent TOCTOU fix):
        По умолчанию (degrade_on_timeout=False): при истечении timeout __enter__
        raises MLXInterLockTimeout. Это безопасный default — caller явно решает,
        как обработать отказ (retry, fallback to intra-lock-only, fail loudly).

        При degrade_on_timeout=True: устанавливает self._acquired=False,
        логирует SEVERE WARNING + пушит CRITICAL в error_bus (если доступен),
        and allows the with-block to run (старое поведение). Используй только
        для non-critical paths где GPU-corruption менее вероятен, чем stale lock.

    acquired property: True если lock был успешно захвачен, False если timeout+degrade.

    Thread safety: один fd на объект — не использовать один экземпляр из нескольких тредов.
    Для multi-thread создавать отдельный экземпляр на тред или использовать mlx_inter_process_lock().
    """

    def __init__(
        self,
        lock_path: Optional[Path] = None,
        timeout_sec: float = 5.0,
        retry_interval_sec: float = 0.05,
        degrade_on_timeout: bool = False,
    ):
        self._lock_path = lock_path or _DEFAULT_LOCK_PATH
        self._timeout_sec = timeout_sec
        self._retry_interval_sec = retry_interval_sec
        self._degrade_on_timeout = degrade_on_timeout
        self._fd: Optional[int] = None
        self._acquired: bool = False

    @property
    def acquired(self) -> bool:
        """True if the flock was successfully acquired; False after a degraded timeout.

        Only meaningful inside a with-block (or after __enter__ when degrade_on_timeout=True).
        Always False before __enter__ is called.
        """
        return self._acquired

    def _open_lock_file(self) -> int:
        """Открывает lock file (создаёт если нет). Возвращает fd."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o666)
        return fd

    def _push_critical_to_error_bus(self) -> None:
        """Push a CRITICAL error event to error_bus if available (best-effort)."""
        try:
            from backend.error_bus import get_error_bus  # type: ignore[import]
            from backend.error_codes import ERROR_REGISTRY  # type: ignore[import]

            bus = get_error_bus()
            if bus is not None:
                code = "mlx.inter_lock_timeout"
                meta = ERROR_REGISTRY.get(code, {})
                bus.push(
                    code=code,
                    severity=meta.get("severity", "critical"),
                    message=(
                        f"mlx_inter_lock: flock not acquired after {self._timeout_sec:.1f}s — "
                        f"MLX GPU section running unguarded (TOCTOU risk). "
                        f"lock_path={self._lock_path}"
                    ),
                    extra={"timeout_sec": self._timeout_sec, "lock_path": str(self._lock_path)},
                )
        except Exception:
            # error_bus may not be available in all contexts (tests, subprocess workers)
            pass

    def __enter__(self) -> "InterProcessMLXLock":
        self._acquired = False
        self._fd = self._open_lock_file()
        deadline = time.monotonic() + self._timeout_sec
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    if self._degrade_on_timeout:
                        # Degrade mode: warn loudly, push to error_bus, allow with-block.
                        # acquired remains False — caller can check lock.acquired.
                        logger.warning(
                            "mlx_inter_lock: flock timeout after %.1fs — proceeding WITHOUT lock "
                            "(degrade_on_timeout=True). GPU corruption risk active. "
                            "lock_path=%s",
                            self._timeout_sec,
                            self._lock_path,
                        )
                        self._push_critical_to_error_bus()
                        break
                    else:
                        # Default (safe): close fd and raise so caller handles contention.
                        try:
                            os.close(self._fd)
                        except OSError:
                            pass
                        finally:
                            self._fd = None
                        raise MLXInterLockTimeout(
                            timeout_sec=self._timeout_sec,
                            lock_path=self._lock_path,
                        )
                time.sleep(self._retry_interval_sec)
        if self._acquired:
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
    degrade_on_timeout: bool = False,
) -> "InterProcessMLXLock | _NoOpContext":
    """Возвращает InterProcessMLXLock если KRAB_EAR_MLX_INTER_PROCESS_LOCK=1, иначе no-op.

    Это основная точка входа для call-site'ов (Wave 49 wire-in).

    Timeout policy (W1636): по умолчанию raises MLXInterLockTimeout при истечении timeout.
    Передай degrade_on_timeout=True для opt-in silent-degrade (старое поведение) на
    non-critical paths — acquired property будет False, error_bus получит CRITICAL.

    Args:
        lock_path: путь к lock-файлу. None → default (~/.../KrabEar/mlx_inter_process.lock)
        timeout_sec: максимальное ожидание acquire в секундах.
        degrade_on_timeout: если True — при timeout продолжает без lock (acquired=False);
            если False (default) — raises MLXInterLockTimeout.

    Returns:
        Context manager: либо InterProcessMLXLock (active), либо _NoOpContext (no-op).

    Raises:
        MLXInterLockTimeout: если lock не захвачен за timeout_sec и degrade_on_timeout=False.
    """
    if os.environ.get(_FEATURE_FLAG_ENV) == "1":
        return InterProcessMLXLock(
            lock_path=lock_path,
            timeout_sec=timeout_sec,
            degrade_on_timeout=degrade_on_timeout,
        )
    return _NOOP


def is_inter_process_lock_enabled() -> bool:
    """Вернуть True если feature flag KRAB_EAR_MLX_INTER_PROCESS_LOCK=1."""
    return os.environ.get(_FEATURE_FLAG_ENV) == "1"
