"""Отслеживание здоровья процесса: RSS, uptime, активные запросы.

Без внешних зависимостей — только stdlib (resource.getrusage).
Используется в Phase A (ping IPC), Phase B (error context), Phase C (memory soak).
"""

from __future__ import annotations

import resource
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator


class HealthMetrics:
    """Thread-safe сборщик runtime-метрик процесса."""

    def __init__(self) -> None:
        self._start_monotonic = time.monotonic()
        self._active_requests = 0
        self._lock = threading.Lock()

    def rss_mb(self) -> float:
        """Resident Set Size в мегабайтах.

        На macOS `ru_maxrss` возвращается в bytes, на Linux — в KB.
        Округляем до 1 знака для удобства логов.
        """
        usage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            # macOS: ru_maxrss в bytes
            return round(usage.ru_maxrss / (1024 * 1024), 1)
        # Linux: ru_maxrss в KB
        return round(usage.ru_maxrss / 1024, 1)

    def uptime_sec(self) -> float:
        """Время от создания HealthMetrics в секундах (monotonic)."""
        return round(time.monotonic() - self._start_monotonic, 2)

    def active_requests(self) -> int:
        """Текущее число активных IPC-запросов."""
        with self._lock:
            return self._active_requests

    @contextmanager
    def track_request(self) -> Iterator[None]:
        """Context manager: инкрементирует счётчик на entry, декрементирует на exit.

        Декремент всегда выполняется (try/finally) даже при исключении.
        """
        with self._lock:
            self._active_requests += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_requests -= 1
