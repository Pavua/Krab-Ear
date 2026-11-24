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
import math
import threading
from typing import Callable, Optional

logger = logging.getLogger("KrabEar.Backend.PurgeScheduler")

_DEFAULT_INTERVAL_HOURS = 24.0
# Потолок ожидания = верхняя граница настройки в settings_validator (168 ч).
_MAX_INTERVAL_SEC = 168 * 3600.0


class PurgeScheduler:
    """Фоновый планировщик авто-очистки истории.

    Thread-safe. Запускает daemon-поток при вызове start().
    Останавливается через stop().

    Args:
        settings_get: Callable(key, default) → читает runtime-настройки.
        purge_fn:     Callable(days: int) → int — выполняет удаление,
                      возвращает количество удалённых записей.
    """

    # Сколько stop() ждёт поток; очистка большой истории может идти дольше.
    _STOP_JOIN_TIMEOUT_SEC = 5.0

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

    def _interval_sec(self) -> float:
        """Интервал ожидания в секундах, всегда конечный и в [1 с, 1 неделя].

        Нижняя граница — против CPU-spin (wave-34: Event.wait(≤0) возвращает
        сразу), верхняя и проверка конечности — против OverflowError: на
        ``Event.wait(inf)`` поток умирал навсегда (G3-P04-10). Сбой чтения
        настройки — тот же дефолт 24 ч, а не смерть потока.
        """
        try:
            hours = float(self._settings_get("auto_purge_check_interval_hours", 24))
        except Exception:
            logger.warning("PurgeScheduler: интервал не прочитан, беру 24 ч", exc_info=True)
            hours = _DEFAULT_INTERVAL_HOURS
        if not math.isfinite(hours):
            hours = _DEFAULT_INTERVAL_HOURS
        return min(_MAX_INTERVAL_SEC, max(1.0, hours * 3600.0))

    def _enabled(self) -> bool:
        """Включена ли очистка. Только настоящий ``True``: ``bool("false")`` — истина,
        и строка из повреждённого settings.json включила бы удаление истории."""
        return self._settings_get("auto_purge_enabled", False) is True

    def _tick(self) -> None:
        """Один шаг очистки. Любой сбой чтения настроек — пропуск шага.

        Пропуск, а не дефолт: удалять историю по выдуманному сроку хранения,
        когда настоящий прочитать не удалось, нельзя.
        """
        try:
            if not self._enabled():
                logger.debug("PurgeScheduler: auto_purge_enabled не True, пропуск")
                return
            days = int(self._settings_get("auto_purge_retention_days", 90))
        except Exception:
            logger.warning("PurgeScheduler: настройки не прочитаны, шаг пропущен", exc_info=True)
            return
        try:
            deleted = self._purge_fn(days)
            logger.info(
                "PurgeScheduler: авто-очистка завершена",
                extra={"deleted": deleted, "retention_days": days},
            )
        except Exception:
            logger.exception("PurgeScheduler: ошибка при авто-очистке истории")

    def _run(self) -> None:
        """Основной цикл фонового потока."""
        logger.info("PurgeScheduler запущен")
        while not self._stop_event.is_set():
            # Интервал перечитывается каждый тик; ожидание прерывается stop().
            self._stop_event.wait(timeout=self._interval_sec())
            if self._stop_event.is_set():
                break
            self._tick()
        logger.info("PurgeScheduler остановлен")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает фоновый поток планировщика (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                # Поток жив (в т.ч. после stop(), не дождавшегося очистки):
                # отменяем остановку вместо запуска второго потока рядом.
                self._stop_event.clear()
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="PurgeScheduler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> bool:
        """Останавливает фоновый поток; True — потока больше нет.

        Ждёт до ``_STOP_JOIN_TIMEOUT_SEC``. Если очистка ещё идёт, ссылка на
        поток СОХРАНЯЕТСЯ и возвращается False (G3-P04-10): раньше она
        обнулялась, ``get_status`` врал running=False, а повторный ``start()``
        запускал второй поток рядом с работающей очисткой.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(timeout=self._STOP_JOIN_TIMEOUT_SEC)
        if thread.is_alive():
            logger.error("PurgeScheduler: поток не завершился за %.1f с", self._STOP_JOIN_TIMEOUT_SEC)
            return False
        with self._lock:
            if self._thread is thread:
                self._thread = None
        return True

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Возвращает статус планировщика."""
        try:
            enabled = self._enabled()
        except Exception:
            enabled = False
        try:
            retention_days = int(self._settings_get("auto_purge_retention_days", 90))
        except Exception:
            retention_days = 90
        try:
            check_interval_hours = float(
                self._settings_get("auto_purge_check_interval_hours", 24)
            )
        except Exception:
            check_interval_hours = 24.0
        return {
            "enabled": enabled,
            "retention_days": retention_days,
            "check_interval_hours": check_interval_hours,
            "running": self._thread is not None and self._thread.is_alive(),
        }
