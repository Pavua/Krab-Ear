"""RecordingScheduler — планировщик записей по расписанию для Krab Ear.

Позволяет задать запись на определённое время с заданной длительностью.
Персистентность через {data_dir}/scheduled_recordings.json.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.RecordingScheduler")

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"


class RecordingScheduler:
    """Планировщик записей: создание, отмена, перечисление, триггер по времени."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._file = self._data_dir / "scheduled_recordings.json"
        self._lock = threading.Lock()
        self._schedules: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # Персистентность
    # ------------------------------------------------------------------

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
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            payload = list(self._schedules.values())
            self._file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
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
            duration_sec: Длительность записи в секундах (>0).
            label: Опциональная метка/описание.

        Returns:
            Словарь с полями задания.
        """
        if duration_sec <= 0:
            raise ValueError("duration_sec должен быть положительным числом")

        # Парсим start_time и нормализуем до UTC ISO строки
        try:
            dt = _parse_datetime(start_time)
        except Exception as exc:
            raise ValueError(f"Неверный формат start_time: {exc}") from exc

        schedule_id = str(uuid.uuid4())
        entry: dict[str, Any] = {
            "id": schedule_id,
            "start_time": dt.isoformat(),
            "duration_sec": int(duration_sec),
            "label": str(label),
            "status": STATUS_PENDING,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        with self._lock:
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
        """Возвращает все записи расписания (все статусы), отсортированные по start_time."""
        with self._lock:
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
