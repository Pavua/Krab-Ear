"""SettingsBackup — rolling backup of KrabEar settings before each write.

Хранит до MAX_BACKUPS (10) снимков в
  ~/Library/Application Support/KrabEar/settings_backups/{ts}_{reason}.json

Публичный API:
  create_backup(settings_dict, reason="auto") → backup_id (str)
  list_backups(limit=10)                       → list[dict]
  restore_backup(backup_id)                    → dict  (загруженные настройки)
  get_backup_dir()                             → Path
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

MAX_BACKUPS: int = 10

# Единый канонический список чувствительных полей — используется как SettingsBackup,
# так и SettingsService (export) и AutoBackupManager (copy redaction).
# W897: объединено из трёх разрозненных frozenset-ов в одно место — source of truth.
SENSITIVE_FIELDS: frozenset[str] = frozenset({
    "voice_gateway_api_key",
    "hf_token",
    "rest_api_key",
    "lm_studio_api_key",
    # Wave 58 follow-up — defense-in-depth from Wave 47 B2 security audit:
    # these keys exist as Settings class fields but users can also override
    # via IPC `set_settings` which persists to settings.json — backup must
    # redact them regardless of where they came from.
    "telnyx_api_key",
    "twilio_account_sid",
    "twilio_auth_token",
    "sentry_dsn",
    "stt_gigaam_hf_token",
})

# Private alias для обратной совместимости внутри модуля
_SENSITIVE: frozenset[str] = SENSITIVE_FIELDS


def _default_backup_dir() -> Path:
    """Возвращает путь к директории бэкапов.

    В production — ~/Library/Application Support/KrabEar/settings_backups/.
    Можно переопределить через переменную окружения KRAB_EAR_SETTINGS_BACKUP_DIR
    (используется в тестах).
    """
    env_override = os.environ.get("KRAB_EAR_SETTINGS_BACKUP_DIR", "")
    if env_override:
        return Path(env_override)
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "KrabEar"
        / "settings_backups"
    )


class SettingsBackup:
    """Управляет rolling-бэкапами настроек (не более MAX_BACKUPS файлов)."""

    def __init__(self, backup_dir: Path | None = None) -> None:
        self._dir: Path = backup_dir if backup_dir is not None else _default_backup_dir()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_backup_dir(self) -> Path:
        """Возвращает Path к директории бэкапов."""
        return self._dir

    def create_backup(
        self,
        settings: dict[str, Any],
        reason: str = "auto",
    ) -> str:
        """Сохраняет снимок настроек, обрезает лишние файлы, возвращает backup_id.

        backup_id = имя файла без `.json` (напр. `20240425T123456Z_auto`).
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        reason_safe = reason.replace(" ", "_")[:40]
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_id = f"{ts}_{reason_safe}"
        out_path = self._dir / f"{backup_id}.json"

        safe = {k: v for k, v in settings.items() if k not in _SENSITIVE}
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(safe, fh, ensure_ascii=False, indent=2)

        _log.info(
            "settings_backup: created %s (%d keys)",
            out_path.name,
            len(safe),
        )
        self._prune()
        return backup_id

    def list_backups(self, limit: int = 10) -> list[dict[str, Any]]:
        """Возвращает список бэкапов, отсортированных от новых к старым.

        Каждый элемент:
          {backup_id, ts, reason, file_size, settings_count_keys}
        """
        if not self._dir.exists():
            return []

        files = sorted(
            self._dir.glob("*.json"),
            key=lambda p: p.name,
            reverse=True,
        )

        result: list[dict[str, Any]] = []
        for f in files[:limit]:
            info = self._parse_file_info(f)
            if info is not None:
                result.append(info)
        return result

    def restore_backup(self, backup_id: str) -> dict[str, Any]:
        """Загружает настройки из указанного бэкапа и возвращает dict.

        Raises:
            FileNotFoundError: если бэкап не существует.
            ValueError: если файл содержит невалидный JSON или не является dict.
        """
        if not backup_id:
            raise ValueError("backup_id не может быть пустым")

        backup_path = self._dir / f"{backup_id}.json"
        if not backup_path.exists():
            raise FileNotFoundError(
                f"Бэкап настроек не найден: {backup_path}"
            )

        try:
            with backup_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Невалидный JSON в файле бэкапа '{backup_id}': {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"Файл бэкапа '{backup_id}' должен содержать JSON-объект"
            )

        _log.info(
            "settings_backup: restored %s (%d keys)",
            backup_id,
            len(data),
        )
        return data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Удаляет старые бэкапы, оставляя не более MAX_BACKUPS файлов."""
        files = sorted(
            self._dir.glob("*.json"),
            key=lambda p: p.name,
        )
        excess = len(files) - MAX_BACKUPS
        for old in files[:excess]:
            try:
                old.unlink()
                _log.debug("settings_backup: pruned %s", old.name)
            except OSError as exc:
                _log.warning("settings_backup: не удалось удалить %s: %s", old.name, exc)

    def _parse_file_info(self, path: Path) -> dict[str, Any] | None:
        """Парсит метаданные файла бэкапа без полной загрузки содержимого."""
        backup_id = path.stem  # filename without .json

        # Формат: {ts}_{reason}  (ts всегда 16 символов: YYYYMMDDTHHMMSSz)
        _TS_LEN = 16
        if len(backup_id) > _TS_LEN + 1:
            ts_str = backup_id[:_TS_LEN]
            reason = backup_id[_TS_LEN + 1:]
        else:
            ts_str = backup_id[:_TS_LEN] if len(backup_id) >= _TS_LEN else backup_id
            reason = "unknown"

        # Размер файла
        try:
            file_size = path.stat().st_size
        except OSError:
            file_size = 0

        # Количество ключей — читаем только верхний уровень JSON
        settings_count_keys = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                settings_count_keys = len(data)
        except (OSError, json.JSONDecodeError):
            pass

        return {
            "backup_id": backup_id,
            "ts": ts_str,
            "reason": reason,
            "file_size": file_size,
            "settings_count_keys": settings_count_keys,
        }
