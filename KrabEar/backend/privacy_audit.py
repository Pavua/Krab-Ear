"""PrivacyAuditLogger — singleton для записи событий режима конфиденциальности.

Каждая запись — строка NDJSON в ~/Library/Application Support/KrabEar/privacy_audit.log.
Все операции записи защищены fcntl.flock (как в state_store.py).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.PrivacyAudit")

_DEFAULT_LOG_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"
)


class PrivacyAuditLogger:
    """Singleton для записи событий режима конфиденциальности в NDJSON-лог."""

    _instance: "PrivacyAuditLogger | None" = None
    _instance_lock: threading.Lock = threading.Lock()

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path: Path = log_path if log_path is not None else _DEFAULT_LOG_PATH
        self._ensure_parent()

    @classmethod
    def get_instance(cls, log_path: Path | None = None) -> "PrivacyAuditLogger":
        """Возвращает singleton-экземпляр (создаёт при первом вызове).

        Использует double-checked locking для thread-safety: быстрый путь
        (без lock) для уже инициализированного экземпляра, медленный путь
        (под lock с повторной проверкой) для первого создания.
        """
        # Быстрый путь: экземпляр уже создан — не берём lock.
        if cls._instance is not None:
            return cls._instance
        # Медленный путь: берём lock и повторно проверяем перед созданием.
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(log_path=log_path)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Сбрасывает singleton — используется только в тестах."""
        with cls._instance_lock:
            cls._instance = None

    def _ensure_parent(self) -> None:
        """Создаёт родительскую директорию если она отсутствует."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(
        self,
        category: str,
        action: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Дописывает одну NDJSON-строку в лог.

        Args:
            category: категория события (sentry, translation, …).
            action:   действие (blocked, forced_offline, …).
            details:  дополнительные данные (опционально).
        """
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "action": action,
            "details": details or {},
        }
        line = json.dumps(entry, ensure_ascii=False) + "\n"

        try:
            self._ensure_parent()
            # Открываем в режиме append (создаём файл если нет).
            with self._log_path.open("a", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line)
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception(
                "PrivacyAuditLogger: ошибка записи события category=%s action=%s",
                category,
                action,
            )

    def read_entries(self, limit: int = 100) -> list[dict[str, Any]]:
        """Читает последние *limit* записей из лога.

        Args:
            limit: максимальное число записей (от самых последних).

        Returns:
            Список словарей с записями (порядок: от старых к новым).
            Пустой список если файл не существует.
        """
        if not self._log_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    lines = fh.readlines()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning(
                        "PrivacyAuditLogger: не удалось разобрать строку: %r", line
                    )
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка чтения лога")

        # Возвращаем последние *limit* записей
        return entries[-limit:] if limit and len(entries) > limit else entries

    def total_count(self) -> int:
        """Возвращает общее число записей в лог-файле (без ограничения limit)."""
        if not self._log_path.exists():
            return 0
        count = 0
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for line in fh:
                        if line.strip():
                            count += 1
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка подсчёта записей")
        return count

    def clear(self) -> None:
        """Удаляет файл лога. Идемпотентно — не ошибается если файл не существует."""
        try:
            self._log_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка удаления лога")


# Удобная точка доступа к singleton
def get_privacy_audit_logger(log_path: Path | None = None) -> PrivacyAuditLogger:
    """Возвращает глобальный singleton PrivacyAuditLogger."""
    return PrivacyAuditLogger.get_instance(log_path=log_path)
