"""Система воспроизведения событий Krab Ear для отладки.

EventReplayManager записывает все события в кольцевой буфер и опционально
сохраняет их в NDJSON-файл. Предоставляет методы для фильтрации, воспроизведения
и статистики событий.

Интеграция: подписывается на EventBus, либо принимает события напрямую
через record_event(). Используется через IPC-методы get_event_log / get_event_stats.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.EventReplay")

_MAX_BUFFER_SIZE = 10_000

# CRITICAL: must be "w" not "a" — see W832/W969.
# Append mode causes unbounded file growth (the regression this constant defends against).
# Do NOT change this to "a" — the W970 branch diff attempted this revert.
_REPLAY_LOG_OPEN_MODE = "w"


def _open_replay_log(path: Path):
    """Открывает файл лога событий в режиме перезаписи (write, не append).

    Использует константу ``_REPLAY_LOG_OPEN_MODE`` вместо литерала,
    чтобы любое ошибочное изменение режима было немедленно заметно.
    """
    return path.open(_REPLAY_LOG_OPEN_MODE, encoding="utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(ts: str) -> datetime:
    """Разбирает ISO 8601 строку в datetime с tzinfo=UTC."""
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Неверный формат timestamp: {ts!r}") from exc


class EventReplayManager:
    """Потокобезопасный менеджер воспроизведения событий.

    Хранит последние 10 000 событий в кольцевом буфере и опционально
    персистирует их в NDJSON-файл.

    Формат записи:
        {"type": str, "ts": ISO-8601 UTC, "data": dict, "seq": int}
    """

    def __init__(
        self,
        persist_path: Path | str | None = None,
        max_buffer: int = _MAX_BUFFER_SIZE,
    ) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self._seq: int = 0  # монотонный счётчик для восстановления порядка
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        self._file_handle = None

        if self._persist_path:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = _open_replay_log(self._persist_path)
            logger.info("EventReplayManager: персистенция в %s", self._persist_path)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def record_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Записывает событие с текущим timestamp."""
        entry = {
            "type": event_type,
            "ts": _utc_now_iso(),
            "data": data if isinstance(data, dict) else {},
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buffer.append(entry)
            if self._file_handle is not None:
                try:
                    self._file_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    self._file_handle.flush()
                except OSError as exc:
                    logger.warning("EventReplayManager: ошибка записи в файл: %s", exc)

    def get_events(
        self,
        since: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Возвращает события из буфера с опциональной фильтрацией.

        Args:
            since: ISO 8601 timestamp — возвращать только события после него.
            event_type: фильтр по типу события.
            limit: максимальное количество возвращаемых записей (не более 10 000).
        """
        limit = max(1, min(limit, _MAX_BUFFER_SIZE))
        since_dt: datetime | None = _parse_ts(since) if since else None

        with self._lock:
            snapshot = list(self._buffer)

        results = []
        for entry in snapshot:
            if event_type and entry["type"] != event_type:
                continue
            if since_dt:
                try:
                    entry_dt = _parse_ts(entry["ts"])
                except ValueError:
                    continue
                if entry_dt <= since_dt:
                    continue
            results.append(entry)
            if len(results) >= limit:
                break

        return results

    def replay_events(self, from_ts: str, to_ts: str) -> list[dict[str, Any]]:
        """Возвращает события в диапазоне [from_ts, to_ts] в хронологическом порядке.

        Args:
            from_ts: начало диапазона (включительно), ISO 8601.
            to_ts: конец диапазона (включительно), ISO 8601.
        """
        from_dt = _parse_ts(from_ts)
        to_dt = _parse_ts(to_ts)

        with self._lock:
            snapshot = list(self._buffer)

        results = []
        for entry in snapshot:
            try:
                entry_dt = _parse_ts(entry["ts"])
            except ValueError:
                continue
            if from_dt <= entry_dt <= to_dt:
                results.append(entry)

        # Сортируем по seq для гарантированного порядка
        results.sort(key=lambda e: e.get("seq", 0))
        return results

    def get_event_stats(self) -> dict[str, Any]:
        """Возвращает статистику: количество событий по типу, скорость за минуту."""
        with self._lock:
            snapshot = list(self._buffer)
            total = len(snapshot)

        counts_by_type: dict[str, int] = defaultdict(int)
        for entry in snapshot:
            counts_by_type[entry["type"]] += 1

        # Скорость за последнюю минуту
        now = datetime.now(timezone.utc)
        rate_by_type: dict[str, float] = defaultdict(float)
        minute_counts: dict[str, int] = defaultdict(int)
        for entry in snapshot:
            try:
                entry_dt = _parse_ts(entry["ts"])
            except ValueError:
                continue
            age_sec = (now - entry_dt).total_seconds()
            if age_sec <= 60:
                minute_counts[entry["type"]] += 1

        for t, cnt in minute_counts.items():
            rate_by_type[t] = round(cnt / 1.0, 2)  # events per minute window

        return {
            "total_events": total,
            "counts_by_type": dict(counts_by_type),
            "rate_per_minute_by_type": dict(rate_by_type),
            "buffer_capacity": self._buffer.maxlen,
        }

    def clear(self) -> None:
        """Очищает буфер событий (не удаляет файл персистенции)."""
        with self._lock:
            self._buffer.clear()

    def close(self) -> None:
        """Закрывает файл персистенции, если он открыт."""
        with self._lock:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except OSError:
                    pass
                self._file_handle = None

    # ------------------------------------------------------------------
    # IPC-обработчики (совместимы с паттерном handle_* в BackendService)
    # ------------------------------------------------------------------

    def handle_get_event_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_event_log."""
        events = self.get_events(
            since=params.get("since"),
            event_type=params.get("event_type"),
            limit=int(params.get("limit", 100)),
        )
        return {"events": events, "count": len(events)}

    def handle_get_event_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_event_stats."""
        return self.get_event_stats()

    def handle_replay_events(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик replay_events."""
        from_ts = params.get("from_ts")
        to_ts = params.get("to_ts")
        if not from_ts or not to_ts:
            raise ValueError("Параметры from_ts и to_ts обязательны")
        events = self.replay_events(from_ts, to_ts)
        return {"events": events, "count": len(events)}


# Глобальный синглтон — создаётся без персистенции; BackendService может
# переопределить путь при инициализации.
replay_manager = EventReplayManager()
