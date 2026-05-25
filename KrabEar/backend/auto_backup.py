"""Автоматическое резервное копирование истории Krab Ear.

AutoBackupManager выполняет резервное копирование оппортунистически —
при вызове check_and_backup() — без фоновых потоков.
Настройки хранятся в файле auto_backup_meta.json в директории backups/.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.AutoBackup")

# Настройки по умолчанию
AUTO_BACKUP_INTERVAL_HOURS: int = 24
AUTO_BACKUP_MAX_COPIES: int = 7


class AutoBackupManager:
    """Управляет автоматическими резервными копиями истории.

    Thread-safe. Не создаёт фоновых потоков — копирование происходит
    только при явном вызове check_and_backup().
    """

    META_FILENAME = "auto_backup_meta.json"

    def __init__(
        self,
        store: Any,
        interval_hours: int = AUTO_BACKUP_INTERVAL_HOURS,
        max_copies: int = AUTO_BACKUP_MAX_COPIES,
        enabled: bool = True,
    ) -> None:
        """
        Args:
            store: StateStore — источник файлов для резервного копирования.
            interval_hours: минимальный интервал между бэкапами (часы).
            max_copies: максимальное количество хранимых бэкапов.
            enabled: если False — check_and_backup() ничего не делает.
        """
        self.store = store
        self.interval_hours = interval_hours
        self.max_copies = max_copies
        self.enabled = enabled
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Вспомогательные свойства
    # ------------------------------------------------------------------

    @property
    def backups_dir(self) -> Path:
        return Path(self.store.data_dir) / "backups"

    @property
    def _meta_path(self) -> Path:
        return self.backups_dir / self.META_FILENAME

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict:
        if self._meta_path.exists():
            try:
                return json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_backup_ts": None, "backup_count": 0}

    def _save_meta(self, meta: dict) -> None:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _list_auto_backups(self) -> list[Path]:
        """Возвращает список папок авто-бэкапов, отсортированных по имени (старые → новые)."""
        if not self.backups_dir.exists():
            return []
        dirs = sorted(
            d for d in self.backups_dir.iterdir()
            if d.is_dir() and d.name.startswith("auto_backup_")
        )
        return dirs

    def _prune_old_backups(self) -> int:
        """Удаляет старые авто-бэкапы, оставляя не более max_copies.

        Returns:
            Количество удалённых копий.
        """
        backups = self._list_auto_backups()
        to_delete = backups[: max(0, len(backups) - self.max_copies)]
        for d in to_delete:
            try:
                shutil.rmtree(d, ignore_errors=True)
                logger.info("Удалён старый авто-бэкап: %s", d)
            except Exception as exc:
                logger.warning("Не удалось удалить авто-бэкап %s: %s", d, exc)
        return len(to_delete)

    def _do_backup(self) -> dict:
        """Выполняет резервное копирование и возвращает метаданные."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self.backups_dir / f"auto_backup_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        files_to_backup = [
            self.store.history_path,
            self.store.tombstones_path,
            self.store.status_path,
            self.store.settings_path,
        ]

        total_bytes = 0
        copied_files = []
        for src in files_to_backup:
            if Path(src).exists():
                dst = backup_dir / Path(src).name
                shutil.copy2(src, dst)
                total_bytes += dst.stat().st_size
                copied_files.append(Path(src).name)

        entries = 0
        try:
            entries = self.store.count_active_items()
        except Exception:
            pass

        meta = {
            "backup_ts": ts,
            "entries": entries,
            "size_bytes": total_bytes,
            "files": copied_files,
            "auto": True,
        }
        (backup_dir / "backup_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        size_mb = round(total_bytes / (1024 * 1024), 3)
        logger.info(
            "Авто-бэкап создан: %s (%s МБ, %d записей)", backup_dir, size_mb, entries
        )
        return {
            "backup_path": str(backup_dir),
            "backup_ts": ts,
            "size_mb": size_mb,
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def check_and_backup(self) -> dict:
        """Создаёт резервную копию, если с последнего бэкапа прошло > interval_hours.

        Returns:
            dict с ключами:
                backed_up (bool): True если бэкап был выполнен
                skipped_reason (str | None): причина пропуска или None
                backup_path (str | None): путь к бэкапу или None
        """
        if not self.enabled:
            return {"backed_up": False, "skipped_reason": "disabled", "backup_path": None}

        with self._lock:
            meta = self._load_meta()
            last_ts_str: str | None = meta.get("last_backup_ts")

            if last_ts_str is not None:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    # Нормализуем к UTC
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    elapsed_hours = (now - last_dt).total_seconds() / 3600
                    if elapsed_hours < self.interval_hours:
                        return {
                            "backed_up": False,
                            "skipped_reason": "too_soon",
                            "backup_path": None,
                            "hours_since_last": round(elapsed_hours, 2),
                            "hours_until_next": round(self.interval_hours - elapsed_hours, 2),
                        }
                except Exception:
                    pass  # Повреждённая метадата — делаем бэкап

            result = self._do_backup()
            self._prune_old_backups()

            meta["last_backup_ts"] = datetime.now(timezone.utc).isoformat()
            meta["backup_count"] = meta.get("backup_count", 0) + 1
            self._save_meta(meta)

            return {
                "backed_up": True,
                "skipped_reason": None,
                "backup_path": result["backup_path"],
                "backup_ts": result["backup_ts"],
                "size_mb": result["size_mb"],
                "entries": result["entries"],
            }

    def get_auto_backup_status(self) -> dict:
        """Возвращает статус авто-резервного копирования.

        Returns:
            dict с ключами:
                enabled (bool)
                last_backup_ts (str | None): ISO-8601 время последнего бэкапа
                next_backup_ts (str | None): ISO-8601 время следующего запланированного бэкапа
                total_backups (int): количество авто-бэкапов на диске
                interval_hours (int)
                max_copies (int)
                backups_dir (str)
        """
        with self._lock:
            meta = self._load_meta()
            last_ts_str: str | None = meta.get("last_backup_ts")
            next_ts_str: str | None = None

            if last_ts_str is not None:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    from datetime import timedelta
                    next_dt = last_dt + timedelta(hours=self.interval_hours)
                    next_ts_str = next_dt.isoformat()
                except Exception:
                    pass

            total_backups = len(self._list_auto_backups())

            return {
                "enabled": self.enabled,
                "last_backup_ts": last_ts_str,
                "next_backup_ts": next_ts_str,
                "total_backups": total_backups,
                "interval_hours": self.interval_hours,
                "max_copies": self.max_copies,
                "backups_dir": str(self.backups_dir),
            }
