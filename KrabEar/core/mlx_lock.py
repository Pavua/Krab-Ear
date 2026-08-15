"""Глобальная блокировка сериализации для MLX операций.

MLX официально не является thread-safe. Конкурентный доступ к внутренней
hash-таблице MTL::Resource* вызывает повреждение состояния GPU и SIGSEGV:

    Thread ThreadPoolExecutor-14_0 (EXC_BAD_ACCESS KERN_INVALID_ADDRESS):
      libmlx.dylib  __hash_table<MTL::Resource*>::__emplace_unique_key_args+28
      libmlx.dylib  mlx::core::concatenate_gpu+1464
      libmlx.dylib  mlx::core::gpu::eval+204

Все MLX inference вызовы — mlx-whisper и любые будущие MLX-адаптеры —
обязаны захватывать эту блокировку перед отправкой работы на GPU.

Компромисс: несколько одновременных транскрибаций теперь выполняются
последовательно. Для однопользовательского десктоп-приложения это приемлемо.

Используем RLock (reentrant) на случай вложенных MLX вызовов в fallback chain.

Межпроцессная координация (Phase C Step 6):
Дополнительная POSIX flock-блокировка в core/mlx_inter_lock.py для координации
между несколькими OS-процессами Krab Ear. Включается через KRAB_EAR_MLX_INTER_PROCESS_LOCK=1.
Паттерн wire-in (Wave 49):
    with mlx_inter_process_lock():  # outer: cross-process flock
        with mlx_lock():            # inner: intra-process RLock
            mlx_whisper.transcribe(...)
"""
import contextlib
import threading
from typing import Iterator

_mlx_lock = threading.RLock()


def mlx_lock() -> threading.RLock:
    """Вернуть глобальную блокировку сериализации MLX (использовать как context manager)."""
    return _mlx_lock


class MLXLockTimeoutError(TimeoutError):
    """Исключение при невозможности захватить intra-process mlx_lock за таймаут."""


@contextlib.contextmanager
def acquire_mlx_lock(timeout_sec: float | None = None) -> Iterator[bool]:
    """Безопасный захват mlx_lock с опциональным таймаутом."""
    lock = mlx_lock()
    if timeout_sec is None or timeout_sec < 0:
        with lock:
            yield True
        return

    acquired = lock.acquire(timeout=timeout_sec)
    if not acquired:
        raise MLXLockTimeoutError(
            f"Не удалось захватить intra-process mlx_lock за {timeout_sec:.2f}с (GPU/STT занят)"
        )
    try:
        yield True
    finally:
        lock.release()


# Re-export inter-process lock helper for convenient single-import call-sites.
# Wave 49 wire-in: from core.mlx_lock import mlx_lock, mlx_inter_process_lock
from core.mlx_inter_lock import mlx_inter_process_lock  # noqa: E402,F401 — re-export

