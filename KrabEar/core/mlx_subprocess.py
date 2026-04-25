"""MLX inference watchdog с auto-recovery.

Проблема: mlx_whisper.transcribe() может зависнуть или вызвать SIGSEGV, когда
Metal GPU застрял. В этом случае весь бэкенд падает и нужен ручной рестарт.

Решение: threading watchdog с таймаутом. Каждый вызов mlx_whisper.transcribe()
выполняется в отдельном daemon-потоке. Если поток не завершается за
MLX_TRANSCRIBE_TIMEOUT_SEC — watchdog логирует событие, репортит в Sentry,
и сигнализирует вызывающей стороне об ошибке через исключение MLXTimeoutError.
Вызывающая сторона (engine.py) перехватывает исключение и помечает MLX-модель
недоступной → fallback chain продолжается на CPU/другом адаптере.

Почему watchdog, а не multiprocessing subprocess?
- Subprocess изолирует SIGSEGV (процесс умирает тихо, main backend жив).
- Но: spawn overhead ~1-2s, IPC-сериализация numpy arrays дорогая,
  macOS SandBox/entitlements могут заблокировать fork для .app bundle,
  complexity значительно выше.
- Watchdog: zero overhead при нормальной работе, простой, тестируемый,
  достаточен для real-world случаев когда GPU "stuck" но не SIGSEGV.
- Если у вас реальный SIGSEGV — BackendSupervisor (Swift) перезапустит процесс.
  Watchdog покрывает "hung/stuck" сценарии которые встречаются чаще.

Трек статистики: crashes_count, total_calls, avg_response_time.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class MLXTimeoutError(RuntimeError):
    """Таймаут ожидания ответа от MLX inference thread."""

    def __init__(self, timeout_sec: float, model_name: str) -> None:
        self.timeout_sec = timeout_sec
        self.model_name = model_name
        super().__init__(
            f"MLX inference timed out after {timeout_sec}s "
            f"(model={model_name}). Metal GPU may be stuck."
        )


class MLXWatchdog:
    """Запускает mlx_whisper.transcribe() с таймаутом.

    При превышении таймаута выбрасывает MLXTimeoutError — caller'у нужно
    поймать и переключиться на fallback (CPU / другой адаптер).

    Поток-executor работает как daemon thread — он автоматически завершается
    при выходе из основного процесса. Если MLX завис — поток "потерян" (нет
    способа принудительно остановить Python thread), но новые вызовы будут
    создавать новые потоки → backend продолжает работать.

    Атрибуты статистики (thread-safe через lock):
        crashes_count:      количество зафиксированных таймаутов.
        total_calls:        общее число вызовов transcribe().
        avg_response_time:  скользящее среднее времени ответа (успешные вызовы).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.crashes_count: int = 0
        self.total_calls: int = 0
        self._total_response_time: float = 0.0
        self._success_count: int = 0

    @property
    def avg_response_time(self) -> float:
        """Среднее время успешного inference (секунды). 0.0 если не было вызовов."""
        with self._lock:
            if self._success_count == 0:
                return 0.0
            return self._total_response_time / self._success_count

    def get_stats(self) -> dict[str, Any]:
        """Возвращает snapshot статистики для диагностики."""
        with self._lock:
            return {
                "crashes_count": self.crashes_count,
                "total_calls": self.total_calls,
                "success_count": self._success_count,
                "avg_response_time_sec": (
                    self._total_response_time / self._success_count
                    if self._success_count > 0
                    else 0.0
                ),
            }

    def run_with_timeout(
        self,
        fn: Callable[[], Any],
        timeout_sec: float,
        model_name: str = "<unknown>",
    ) -> Any:
        """Запускает fn() в daemon-потоке, ждёт не дольше timeout_sec.

        Args:
            fn:          callable без аргументов; внутри должен вызвать mlx_whisper.
            timeout_sec: максимальное время ожидания (секунды).
            model_name:  имя модели для логов и Sentry.

        Returns:
            Результат fn() при успехе.

        Raises:
            MLXTimeoutError: если fn() не завершилась за timeout_sec.
            Exception:       если fn() бросила исключение (прокидывается as-is).
        """
        with self._lock:
            self.total_calls += 1

        result_queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        exc_queue: queue.Queue[BaseException] = queue.Queue(maxsize=1)
        start_time = time.monotonic()

        def _worker() -> None:
            try:
                result = fn()
                result_queue.put(result)
            except BaseException as exc:  # noqa: BLE001
                exc_queue.put(exc)

        thread = threading.Thread(target=_worker, daemon=True, name=f"mlx-watchdog-{model_name}")
        thread.start()
        thread.join(timeout=timeout_sec)

        elapsed = time.monotonic() - start_time

        if thread.is_alive():
            # Поток завис — таймаут
            with self._lock:
                self.crashes_count += 1
            logger.error(
                "[MLXWatchdog] inference timed out after %.1fs (model=%s, "
                "total_crashes=%d). Signalling fallback.",
                elapsed,
                model_name,
                self.crashes_count,
            )
            _notify_sentry_timeout(model_name, elapsed, self.crashes_count)
            raise MLXTimeoutError(timeout_sec=timeout_sec, model_name=model_name)

        # Поток завершился — проверяем результат
        if not exc_queue.empty():
            exc = exc_queue.get_nowait()
            raise exc  # type: ignore[misc]

        if result_queue.empty():
            # Нет ни результата, ни исключения — не должно случаться
            raise RuntimeError(
                f"MLXWatchdog: worker thread finished with no result (model={model_name})"
            )

        result = result_queue.get_nowait()
        with self._lock:
            self._success_count += 1
            self._total_response_time += elapsed
        return result


# ---------------------------------------------------------------------------
# Module-level singleton watchdog
# ---------------------------------------------------------------------------

_watchdog = MLXWatchdog()


def get_watchdog() -> MLXWatchdog:
    """Вернуть module-level singleton MLXWatchdog."""
    return _watchdog


# ---------------------------------------------------------------------------
# Sentry helper
# ---------------------------------------------------------------------------

def _notify_sentry_timeout(
    model_name: str,
    elapsed_sec: float,
    crash_count: int,
) -> None:
    """Отправить событие таймаута в Sentry (no-op если Sentry не инициализирован)."""
    try:
        from backend.observability import (  # noqa: PLC0415
            is_sentry_initialized,
            add_breadcrumb,
            capture_exception,
        )
        if not is_sentry_initialized():
            return
        add_breadcrumb(
            category="mlx",
            message="MLX inference timeout — Metal GPU may be stuck",
            level="error",
            data={
                "model": model_name,
                "elapsed_sec": round(elapsed_sec, 2),
                "crash_count": crash_count,
            },
        )
        capture_exception(
            MLXTimeoutError(timeout_sec=elapsed_sec, model_name=model_name),
            component="mlx_watchdog",
        )
    except Exception:  # noqa: BLE001
        pass  # telemetry никогда не должна ломать основной поток
