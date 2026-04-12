"""Audit logger для IPC-методов Krab Ear.

Каждый вызов IPC-метода записывается в append-only NDJSON файл.
Файлы ротируются ежедневно, хранятся последние 7 дней.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.AuditLogger")

# Методы, параметры которых не логируются (чувствительные данные)
_SENSITIVE_METHODS = frozenset({
    "set_settings",
    "set_notification_preferences",
    "apply_profile_preset",
})

_KEEP_DAYS = 7


class AuditLogger:
    """Логирует IPC-запросы в append-only NDJSON файлы с ежедневной ротацией."""

    def __init__(self, data_dir: str | os.PathLike) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_date: str = ""
        self._file_handle = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def log_request(
        self,
        method: str,
        params: dict,
        result: dict,
        duration_ms: float,
        client_info: dict | None = None,
    ) -> None:
        """Записывает одну запись аудита."""
        ts = datetime.now(timezone.utc).isoformat()
        success = bool(result.get("ok", False)) if isinstance(result, dict) else False

        entry: dict[str, Any] = {
            "ts": ts,
            "method": method,
            "params_keys": sorted(params.keys()) if (params and method not in _SENSITIVE_METHODS) else [],
            "success": success,
            "duration_ms": round(duration_ms, 2),
        }
        if client_info:
            entry["client_info"] = client_info

        line = json.dumps(entry, ensure_ascii=False)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            self._rotate_if_needed(today)
            try:
                assert self._file_handle is not None
                self._file_handle.write(line + "\n")
                self._file_handle.flush()
            except Exception:
                logger.exception("Ошибка записи в audit log")

        self._cleanup_old_files()

    def get_audit_log(
        self,
        limit: int = 100,
        method_filter: str | None = None,
    ) -> list[dict]:
        """Возвращает последние записи из всех доступных файлов аудита."""
        files = sorted(self._data_dir.glob("audit_*.ndjson"), reverse=True)
        entries: list[dict] = []

        for path in files:
            if len(entries) >= limit:
                break
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if method_filter and entry.get("method") != method_filter:
                        continue
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
            except Exception:
                logger.exception("Ошибка чтения audit log файла %s", path)

        return entries

    def close(self) -> None:
        """Закрывает текущий файловый дескриптор."""
        with self._lock:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
                self._file_handle = None

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _audit_path(self, date: str) -> Path:
        return self._data_dir / f"audit_{date}.ndjson"

    def _rotate_if_needed(self, today: str) -> None:
        """Открывает новый файл при смене даты (вызывается под self._lock)."""
        if today != self._current_date:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
            path = self._audit_path(today)
            self._file_handle = open(path, "a", encoding="utf-8")
            self._current_date = today

    def _cleanup_old_files(self) -> None:
        """Удаляет файлы аудита старше _KEEP_DAYS дней."""
        files = sorted(self._data_dir.glob("audit_*.ndjson"))
        if len(files) <= _KEEP_DAYS:
            return
        for old_file in files[: len(files) - _KEEP_DAYS]:
            try:
                old_file.unlink()
            except Exception:
                logger.warning("Не удалось удалить старый audit log: %s", old_file)
