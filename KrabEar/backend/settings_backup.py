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

# Чувствительные поля — никогда не пишутся в бэкап.
# Публичное имя SENSITIVE_FIELDS позволяет settings_service.py
# импортировать этот набор вместо дублирования (W929 F4).
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
    # Wave 20 additions — MED credential leak: llm_api_key is sent as
    # Authorization: Bearer <key> in service.py + llm_ops_service.py;
    # smtp_password is a cleartext SMTP credential; ipc_signing_secret is
    # the HMAC-SHA256 shared secret for IPC request authentication.
    "llm_api_key",
    "smtp_password",
    "ipc_signing_secret",
})
# Legacy alias kept for any internal references within this module.
_SENSITIVE = SENSITIVE_FIELDS


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

        backup_id = имя файла без `.json`
        (напр. `20240425T123456_789012Z_auto`; microsecond precision prevents
        same-second same-reason collisions).

        Директория создаётся с правами 0o700, файл — 0o600:
        credentials-bearing backups не должны быть world-readable.
        """
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Force perms on the dir even if it pre-existed with wider bits.
        try:
            os.chmod(self._dir, 0o700)
        except OSError:
            pass  # best-effort; no reason to abort the backup

        reason_safe = reason.replace(" ", "_")[:40]
        # Microsecond precision ("%f" = 6 digits) prevents silent overwrites
        # when two backups are requested within the same second with the same
        # reason (e.g. rapid concurrent set_settings calls).
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        backup_id = f"{ts}_{reason_safe}"
        out_path = self._dir / f"{backup_id}.json"

        safe = {k: v for k, v in settings.items() if k not in _SENSITIVE}
        # Write to a .tmp sidecar then rename for atomicity, then lock down perms.
        tmp_path = out_path.with_suffix(".tmp")
        try:
            fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(safe, fh, ensure_ascii=False, indent=2)
            tmp_path.rename(out_path)
        except Exception:
            # Clean up orphaned .tmp on failure; re-raise so callers know.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

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

        # W929 F1: path-traversal guard — reject any backup_id that escapes
        # the backup directory (e.g. "../../etc/passwd").
        resolved = (self._dir / f"{backup_id}.json").resolve()
        root = self._dir.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"Недопустимый backup_id: {backup_id!r}")

        backup_path = resolved
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

        # New format (Wave 20): {ts}_{reason}
        # where ts = YYYYMMDDTHHMMSSµµµµµµZ (23 chars, e.g. 20240425T123456_789012Z).
        # Legacy format:          YYYYMMDDTHHMMSSz    (16 chars).
        # We detect by checking for the underscore-after-seconds pattern.
        _TS_LEN_NEW = 23  # YYYYMMDDTHHMMSSµµµµµµZ (e.g. 20240425T123456_789012Z)
        _TS_LEN_OLD = 16  # YYYYMMDDTHHMMSSz        (e.g. 20240425T123456Z)
        # Choose whichever ts length fits; fall back to old length for legacy files.
        if len(backup_id) > _TS_LEN_NEW and backup_id[_TS_LEN_NEW] == "_":
            ts_str = backup_id[:_TS_LEN_NEW]
            reason = backup_id[_TS_LEN_NEW + 1:]
        elif len(backup_id) > _TS_LEN_OLD and backup_id[_TS_LEN_OLD] == "_":
            ts_str = backup_id[:_TS_LEN_OLD]
            reason = backup_id[_TS_LEN_OLD + 1:]
        else:
            ts_str = backup_id[:_TS_LEN_OLD] if len(backup_id) >= _TS_LEN_OLD else backup_id
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
