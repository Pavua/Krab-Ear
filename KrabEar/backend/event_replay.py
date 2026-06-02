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
import os
import threading
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.EventReplay")

_MAX_BUFFER_SIZE = 10_000

# Жёсткий байтовый предел файла персистенции.
# W829 truncate-on-restart ограничивает рост только МЕЖДУ перезапусками; внутри
# одной длительной сессии (launchd backend живёт сутками) record_event делает
# append на каждое событие, и файл растёт без границ, хотя in-memory кольцо
# ограничено 10 000 записей. Когда размер файла превышает этот предел, файл
# атомарно пересобирается из текущего кольцевого буфера (последние ~10 000
# событий) — так файл отслеживает кольцо, а не растёт вечно. 8 МБ заведомо
# больше, чем 2× содержимое полного кольца, поэтому пересборка случается редко
# и общий путь (append) остаётся быстрым.
_MAX_FILE_BYTES = 8 * 1024 * 1024


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
        settings_provider: Optional[Callable[[], dict]] = None,
        max_file_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=max_buffer)
        self._seq: int = 0  # монотонный счётчик для восстановления порядка
        self._persist_path: Path | None = Path(persist_path) if persist_path else None
        self._settings_provider = settings_provider
        # Байтовый предел файла; при превышении — атомарная пересборка из кольца.
        self._max_file_bytes: int = max(1, int(max_file_bytes))
        # Счётчик байт, записанных в текущий открытый файл (дешевле, чем stat()
        # на каждое событие): мы всегда дописываем в конец, поэтому ведём учёт сами.
        self._file_bytes: int = 0
        self._file_handle = None

        if self._persist_path:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            # Открываем в режиме "w" (truncate), а не "a" (append):
            # файл ограничен событиями текущей сессии — in-memory кольцевой буфер
            # уже ограничивает их до 10 000. Режим "a" приводил к неограниченному
            # росту (~14 МБ/день, ~5 ГБ/год). W829 CRIT-1.
            self._file_handle = self._persist_path.open("w", encoding="utf-8")
            self._file_bytes = 0
            logger.info("EventReplayManager: персистенция в %s (truncate на старте)", self._persist_path)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def _is_privacy_mode(self) -> bool:
        """Возвращает True если privacy_mode_enabled активен в настройках."""
        if self._settings_provider is None:
            return False
        try:
            return bool(self._settings_provider().get("privacy_mode_enabled", False))
        except Exception:
            return False

    def record_event(
        self,
        event_type: str,
        data: dict[str, Any],
        ts: Optional[str] = None,
    ) -> None:
        """Записывает событие с указанным или текущим timestamp.

        Args:
            event_type: тип события (например ``"stt.final"``).
            data: payload события.
            ts: ISO 8601 UTC timestamp. Если передан — используется как есть,
                что позволяет сохранить тот же момент времени, который был
                вычислен в EventBus.emit() (W1673 F4 LOW). Если ``None`` —
                берётся текущее время (обратная совместимость).

        В режиме конфиденциальности (privacy_mode_enabled=True) вместо
        оригинальных данных сохраняются только метаданные-заглушки, чтобы
        текст транскрипций не попал в лог событий.
        """
        if self._is_privacy_mode():
            event_data: dict[str, Any] = {"redacted": True, "reason": "privacy_mode"}
        else:
            event_data = data if isinstance(data, dict) else {}
        entry = {
            "type": event_type,
            "ts": ts if ts is not None else _utc_now_iso(),
            "data": event_data,
        }
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buffer.append(entry)
            if self._file_handle is not None:
                line = json.dumps(entry, ensure_ascii=False) + "\n"
                try:
                    self._file_handle.write(line)
                    self._file_handle.flush()
                    # Учёт по фактическим UTF-8 байтам (кириллица многобайтовая).
                    self._file_bytes += len(line.encode("utf-8"))
                except OSError as exc:
                    logger.warning("EventReplayManager: ошибка записи в файл: %s", exc)
                # Файл перерос предел — атомарно пересобираем его из кольцевого
                # буфера, чтобы он не рос без границ внутри одной сессии (W1770).
                if self._file_bytes > self._max_file_bytes:
                    self._compact_file_to_buffer_locked()

    def _compact_file_to_buffer_locked(self) -> None:
        """Пересобирает файл персистенции из текущего кольцевого буфера.

        Вызывать ТОЛЬКО при удержании ``self._lock``. Пишет все события кольца
        во временный файл и атомарно (tmp + ``os.replace``) заменяет им основной
        файл, после чего переоткрывает дескриптор в режиме "a" (append) и
        обновляет счётчик байт. Так файл отслеживает кольцо (~10 000 событий) и
        не растёт без границ внутри одной длительной сессии (W829 ограничивал
        рост только между перезапусками). Best-effort: при ошибке ввода-вывода
        пишет warning и оставляет текущий дескриптор без изменений.
        """
        if self._persist_path is None or self._file_handle is None:
            return
        tmp_path = self._persist_path.with_name(self._persist_path.name + ".tmp")
        try:
            written = 0
            with tmp_path.open("w", encoding="utf-8") as tmp:
                for item in self._buffer:
                    line = json.dumps(item, ensure_ascii=False) + "\n"
                    tmp.write(line)
                    written += len(line.encode("utf-8"))
                tmp.flush()
                os.fsync(tmp.fileno())
            # Закрываем старый дескриптор перед атомарной заменой.
            try:
                self._file_handle.close()
            except OSError:
                pass
            os.replace(tmp_path, self._persist_path)
            # Переоткрываем в append: дальнейшие события дописываются в конец
            # пересобранного файла.
            self._file_handle = self._persist_path.open("a", encoding="utf-8")
            self._file_bytes = written
            logger.info(
                "EventReplayManager: файл пересобран из кольца (events=%d, bytes=%d)",
                len(self._buffer),
                written,
            )
        except OSError as exc:
            logger.warning("EventReplayManager: ошибка пересборки файла: %s", exc)
            # Подчищаем временный файл, если он остался.
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

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
        """Очищает кольцевой буфер и усекает файл персистенции (если задан).

        Используется для privacy-purge: файл event_replay.ndjson содержит
        cleartext-текст транскрипций, поэтому при полной очистке данных его надо
        обнулить вместе с in-memory кольцом. Координатор (handle_purge_all_data)
        вызывает этот метод — здесь только сама очистка.

        Усекается ИМЕННО открытый дескриптор (seek(0) + truncate(0)), а не
        отдельный fd: запись идёт в режиме "w"/"a", у дескриптора своё смещение,
        и truncate через сторонний open() оставил бы «дыру» — последующие
        события легли бы за нулями. Файл на диске сохраняется (не удаляется),
        содержимое обнуляется.
        """
        with self._lock:
            self._buffer.clear()
            if self._file_handle is not None:
                try:
                    self._file_handle.seek(0)
                    self._file_handle.truncate(0)
                    self._file_handle.flush()
                    self._file_bytes = 0
                except OSError:
                    logger.warning("event_replay: failed to truncate open persist handle on clear")
            elif self._persist_path is not None:
                # Дескриптор закрыт (например, после close()) — усекаем файл напрямую.
                try:
                    self._persist_path.write_text("", encoding="utf-8")
                    self._file_bytes = 0
                except OSError:
                    logger.warning("event_replay: failed to truncate persist file on clear")

    def close(self) -> None:
        """Закрывает файл персистенции, если он открыт."""
        with self._lock:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except OSError:
                    pass
                self._file_handle = None
            self._file_bytes = 0

    # ------------------------------------------------------------------
    # IPC-обработчики (совместимы с паттерном handle_* в BackendService)
    # ------------------------------------------------------------------

    def handle_get_event_log(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_event_log."""
        try:
            limit = int(params.get("limit", 100))
            if limit < 1 or limit > 10_000:
                limit = max(1, min(10_000, limit))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be an integer 1-10000"}
        events = self.get_events(
            since=params.get("since"),
            event_type=params.get("event_type"),
            limit=limit,
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
