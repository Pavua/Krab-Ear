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
import os
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard-kill timeout for the unbounded-join guard (wave-20 MED fix).
# ---------------------------------------------------------------------------
# After the initial timed join() reveals a hung thread we wait at most this
# many seconds for the daemon to finish before giving up and propagating
# MLXTimeoutError anyway.  This bounds the worst-case stall to a finite
# window instead of holding the backend indefinitely.
#
# Tradeoff: if the daemon thread hasn't exited by the hard-kill deadline, the
# mlx_lock held by the caller IS released while the daemon may still be
# touching the MLX GPU.  That reintroduces a narrow SIGSEGV race window, but
# it's strictly better than an infinite backend stall — BackendSupervisor's
# HealthMonitor would not be able to distinguish a hung watchdog from a hung
# backend and would SIGTERM/SIGKILL the entire process anyway after its own
# 2-fail-ping timeout.  So bounding the join to 120 s caps the worst case.
#
# Override via env: KRAB_EAR_MLX_HANG_HARD_KILL_SEC=<float>
# KRAB-EAR-BACKEND-1V: дефолт 10с (вместо прежних 120с) предотвращает достижение 180с IPC backstop.
MLX_HANG_HARD_KILL_SEC: float = float(
    os.environ.get("KRAB_EAR_MLX_HANG_HARD_KILL_SEC", "10.0")
)


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

        THREAD-SAFETY NOTE (W1358 F1 MED — sister to 2026-04-19 SIGSEGV, PR #71):
        run_with_timeout() is always called from inside ``with mlx_lock():`` in
        engine.py.  The daemon thread executes fn() WITHOUT holding that lock —
        only the caller's thread holds it.  If we raise MLXTimeoutError while the
        daemon thread is still running mlx_whisper.transcribe(), the caller exits
        the ``with mlx_lock()`` block, releasing the lock.  The still-running
        daemon thread then accesses the MLX GPU concurrently with the next caller
        that acquires the lock → SIGSEGV (same class of bug as PR #71).

        Fix (W1358 race-guard): after the initial timed join reveals a live thread,
        perform a *bounded* join(timeout=MLX_HANG_HARD_KILL_SEC) — wait up to
        MLX_HANG_HARD_KILL_SEC seconds for the daemon to finish — BEFORE raising
        MLXTimeoutError.  This keeps the mlx_lock held as long as the daemon is
        still running (up to the hard-kill bound), eliminating the race for
        normally-recovering hangs.  The trade-off on a truly stuck GPU:

        - If the daemon exits within MLX_HANG_HARD_KILL_SEC: full race safety,
          MLXTimeoutError is then propagated cleanly.
        - If the daemon is STILL alive after MLX_HANG_HARD_KILL_SEC (wave-20 MED fix):
          we give up waiting, log an error, and propagate MLXTimeoutError.  The
          daemon may still be touching the MLX GPU without the lock held — narrow
          SIGSEGV race window.  However, this is strictly better than an infinite
          backend stall: BackendSupervisor's HealthMonitor would SIGTERM/SIGKILL
          the process anyway after its own ping-failure timeout.

        Override the hard-kill window via env: KRAB_EAR_MLX_HANG_HARD_KILL_SEC=<sec>.
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
            # Поток завис — таймаут истёк, но поток ещё держит MLX GPU.
            # КРИТИЧНО: НЕ выходим немедленно.  Делаем unbounded join() чтобы
            # удержать mlx_lock (захваченный caller'ом) до завершения потока.
            # Это предотвращает SIGSEGV от concurrent GPU access (W1358 F1 MED).
            with self._lock:
                self.crashes_count += 1
            logger.error(
                "[MLXWatchdog] inference timed out after %.1fs (model=%s, "
                "total_crashes=%d). Waiting for daemon thread to finish "
                "before releasing MLX lock (W1358 race-guard).",
                elapsed,
                model_name,
                self.crashes_count,
            )
            _notify_sentry_timeout(model_name, elapsed, self.crashes_count)
            _push_watchdog_hang(model_name, elapsed, self.crashes_count)
            # Bounded wait: держим caller-thread (и mlx_lock) пока daemon thread
            # не завершится ИЛИ пока не истечёт MLX_HANG_HARD_KILL_SEC.
            # Ограниченное ожидание (wave-20 MED fix): ранее join() был
            # unbounded — бэкенд мог висеть вечно если Metal GPU полностью зависал.
            # Теперь join ограничен MLX_HANG_HARD_KILL_SEC (default 10s, KRAB-EAR-BACKEND-1V).
            thread.join(timeout=MLX_HANG_HARD_KILL_SEC)
            if thread.is_alive():
                # Daemon thread не завершился за hard-kill timeout.
                # Мы вынуждены выйти и освободить mlx_lock — это означает, что
                # daemon thread продолжает работать с MLX GPU без блокировки
                # (узкое SIGSEGV-окно), но это строго лучше, чем бесконечный
                # стол бэкенда.  BackendSupervisor (HealthMonitor) убьёт процесс
                # через SIGTERM/SIGKILL если ping не отвечает.
                logger.error(
                    "[MLXWatchdog] daemon thread STILL ALIVE after hard-kill timeout "
                    "(%.1fs). Releasing mlx_lock and propagating MLXTimeoutError "
                    "— narrow SIGSEGV race window possible (model=%s, crashes=%d). "
                    "BackendSupervisor should terminate the process.",
                    MLX_HANG_HARD_KILL_SEC,
                    model_name,
                    self.crashes_count,
                )
            else:
                logger.warning(
                    "[MLXWatchdog] daemon thread completed within hard-kill window "
                    "(model=%s). Propagating MLXTimeoutError.",
                    model_name,
                )
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

_SENTRY_REPORT_THRESHOLDS = frozenset({1, 5, 25, 125, 625})


def _should_report_to_sentry(crash_count: int) -> bool:
    """Throttle: report only at exponential thresholds (1, 5, 25, 125, 625).

    First crash always reported; subsequent crashes sampled to avoid Sentry spam.
    Backend remains alive after timeout (watchdog signals fallback), so this is
    effectively a warning-level metric, not a crash event.
    """
    return crash_count in _SENTRY_REPORT_THRESHOLDS


def _notify_sentry_timeout(
    model_name: str,
    elapsed_sec: float,
    crash_count: int,
) -> None:
    """Отправить событие таймаута в Sentry (no-op если Sentry не инициализирован).

    Severity = warning (не error): watchdog корректно отработал, fallback signaled,
    backend жив. Throttled через _should_report_to_sentry чтобы не флудить Sentry.
    """
    try:
        from backend.observability import (  # noqa: PLC0415
            is_sentry_initialized,
            add_breadcrumb,
        )
        if not is_sentry_initialized():
            return
        # Breadcrumb всегда — попадает в следующий crash event как контекст
        add_breadcrumb(
            category="mlx",
            message="MLX inference timeout — Metal GPU may be stuck",
            level="warning",
            data={
                "model": model_name,
                "elapsed_sec": round(elapsed_sec, 2),
                "crash_count": crash_count,
            },
        )
        # Issue только на exponential thresholds — иначе флудит Sentry
        if not _should_report_to_sentry(crash_count):
            return
        import sentry_sdk  # noqa: PLC0415

        with sentry_sdk.push_scope() as scope:
            scope.set_tag("component", "mlx_watchdog")
            scope.set_tag("mlx_model", model_name)
            scope.set_level("warning")
            sentry_sdk.capture_message(
                f"MLXWatchdog: timeout after {elapsed_sec:.1f}s "
                f"(model={model_name}, total_crashes={crash_count})"
            )
    except Exception:  # noqa: BLE001
        pass  # telemetry никогда не должна ломать основной поток


def _push_watchdog_hang(
    model_name: str,
    elapsed_sec: float,
    crash_count: int,
) -> None:
    """Push stt.mlx_watchdog_hang KrabError via module-level error bus (no-op if absent).

    The error bus reference is injected at runtime by BackendService after init:
      mlx_subprocess._error_bus = self._error_bus
    Falls back silently when not wired (tests, standalone usage).
    """
    try:
        error_bus = globals().get("_error_bus")
        if error_bus is None:
            return
        from backend.error_bus import KrabError  # noqa: PLC0415
        from backend.error_codes import ERROR_REGISTRY  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415
        entry = ERROR_REGISTRY.get("stt.mlx_watchdog_hang", {})
        err = KrabError(
            severity=entry.get("severity", "critical"),
            component="stt",
            code="stt.mlx_watchdog_hang",
            message_user=entry.get("user_msg_ru", "MLX watchdog: GPU hang"),
            message_debug=(
                f"MLXWatchdog: timeout after {elapsed_sec:.1f}s "
                f"(model={model_name}, total_crashes={crash_count})"
            ),
            timestamp=datetime.now(timezone.utc),
            context={"model": model_name, "elapsed_sec": round(elapsed_sec, 2),
                     "crash_count": crash_count},
            actionable=entry.get("actionable", False),
            action_id=entry.get("action_id"),
        )
        error_bus.push(err)
    except Exception:  # noqa: BLE001
        pass  # telemetry никогда не должна ломать основной поток


# Module-level error bus reference — injected by BackendService after construction.
# Pattern mirrors how engine._error_bus is wired in service.py __init__.
_error_bus: "object | None" = None  # type: ignore[assignment]
