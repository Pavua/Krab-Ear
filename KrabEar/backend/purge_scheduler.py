"""PurgeScheduler — периодическое авто-удаление старых записей истории.

Запускает фоновый поток, который с интервалом check_interval_hours проверяет,
включён ли auto_purge_enabled, и если да — вызывает purge_fn(days) для удаления
записей старше auto_purge_retention_days дней.

Паттерн идентичен RecapScheduler:
  - Не наследует threading.Thread (composition, duck-type-безопасно).
  - stop() сигнализирует Event и присоединяет поток с таймаутом.
  - Таймаут ожидания зажат снизу на 1.0 с (предотвращает CPU-spin при ≤0).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

logger = logging.getLogger("KrabEar.Backend.PurgeScheduler")


class PurgeScheduler:
    """Фоновый планировщик авто-очистки истории.

    Thread-safe. Запускает daemon-поток при вызове start().
    Останавливается через stop().

    Args:
        settings_get: Callable(key, default) → читает runtime-настройки.
        purge_fn:     Callable(days: int) → int — выполняет удаление,
                      возвращает количество удалённых записей.
    """

    def __init__(
        self,
        *,
        settings_get: Callable[[str, object], object],
        purge_fn: Callable[[int], int],
    ) -> None:
        self._settings_get = settings_get
        self._purge_fn = purge_fn

        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Основной цикл фонового потока."""
        logger.info("PurgeScheduler запущен")
        while not self._stop_event.is_set():
            # Читаем интервал ожидания из runtime настроек каждый тик.
            # Нижняя граница 1.0 с — предотвращает CPU-spin при некорректном
            # значении (wave-34 lesson: Event.wait(≤0) возвращает немедленно).
            try:
                hours = float(
                    self._settings_get("auto_purge_check_interval_hours", 24)
                )
            except (TypeError, ValueError):
                hours = 24.0
            timeout = max(1.0, hours * 3600.0)

            # Ожидаем либо таймаут, либо сигнал stop().
            self._stop_event.wait(timeout=timeout)

            if self._stop_event.is_set():
                break

            # Проверяем, включён ли авто-пурдж.
            enabled = bool(self._settings_get("auto_purge_enabled", False))
            if not enabled:
                logger.debug("PurgeScheduler: auto_purge_enabled=False, пропуск")
                continue

            try:
                days = int(self._settings_get("auto_purge_retention_days", 90))
            except (TypeError, ValueError):
                days = 90

            try:
                deleted = self._purge_fn(days)
                logger.info(
                    "PurgeScheduler: авто-очистка завершена",
                    extra={"deleted": deleted, "retention_days": days},
                )
            except Exception:
                logger.exception("PurgeScheduler: ошибка при авто-очистке истории")

        logger.info("PurgeScheduler остановлен")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает фоновый поток планировщика (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="PurgeScheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Останавливает фоновый поток (ждёт завершения до 5 секунд)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Возвращает статус планировщика."""
        enabled = bool(self._settings_get("auto_purge_enabled", False))
        try:
            retention_days = int(self._settings_get("auto_purge_retention_days", 90))
        except (TypeError, ValueError):
            retention_days = 90
        try:
            check_interval_hours = float(
                self._settings_get("auto_purge_check_interval_hours", 24)
            )
        except (TypeError, ValueError):
            check_interval_hours = 24.0
        return {
            "enabled": enabled,
            "retention_days": retention_days,
            "check_interval_hours": check_interval_hours,
            "running": self._thread is not None and self._thread.is_alive(),
        }
