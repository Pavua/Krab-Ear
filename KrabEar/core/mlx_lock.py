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
"""
import threading

_mlx_lock = threading.RLock()


def mlx_lock() -> threading.RLock:
    """Вернуть глобальную блокировку сериализации MLX (использовать как context manager)."""
    return _mlx_lock
