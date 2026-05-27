"""ErrorReporter — сервис сбора и категоризации ошибок бэкенда Krab Ear.

Хранит последние 500 ошибок в кольцевом буфере памяти.
Обеспечивает IPC-методы get_error_report и get_error_stats.

W977 hardening (Wave 987):
- F2 MEDIUM: message + context теперь усекаются до _MAX_MESSAGE_LEN / _MAX_CONTEXT_BYTES.
  Privacy mode (settings.privacy_mode_enabled) полностью обнуляет message/context.
- F3 LOW: handle_get_error_report снимает (errors, total_in_buffer) за один lock-захват.
- F5 LOW: длинные stack-trace в message поле обрезаются тем же _MAX_MESSAGE_LEN cap.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.observability import capture_exception as _sentry_capture

VALID_CATEGORIES = {"stt", "llm", "translation", "ipc", "audio", "storage", "other"}
_BUFFER_SIZE = 500

# W977 F2/F5 — caps on stored data per error record
_MAX_MESSAGE_LEN = 1024       # chars; truncates long tracebacks too (F5)
_MAX_CONTEXT_BYTES = 4096     # JSON-serialised bytes; replaced with tombstone if exceeded


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
    """Кольцевой буфер ошибок со статистикой по компонентам и типам.

    Args:
        max_size:          максимальное число записей в буфере.
        settings_provider: callable() → dict[str, Any] для чтения runtime-настроек.
                           Если None — privacy_mode всегда отключён.
    """

    def __init__(
        self,
        max_size: int = _BUFFER_SIZE,
        settings_provider: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self._max_size = max_size
        self._buffer: deque[ErrorRecord] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._settings_provider = settings_provider

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _is_privacy_mode(self) -> bool:
        """Возвращает True если runtime-настройка privacy_mode_enabled = True."""
        if self._settings_provider is None:
            return False
        try:
            cfg = self._settings_provider()
            return bool(cfg.get("privacy_mode_enabled", False))
        except Exception:
            return False

    @staticmethod
    def _sanitize_message(message: str) -> str:
        """Усекает сообщение до _MAX_MESSAGE_LEN символов (F2, F5)."""
        if len(message) > _MAX_MESSAGE_LEN:
            return message[:_MAX_MESSAGE_LEN] + "... [truncated]"
        return message

    @staticmethod
    def _sanitize_context(context: dict | None) -> dict:
        """Усекает context до _MAX_CONTEXT_BYTES (F2).

        Если context не сериализуется — возвращает маркер non_serializable.
        Если превышает лимит — возвращает маркер truncated.
        """
        if context is None:
            return {}
        try:
            serialized = json.dumps(context, ensure_ascii=False)
            if len(serialized) > _MAX_CONTEXT_BYTES:
                return {"truncated": True, "original_size_bytes": len(serialized)}
            return dict(context)
        except (TypeError, ValueError):
            return {"non_serializable": True}

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def report_error(
        self,
        component: str,
        error_type: str,
        message: str,
        context: dict | None = None,
        exc: Exception | None = None,
    ) -> ErrorRecord:
        """Добавляет запись об ошибке в буфер. Потокобезопасно.

        W977 hardening:
        - message усекается до _MAX_MESSAGE_LEN (F2, F5).
        - context усекается до _MAX_CONTEXT_BYTES JSON (F2).
        - В privacy_mode: message и context обнуляются до redacted-маркеров (F2).

        Если Sentry инициализирован и передан exc — зеркалирует исключение в Sentry.
        """
        # Нормализуем категорию компонента
        normalized = component.lower().strip()
        if normalized not in VALID_CATEGORIES:
            normalized = "other"

        # F2/F5 — sanitize перед сохранением
        safe_message = self._sanitize_message(str(message))
        safe_context = self._sanitize_context(context)

        # F2 — privacy guard: перезаписываем после санитизации
        if self._is_privacy_mode():
            safe_message = "<redacted: privacy_mode>"
            safe_context = {}

        record = ErrorRecord(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            component=normalized,
            error_type=str(error_type),
            message=safe_message,
            context=safe_context,
        )
        with self._lock:
            self._buffer.append(record)

        # Mirror to Sentry if SDK initialized and exception provided
        if exc is not None:
            _sentry_capture(exc, component=normalized)

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
        """IPC-метод get_error_report: последние N ошибок.

        W977 F3: errors и total_in_buffer снимаются за один lock-захват,
        исключая TOCTOU-расхождение между двумя отдельными чтениями буфера.
        """
        limit = int(params.get("limit", 50))
        limit = max(1, min(limit, self._max_size))

        # F3 — атомарный снимок: список + размер за один lock
        with self._lock:
            items = list(self._buffer)
            total_in_buffer = len(self._buffer)

        # Новейшие — первыми, затем применяем limit
        items.reverse()
        errors = items[:limit]

        return {
            "errors": [e.to_dict() for e in errors],
            "total_in_buffer": total_in_buffer,
        }

    def handle_get_error_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-метод get_error_stats: счётчики по компоненту, типу, временным окнам."""
        return self.get_error_stats()
