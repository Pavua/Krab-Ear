"""ErrorReporter — сервис сбора и категоризации ошибок бэкенда Krab Ear.

Хранит последние 500 ошибок в кольцевом буфере памяти.
Обеспечивает IPC-методы get_error_report и get_error_stats.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

VALID_CATEGORIES = {"stt", "llm", "translation", "ipc", "audio", "storage", "other"}
_BUFFER_SIZE = 500


@dataclass
class ErrorRecord:
    """Одна запись об ошибке."""

    timestamp: str         # ISO-8601 UTC
    component: str         # например "stt", "llm", "ipc"
    error_type: str        # например "TimeoutError", "ValueError"
    message: str
    context: dict          # произвольные данные контекста
    resolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "component": self.component,
            "error_type": self.error_type,
            "message": self.message,
            "context": self.context,
            "resolved": self.resolved,
        }


class ErrorReporter:
    """Кольцевой буфер ошибок со статистикой по компонентам и типам."""

    def __init__(self, max_size: int = _BUFFER_SIZE) -> None:
        self._max_size = max_size
        self._buffer: deque[ErrorRecord] = deque(maxlen=max_size)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def report_error(
        self,
        component: str,
        error_type: str,
        message: str,
        context: dict | None = None,
    ) -> ErrorRecord:
        """Добавляет запись об ошибке в буфер. Потокобезопасно."""
        # Нормализуем категорию компонента
        normalized = component.lower().strip()
        if normalized not in VALID_CATEGORIES:
            normalized = "other"

        record = ErrorRecord(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            component=normalized,
            error_type=str(error_type),
            message=str(message),
            context=dict(context) if context else {},
        )
        with self._lock:
            self._buffer.append(record)
        return record

    def get_recent_errors(self, limit: int = 50) -> list[ErrorRecord]:
        """Возвращает последние limit ошибок (от новых к старым)."""
        with self._lock:
            items = list(self._buffer)
        # Новейшие — первыми
        items.reverse()
        return items[:limit]

    def get_error_stats(self) -> dict[str, Any]:
        """Статистика: счётчики по компоненту, типу и 5 временным окнам."""
        with self._lock:
            items = list(self._buffer)

        by_component: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for r in items:
            by_component[r.component] = by_component.get(r.component, 0) + 1
            by_type[r.error_type] = by_type.get(r.error_type, 0) + 1

        # Временные периоды (в секундах): 5m, 1h, 24h
        now = datetime.now(tz=timezone.utc)
        windows: dict[str, int] = {"last_5m": 0, "last_1h": 0, "last_24h": 0}
        for r in items:
            try:
                ts = datetime.fromisoformat(r.timestamp)
                delta = (now - ts).total_seconds()
                if delta <= 300:
                    windows["last_5m"] += 1
                if delta <= 3600:
                    windows["last_1h"] += 1
                if delta <= 86400:
                    windows["last_24h"] += 1
            except (ValueError, TypeError):
                pass

        return {
            "total": len(items),
            "by_component": by_component,
            "by_type": by_type,
            "by_time_window": windows,
        }

    def resolve_error(self, index: int) -> bool:
        """Помечает ошибку с указанным индексом в буфере как resolved. Возвращает True при успехе."""
        with self._lock:
            items = list(self._buffer)
            if 0 <= index < len(items):
                items[index].resolved = True
                return True
        return False

    def clear(self) -> None:
        """Очищает буфер (для тестов / команды сброса)."""
        with self._lock:
            self._buffer.clear()

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_get_error_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-метод get_error_report: последние N ошибок."""
        limit = int(params.get("limit", 50))
        limit = max(1, min(limit, self._max_size))
        errors = self.get_recent_errors(limit=limit)
        return {
            "errors": [e.to_dict() for e in errors],
            "total_in_buffer": len(self._buffer),
        }

    def handle_get_error_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-метод get_error_stats: счётчики по компоненту, типу, временным окнам."""
        return self.get_error_stats()
