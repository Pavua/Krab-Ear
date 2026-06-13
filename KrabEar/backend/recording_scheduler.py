"""RecordingScheduler — планировщик записей по расписанию для Krab Ear.

Позволяет задать запись на определённое время с заданной длительностью.
Персистентность через {data_dir}/scheduled_recordings.json.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.RecordingScheduler")

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

# DoS cap (MED wave-25)
MAX_SCHEDULED_RECORDINGS = 1_000
# Eviction: remove terminal entries older than this many seconds
_EVICT_AFTER_SECONDS = 86_400  # 24 h

# C1: pending schedule cap — reject new schedules when this many are already pending
MAX_PENDING_SCHEDULES = 50

# C2: validation bounds
_MAX_DURATION_SEC = 7200  # 2 hours
_MAX_FUTURE_DAYS = 30  # reject start_time > 30 days from now

# C1: background trigger poll interval (seconds)
_TRIGGER_POLL_INTERVAL = 30


class RecordingScheduler:
    """Планировщик записей: создание, отмена, перечисление, триггер по времени.

    Параметр trigger_fn (C1): если передан, фоновый поток вызывает check_and_trigger()
    каждые _TRIGGER_POLL_INTERVAL секунд и, при обнаружении задания, передаёт его
    параметры в trigger_fn(duration_sec, label).  Поток демонический — останавливается
    вместе с процессом или по вызову stop().
    """

    def __init__(
        self,
        data_dir: str | Path,
        trigger_fn: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._file = self._data_dir / "scheduled_recordings.json"
        self._lock = threading.Lock()
        self._schedules: dict[str, dict] = {}
        self._load()

        # C1: background trigger thread
        self._trigger_fn = trigger_fn
        self._stop_event = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        if trigger_fn is not None:
            self._bg_thread = threading.Thread(
                target=self._trigger_loop,
                name="RecordingScheduler-trigger",
                daemon=True,
            )
            self._bg_thread.start()
            logger.debug("RecordingScheduler: фоновый поток триггера запущен (интервал %ds)", _TRIGGER_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Фоновый поток (C1)
    # ------------------------------------------------------------------

    def _trigger_loop(self) -> None:
        """Фоновый цикл: каждые _TRIGGER_POLL_INTERVAL секунд проверяет расписание."""
        while not self._stop_event.wait(timeout=_TRIGGER_POLL_INTERVAL):
            try:
                triggered = self.check_and_trigger()
                if triggered and self._trigger_fn is not None:
                    try:
                        self._trigger_fn(
                            int(triggered.get("duration_sec", 0)),
                            str(triggered.get("label", "")),
                        )
                    except Exception:
                        logger.exception(
                            "RecordingScheduler: ошибка вызова trigger_fn для задания %s",
                            triggered.get("id"),
                        )
            except Exception:
                logger.exception("RecordingScheduler: ошибка в фоновом цикле триггера")

    def stop(self) -> None:
        """Останавливает фоновый поток триггера (если был запущен)."""
        self._stop_event.set()
        if self._bg_thread is not None and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=5)
            logger.debug("RecordingScheduler: фоновый поток остановлен")

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

    def _evict_old_terminal(self) -> None:
        """Удаляет завершённые/отменённые записи старше _EVICT_AFTER_SECONDS.

        Вызывается под self._lock. Предотвращает неограниченный рост файла.
        """
        now = datetime.now(tz=timezone.utc)
        to_delete = []
        for sid, entry in self._schedules.items():
            if entry.get("status") not in (STATUS_COMPLETED, STATUS_CANCELLED):
                continue
            created_raw = entry.get("created_at", "")
            try:
                created_dt = _parse_datetime(created_raw)
            except Exception:
                continue
            if (now - created_dt).total_seconds() >= _EVICT_AFTER_SECONDS:
                to_delete.append(sid)
        for sid in to_delete:
            del self._schedules[sid]
        if to_delete:
            logger.debug(
                "RecordingScheduler: evicted %d terminal entries",
                len(to_delete),
            )

    def _load(self) -> None:
        """Загружает расписания из файла, если он существует."""
        if not self._file.exists():
            return
        try:
            raw = self._file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                self._schedules = {item["id"]: item for item in data if isinstance(item, dict) and "id" in item}
            elif isinstance(data, dict):
                self._schedules = data
        except Exception:
            logger.exception("Ошибка загрузки scheduled_recordings.json")

    def _save(self) -> None:
        """Сохраняет текущие расписания в файл."""
        from core.atomic_io import atomic_write_text
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            payload = list(self._schedules.values())
            atomic_write_text(self._file, json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            logger.exception("Ошибка сохранения scheduled_recordings.json")

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def schedule_recording(
        self,
        start_time: str,
        duration_sec: int,
        label: str = "",
    ) -> dict:
        """Добавляет новое запланированное задание записи.

        Args:
            start_time: ISO 8601 строка (напр. "2026-04-12T15:00:00" или "2026-04-12T15:00:00+03:00").
            duration_sec: Длительность записи в секундах (1..7200).
            label: Опциональная метка/описание.

        Returns:
            Словарь с полями задания.

        Raises:
            ValueError: если duration_sec вне диапазона [1, _MAX_DURATION_SEC],
                        start_time в прошлом или дальше _MAX_FUTURE_DAYS дней.
        """
        # C2: validate duration
        if not (1 <= int(duration_sec) <= _MAX_DURATION_SEC):
            raise ValueError(
                f"duration_sec должен быть в диапазоне [1, {_MAX_DURATION_SEC}], "
                f"получено: {duration_sec}"
            )

        # Парсим start_time и нормализуем до UTC ISO строки
        try:
            dt = _parse_datetime(start_time)
        except Exception as exc:
            raise ValueError(f"Неверный формат start_time: {exc}") from exc

        # C2: validate start_time range
        now = datetime.now(tz=timezone.utc)
        if dt < now:
            raise ValueError(
                f"start_time не может быть в прошлом: {dt.isoformat()} < {now.isoformat()}"
            )
        max_future = now + timedelta(days=_MAX_FUTURE_DAYS)
        if dt > max_future:
            raise ValueError(
                f"start_time не может быть дальше {_MAX_FUTURE_DAYS} дней от текущего момента: "
                f"{dt.isoformat()} > {max_future.isoformat()}"
            )

        schedule_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "id": schedule_id,
            "start_time": dt.isoformat(),
            "duration_sec": int(duration_sec),
            "label": str(label),
            "status": STATUS_PENDING,
            "created_at": now.isoformat(),
        }
        with self._lock:
            # Evict stale terminal entries before checking the caps
            self._evict_old_terminal()
            # C1: pending-count cap (stricter than total cap)
            pending_count = sum(
                1 for e in self._schedules.values()
                if e.get("status") == STATUS_PENDING
            )
            if pending_count >= MAX_PENDING_SCHEDULES:
                raise ValueError(
                    f"Достигнут лимит ожидающих записей ({MAX_PENDING_SCHEDULES}). "
                    "Отмените некоторые задания перед добавлением новых."
                )
            if len(self._schedules) >= MAX_SCHEDULED_RECORDINGS:
                raise ValueError(
                    f"Достигнут лимит запланированных записей ({MAX_SCHEDULED_RECORDINGS}). "
                    "Отмените или дождитесь завершения существующих заданий."
                )
            self._schedules[schedule_id] = entry
            self._save()

        logger.info("Запись запланирована: id=%s start=%s dur=%ds label=%r", schedule_id, dt.isoformat(), duration_sec, label)
        return dict(entry)

    def cancel_scheduled(self, schedule_id: str) -> bool:
        """Отменяет задание по ID. Возвращает True если задание найдено и отменено."""
        with self._lock:
            entry = self._schedules.get(schedule_id)
            if entry is None:
                return False
            if entry["status"] != STATUS_PENDING:
                return False
            entry["status"] = STATUS_CANCELLED
            self._save()

        logger.info("Запись отменена: id=%s", schedule_id)
        return True

    def list_scheduled(self) -> list[dict]:
        """Возвращает все записи расписания (все статусы), отсортированные по start_time.

        Заодно выполняет eviction старых terminal-записей (>24h) для самоочистки.
        """
        with self._lock:
            self._evict_old_terminal()
            items = list(self._schedules.values())
        items.sort(key=lambda x: x.get("start_time", ""))
        return [dict(item) for item in items]

    def get_next_scheduled(self) -> dict | None:
        """Возвращает ближайшее предстоящее (pending) задание или None."""
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            pending = [
                item for item in self._schedules.values()
                if item.get("status") == STATUS_PENDING
            ]
        if not pending:
            return None

        def _start_dt(item: dict) -> datetime:
            try:
                return _parse_datetime(item["start_time"])
            except Exception:
                return datetime.max.replace(tzinfo=timezone.utc)

        upcoming = [item for item in pending if _start_dt(item) >= now]
        if not upcoming:
            return None
        return dict(min(upcoming, key=_start_dt))

    def check_and_trigger(self) -> dict | None:
        """Проверяет, есть ли задание, которое нужно запустить прямо сейчас.

        Возвращает параметры задания (duration_sec, label, id) если пора записывать,
        иначе None. Триггернутое задание переводится в статус 'completed'.
        """
        now = datetime.now(tz=timezone.utc)
        with self._lock:
            for entry in list(self._schedules.values()):
                if entry.get("status") != STATUS_PENDING:
                    continue
                try:
                    start_dt = _parse_datetime(entry["start_time"])
                except Exception:
                    continue
                # Запускаем если время наступило (с допуском 5 секунд вперёд)
                delta = (now - start_dt).total_seconds()
                if 0 <= delta <= 5:
                    entry["status"] = STATUS_COMPLETED
                    self._save()
                    logger.info("Триггер записи: id=%s label=%r", entry["id"], entry.get("label"))
                    return {
                        "id": entry["id"],
                        "duration_sec": entry["duration_sec"],
                        "label": entry.get("label", ""),
                    }
        return None

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_schedule_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        start_time = params.get("start_time")
        if not start_time:
            raise ValueError("Параметр start_time обязателен")
        duration_sec = int(params.get("duration_sec", 0))
        label = str(params.get("label", ""))
        entry = self.schedule_recording(start_time=start_time, duration_sec=duration_sec, label=label)
        return {"schedule": entry}

    def handle_cancel_scheduled_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        schedule_id = params.get("schedule_id") or params.get("id")
        if not schedule_id:
            raise ValueError("Параметр schedule_id обязателен")
        cancelled = self.cancel_scheduled(str(schedule_id))
        return {"cancelled": cancelled}

    def handle_list_scheduled_recordings(self, params: dict[str, Any]) -> dict[str, Any]:
        items = self.list_scheduled()
        return {"schedules": items, "count": len(items)}


# ------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------

def _parse_datetime(value: str) -> datetime:
    """Парсит ISO 8601 строку в datetime с timezone (UTC если не указана)."""
    # Python 3.7+ fromisoformat не поддерживает 'Z' суффикс до 3.11
    value = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
