"""PrivacyAuditLogger — singleton для записи событий режима конфиденциальности.

Каждая запись — строка NDJSON в ~/Library/Application Support/KrabEar/privacy_audit.log.
Все операции записи защищены fcntl.flock (как в state_store.py).

Tamper detection (W952 F-3 HIGH): записи содержат HMAC-SHA256 хеш-цепочку.
- Каждая запись включает ``prev_hash`` (хеш предыдущей записи) и ``entry_hash``
  (HMAC-SHA256 от ключа и содержимого текущей записи).
- Ключ хранится в ``<data_dir>/privacy_audit.key`` (режим 0600), генерируется
  однократно при первом запуске; никогда не ротируется (ротация обрывает цепочку).
- ``verify_chain()`` проходит лог от начала до конца и проверяет каждую ссылку.
- Обратная совместимость: старые записи без ``prev_hash``/``entry_hash`` не ломают
  цепочку — они обрабатываются как «точка рестарта» (``prev_hash = None``).
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.PrivacyAudit")

_DEFAULT_LOG_PATH = (
    Path.home() / "Library" / "Application Support" / "KrabEar" / "privacy_audit.log"
)

# Имя файла ключа относительно директории лога
_KEY_FILENAME = "privacy_audit.key"


def _load_or_create_key(key_path: Path) -> bytes:
    """Загружает ключ из файла или создаёт новый (однократно, write-once).

    Файл создаётся с правами 0o600 (чтение/запись только владельца).
    Если файл уже существует — считывает и возвращает его содержимое.
    """
    if key_path.exists():
        try:
            raw = key_path.read_bytes()
            if len(raw) >= 16:  # санити-чек: минимум 16 байт
                return raw
            logger.warning(
                "PrivacyAudit: ключ подозрительно короткий (%d б), пересоздаём",
                len(raw),
            )
        except Exception:
            logger.exception(
                "PrivacyAudit: не удалось прочитать ключ из %s, пересоздаём", key_path
            )

    # Генерируем новый 32-байтный ключ
    new_key = os.urandom(32)
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # Записываем через временный путь и rename для атомарности
        tmp_path = key_path.with_suffix(".key.tmp")
        tmp_path.write_bytes(new_key)
        # Устанавливаем режим 0o600 до rename
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        tmp_path.rename(key_path)
    except Exception:
        logger.exception("PrivacyAudit: не удалось сохранить ключ в %s", key_path)
    return new_key


def _compute_entry_hash(
    secret_key: bytes,
    prev_hash: str | None,
    entry_without_hash: dict[str, Any],
) -> str:
    """Вычисляет HMAC-SHA256 хеш для записи.

    Args:
        secret_key:          32-байтный секретный ключ.
        prev_hash:           hex-digest предыдущей записи или None (начало цепочки).
        entry_without_hash:  словарь записи БЕЗ полей prev_hash и entry_hash.

    Returns:
        HMAC-SHA256 hex-digest строкой.
    """
    # Канонический JSON: sort_keys=True, ensure_ascii=False, без лишних пробелов
    canonical = json.dumps(
        entry_without_hash, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    prev_part = prev_hash if prev_hash is not None else "null"
    message = (prev_part + "|" + canonical).encode("utf-8")
    return hmac.new(secret_key, message, hashlib.sha256).hexdigest()


class PrivacyAuditLogger:
    """Singleton для записи событий режима конфиденциальности в NDJSON-лог.

    Поддерживает HMAC-SHA256 хеш-цепочку для обнаружения несанкционированных
    изменений файла лога (W952 F-3 HIGH).
    """

    _instance: "PrivacyAuditLogger | None" = None

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path: Path = log_path if log_path is not None else _DEFAULT_LOG_PATH
        self._ensure_parent()

        # Загрузка/создание секретного ключа
        key_path = self._log_path.parent / _KEY_FILENAME
        self._secret_key: bytes = _load_or_create_key(key_path)

        # Инициализация кончика цепочки из существующего лога
        self._last_hash: str | None = self._read_chain_tip()

    # ------------------------------------------------------------------
    # Singleton API
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls, log_path: Path | None = None) -> "PrivacyAuditLogger":
        """Возвращает singleton-экземпляр (создаёт при первом вызове)."""
        if cls._instance is None:
            cls._instance = cls(log_path=log_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Сбрасывает singleton — используется только в тестах."""
        cls._instance = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_parent(self) -> None:
        """Создаёт родительскую директорию если она отсутствует."""
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_chain_tip(self) -> str | None:
        """Считывает entry_hash последней записи из существующего лога.

        Если файл отсутствует, пуст или последняя запись — legacy (без entry_hash),
        возвращает None (цепочка начинается заново).
        """
        if not self._log_path.exists():
            return None
        last_hash: str | None = None
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if "entry_hash" in entry:
                                last_hash = entry["entry_hash"]
                            else:
                                # Legacy-запись без хеша: после неё цепочка
                                # начинается заново (prev_hash = None)
                                last_hash = None
                        except json.JSONDecodeError:
                            pass
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAudit: ошибка чтения кончика цепочки")
        return last_hash

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

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
        # Формируем тело записи без хеш-полей
        entry_body: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "action": action,
            "details": details or {},
        }

        # Хеш-цепочка
        prev_hash = self._last_hash
        entry_hash = _compute_entry_hash(self._secret_key, prev_hash, entry_body)

        # Финальная запись включает оба хеш-поля
        entry: dict[str, Any] = {
            **entry_body,
            "prev_hash": prev_hash,
            "entry_hash": entry_hash,
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
            # Обновляем кончик цепочки после успешной записи
            self._last_hash = entry_hash
        except Exception:
            logger.exception(
                "PrivacyAuditLogger: ошибка записи события category=%s action=%s",
                category,
                action,
            )

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

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
        """Удаляет файл лога. Идемпотентно — не ошибается если файл не существует.

        После очистки цепочка хешей сбрасывается (следующая запись начнёт новую
        цепочку с prev_hash=None).
        """
        try:
            self._log_path.unlink(missing_ok=True)
            self._last_hash = None
        except Exception:
            logger.exception("PrivacyAuditLogger: ошибка удаления лога")

    def verify_chain(self) -> dict[str, Any]:
        """Проверяет целостность HMAC-SHA256 хеш-цепочки.

        Алгоритм:
        - Читает записи последовательно от начала.
        - Для каждой записи с ``entry_hash`` пересчитывает хеш и сравнивает
          с сохранённым значением.
        - Также проверяет что ``prev_hash`` записи совпадает с хешем предыдущей.
        - Legacy-записи без ``entry_hash`` пропускаются; после них цепочка
          считается «перезапущенной» (ожидаемый prev_hash сбрасывается в None).

        Returns:
            ``{"valid": True, "first_broken_index": None, "checked": N}``
            или
            ``{"valid": False, "first_broken_index": int, "reason": str, "checked": N}``.
        """
        if not self._log_path.exists():
            return {"valid": True, "first_broken_index": None, "checked": 0}

        entries_raw: list[str] = []
        try:
            with self._log_path.open("r", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    entries_raw = [ln.rstrip("\n") for ln in fh if ln.strip()]
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            logger.exception("PrivacyAudit: ошибка чтения при верификации цепочки")
            return {
                "valid": False,
                "first_broken_index": None,
                "reason": "read_error",
                "checked": 0,
            }

        prev_hash: str | None = None
        checked = 0

        for idx, raw_line in enumerate(entries_raw):
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError:
                return {
                    "valid": False,
                    "first_broken_index": idx,
                    "reason": "json_decode_error",
                    "checked": checked,
                }

            if "entry_hash" not in entry:
                # Legacy-запись: пропускаем, сбрасываем prev_hash
                prev_hash = None
                checked += 1
                continue

            stored_entry_hash: str = entry["entry_hash"]
            stored_prev_hash: str | None = entry.get("prev_hash")

            # Проверяем что prev_hash в записи совпадает с ожидаемым
            if stored_prev_hash != prev_hash:
                return {
                    "valid": False,
                    "first_broken_index": idx,
                    "reason": "prev_hash_mismatch",
                    "checked": checked,
                }

            # Пересчитываем entry_hash из тела записи (без хеш-полей)
            entry_body = {
                k: v
                for k, v in entry.items()
                if k not in ("prev_hash", "entry_hash")
            }
            expected_hash = _compute_entry_hash(self._secret_key, prev_hash, entry_body)

            if not hmac.compare_digest(expected_hash, stored_entry_hash):
                return {
                    "valid": False,
                    "first_broken_index": idx,
                    "reason": "entry_hash_mismatch",
                    "checked": checked,
                }

            prev_hash = stored_entry_hash
            checked += 1

        return {"valid": True, "first_broken_index": None, "checked": checked}


# Удобная точка доступа к singleton
def get_privacy_audit_logger(log_path: Path | None = None) -> PrivacyAuditLogger:
    """Возвращает глобальный singleton PrivacyAuditLogger."""
    return PrivacyAuditLogger.get_instance(log_path=log_path)
