"""Инструмент миграции данных Krab Ear между форматами/версиями.

Поддерживаемые миграции:
- v1.0 → v2.0: добавляет поля tags, favorite, annotation к записям, где они отсутствуют.

Всегда создаёт резервную копию данных перед миграцией.
"""

from __future__ import annotations

import fcntl
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.DataMigrator")

# Текущая поддерживаемая версия схемы
LATEST_VERSION = "2.0"

# Поля v2.0, которых не было в v1.0
_V2_DEFAULTS: dict[str, Any] = {
    "tags": [],
    "favorite": False,
    "annotation": "",
}


@dataclass
class MigrationResult:
    """Результат миграции данных."""

    from_version: str
    to_version: str
    items_migrated: int
    items_skipped: int
    backup_path: str


def _detect_version_from_items(items: list[dict[str, Any]]) -> str:
    """Определяет версию схемы по набору записей истории.

    Логика:
    - Если хотя бы одна запись не имеет полей tags/favorite — это v1.0.
    - Если все записи содержат эти поля или история пуста — v2.0.
    """
    if not items:
        return LATEST_VERSION

    for item in items:
        if "tags" not in item or "favorite" not in item:
            return "1.0"

    return LATEST_VERSION


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    """Читает NDJSON-файл и возвращает список объектов, пропуская некорректные строки."""
    if not path.exists():
        return []

    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Некорректный JSON в %s строка %d — пропущена", path.name, line_no)
    return items


def _read_tombstones(tombstones_path: Path) -> set[str]:
    """Возвращает множество ID удалённых записей."""
    deleted: set[str] = set()
    for record in _read_ndjson(tombstones_path):
        item_id = str(record.get("id", "")).strip()
        if item_id:
            deleted.add(item_id)
    return deleted


def _load_active_items(data_dir: Path) -> list[dict[str, Any]]:
    """Возвращает только активные (не удалённые) записи истории."""
    history_path = data_dir / "history.ndjson"
    tombstones_path = data_dir / "history_tombstones.ndjson"

    all_items = _read_ndjson(history_path)
    deleted = _read_tombstones(tombstones_path)

    return [item for item in all_items if str(item.get("id", "")).strip() not in deleted]


class DataMigrator:
    """Мигратор данных Krab Ear между версиями формата истории.

    Принимает ``data_dir`` при инициализации — это единственная разрешённая
    директория для IPC-обработчиков (``handle_run_migration``,
    ``handle_check_migration``, ``handle_rollback_migration``).

    Параметр ``data_dir`` из входящего IPC-запроса ИГНОРИРУЕТСЯ: принимать
    произвольный путь от клиента позволяло бы записывать/бэкапить файлы за
    пределами данных приложения (path-write уязвимость, W1761).

    Внутренние методы (``migrate``, ``get_schema_version`` и т.д.) по-прежнему
    принимают ``data_dir: Path`` как аргумент — это необходимо для юнит-тестов
    и startup-миграции в ``BackendService.__init__``, которые передают явный путь.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        # Закреплённый путь к данным; None = не задан (legacy-режим без IPC)
        self._data_dir: Path | None = Path(data_dir).resolve() if data_dir is not None else None

    # ------------------------------------------------------------------
    # Публичные методы (принимают data_dir явным аргументом)
    # ------------------------------------------------------------------

    def get_schema_version(self, data_dir: Path) -> str:
        """Определяет текущую версию схемы данных в указанной директории.

        Args:
            data_dir: путь к директории данных Krab Ear.

        Returns:
            Строка версии, например "1.0" или "2.0".
        """
        data_dir = Path(data_dir)
        active_items = _load_active_items(data_dir)
        return _detect_version_from_items(active_items)

    def check_migration_needed(self, data_dir: Path) -> bool:
        """Проверяет, требуется ли миграция данных.

        Args:
            data_dir: путь к директории данных Krab Ear.

        Returns:
            True если текущая версия < LATEST_VERSION.
        """
        current = self.get_schema_version(Path(data_dir))
        return current != LATEST_VERSION

    def get_migration_plan(self, data_dir: Path) -> list[str]:
        """Описывает список изменений, которые будут применены при миграции.

        Args:
            data_dir: путь к директории данных Krab Ear.

        Returns:
            Список строк с описанием шагов миграции.
        """
        data_dir = Path(data_dir)
        current = self.get_schema_version(data_dir)

        if current == LATEST_VERSION:
            return ["Миграция не требуется: схема уже версии " + LATEST_VERSION]

        active_items = _load_active_items(data_dir)
        needs_migration = sum(
            1 for item in active_items
            if "tags" not in item or "favorite" not in item
        )

        plan: list[str] = [
            f"Текущая версия схемы: {current}",
            f"Целевая версия схемы: {LATEST_VERSION}",
            f"Записей в истории (активных): {len(active_items)}",
            f"Записей, требующих обновления: {needs_migration}",
            "Создать резервную копию history.ndjson и связанных файлов перед миграцией",
        ]

        if needs_migration > 0:
            plan.append(
                f"v1.0 → v2.0: добавить поля tags=[], favorite=false, annotation=\"\" "
                f"к {needs_migration} записям"
            )

        return plan

    def migrate(
        self,
        data_dir: Path,
        target_version: str = "2.0",
    ) -> MigrationResult:
        """Выполняет миграцию данных к указанной версии.

        Всегда создаёт резервную копию перед миграцией.

        Args:
            data_dir: путь к директории данных Krab Ear.
            target_version: целевая версия схемы (по умолчанию "2.0").

        Returns:
            MigrationResult с информацией о результате миграции.

        Raises:
            ValueError: если целевая версия не поддерживается.
        """
        data_dir = Path(data_dir)

        if target_version != "2.0":
            raise ValueError(f"Неподдерживаемая целевая версия: {target_version!r}. Поддерживается только '2.0'.")

        current = self.get_schema_version(data_dir)
        backup_path = self._create_backup(data_dir)

        if current == target_version:
            logger.info("Миграция не требуется: текущая версия %s == целевой %s", current, target_version)
            return MigrationResult(
                from_version=current,
                to_version=target_version,
                items_migrated=0,
                items_skipped=0,
                backup_path=backup_path,
            )

        # Миграция v1.0 → v2.0
        if current == "1.0" and target_version == "2.0":
            return self._migrate_v1_to_v2(data_dir, backup_path)

        # Неизвестный путь миграции — ничего не делаем, возвращаем статус
        logger.warning("Неизвестный путь миграции: %s → %s", current, target_version)
        return MigrationResult(
            from_version=current,
            to_version=target_version,
            items_migrated=0,
            items_skipped=0,
            backup_path=backup_path,
        )

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def _require_configured_data_dir(self) -> Path:
        """Возвращает закреплённый data_dir или бросает RuntimeError.

        Используется IPC-обработчиками вместо принятия пути от клиента:
        произвольный path из IPC-запроса открывал бы path-write уязвимость
        (запись/бэкап файлов вне директории данных приложения).
        """
        if self._data_dir is None:
            raise RuntimeError(
                "DataMigrator не инициализирован с data_dir. "
                "IPC-обработчики работают только с закреплённой директорией данных."
            )
        return self._data_dir

    def handle_check_migration(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик check_migration.

        Параметр ``data_dir`` из params игнорируется (безопасность W1761):
        обработчик использует только директорию, заданную при инициализации.

        Returns:
            migration_needed (bool), current_version (str), target_version (str), plan (list[str])
        """
        data_dir = self._require_configured_data_dir()
        needed = self.check_migration_needed(data_dir)
        current = self.get_schema_version(data_dir)
        plan = self.get_migration_plan(data_dir)

        return {
            "migration_needed": needed,
            "current_version": current,
            "target_version": LATEST_VERSION,
            "plan": plan,
        }

    def handle_run_migration(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик run_migration.

        Параметр ``data_dir`` из params игнорируется (безопасность W1761):
        обработчик использует только директорию, заданную при инициализации.

        Params:
            target_version (str, optional): целевая версия (default "2.0").

        Returns:
            from_version, to_version, items_migrated, items_skipped, backup_path
        """
        data_dir = self._require_configured_data_dir()
        target = str(params.get("target_version", LATEST_VERSION)).strip() or LATEST_VERSION
        result = self.migrate(data_dir, target_version=target)

        return {
            "from_version": result.from_version,
            "to_version": result.to_version,
            "items_migrated": result.items_migrated,
            "items_skipped": result.items_skipped,
            "backup_path": result.backup_path,
        }

    def handle_rollback_migration(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик rollback_migration.

        Восстанавливает данные из резервной копии, созданной при миграции.

        Параметр ``data_dir`` из params игнорируется (безопасность W1761):
        обработчик использует только директорию, заданную при инициализации.
        ``backup_path`` должен находиться внутри ``<data_dir>/backups/`` —
        иначе бросает RuntimeError (защита от traversal через backup_path).

        Params:
            backup_path (str): путь к директории резервной копии (из MigrationResult.backup_path).

        Returns:
            restored_files (list[str]), backup_path (str)
        """
        data_dir = self._require_configured_data_dir()

        backup_path = str(params.get("backup_path", "")).strip()
        if not backup_path:
            raise ValueError("Параметр backup_path обязателен")

        # Защита от path traversal через backup_path: разрешаем только пути
        # внутри <data_dir>/backups/ (директория, куда _create_backup пишет).
        resolved_backup = Path(backup_path).expanduser().resolve()
        backups_root = (data_dir / "backups").resolve()
        if resolved_backup != backups_root and not resolved_backup.is_relative_to(backups_root):
            raise RuntimeError(
                f"rollback_migration: backup_path {resolved_backup!s} "
                f"находится за пределами разрешённой директории {backups_root!s}"
            )

        return self.rollback_migration(data_dir, backup_path)

    def rollback_migration(self, data_dir: Path, backup_path: str) -> dict[str, Any]:
        """Откатывает последнюю миграцию, восстанавливая файлы из резервной копии.

        Удерживает POSIX flock(LOCK_EX) на history.lock на всё время восстановления,
        чтобы исключить гонку с параллельными append-операциями StateStore (тот же
        паттерн, что в _migrate_v1_to_v2).

        Args:
            data_dir: путь к директории данных Krab Ear.
            backup_path: путь к резервной копии (из MigrationResult.backup_path).

        Returns:
            dict с ключами: restored_files (list[str]), backup_path (str).

        Raises:
            ValueError: если backup_path не существует или не является директорией.
        """
        backup_dir = Path(backup_path)
        if not backup_dir.is_dir():
            raise ValueError(f"Директория резервной копии не найдена: {backup_path!r}")

        data_dir = Path(data_dir)
        lock_path = data_dir / "history.lock"
        restored: list[str] = []

        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                for src in backup_dir.iterdir():
                    if src.name == "migration_meta.json":
                        continue
                    dest = data_dir / src.name
                    shutil.copy2(src, dest)
                    restored.append(src.name)
                    logger.info("Откат: восстановлен файл %s", src.name)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

        logger.info("Откат миграции завершён: восстановлено %d файлов из %s", len(restored), backup_dir)
        return {"restored_files": restored, "backup_path": str(backup_dir)}

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _create_backup(self, data_dir: Path) -> str:
        """Создаёт резервную копию данных перед миграцией.

        Returns:
            Строковый путь к директории резервной копии.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backups_dir = data_dir / "backups"
        backup_dir = backups_dir / f"migration_backup_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)

        files_to_backup = [
            "history.ndjson",
            "history_tombstones.ndjson",
            "history_status.ndjson",
            "history_tags.ndjson",
            "history_favorites.ndjson",
            "history_annotations.ndjson",
            # text_updates: правки текста через replace_word / save_transcript_version
            "history_text_updates.ndjson",
            # purged_ids: tombstone-метки полной очистки (purge_all_data) — потеря
            # этого файла при откате воскрешает удалённые записи
            "history_purged_ids.ndjson",
            "settings.json",
        ]

        backed_up: list[str] = []
        for fname in files_to_backup:
            src = data_dir / fname
            if src.exists():
                shutil.copy2(src, backup_dir / fname)
                backed_up.append(fname)

        meta = {
            "migration_backup_ts": ts,
            "files": backed_up,
        }
        (backup_dir / "migration_meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        logger.info("Резервная копия для миграции создана: %s", backup_dir)
        return str(backup_dir)

    def _migrate_v1_to_v2(self, data_dir: Path, backup_path: str) -> MigrationResult:
        """Применяет миграцию v1.0 → v2.0.

        Добавляет поля tags=[], favorite=false, annotation="" ко всем записям,
        которым они не хватает. Перезаписывает history.ndjson атомарно.

        Удерживает POSIX flock(LOCK_EX) на history.lock на всё время записи,
        чтобы исключить гонку с параллельными append-операциями StateStore.
        """
        lock_path = data_dir / "history.lock"
        with open(lock_path, "a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                result = self._do_migrate_v1_to_v2(data_dir, backup_path)
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        return result

    def _do_migrate_v1_to_v2(self, data_dir: Path, backup_path: str) -> MigrationResult:
        """Внутренний метод миграции — вызывается под удержанием history.lock."""
        history_path = data_dir / "history.ndjson"
        all_items = _read_ndjson(history_path)

        migrated = 0
        skipped = 0
        updated_lines: list[str] = []

        for item in all_items:
            # Tombstone-записи не трогаем — они не являются полными историческими записями
            if "text" not in item:
                updated_lines.append(json.dumps(item, ensure_ascii=False))
                skipped += 1
                continue

            changed = False
            for field, default_val in _V2_DEFAULTS.items():
                if field not in item:
                    item[field] = default_val
                    changed = True

            updated_lines.append(json.dumps(item, ensure_ascii=False))
            if changed:
                migrated += 1
            else:
                skipped += 1

        # Атомарная запись через tmp-файл.
        # try/finally гарантирует удаление tmp-файла при любом исключении между
        # write и replace, чтобы осиротевший *.migration_tmp не оставался на диске.
        tmp_path = history_path.with_suffix(".ndjson.migration_tmp")
        content = "\n".join(updated_lines)
        if content and not content.endswith("\n"):
            content += "\n"
        try:
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(history_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        logger.info(
            "Миграция v1.0→v2.0 завершена: обновлено %d записей, пропущено %d",
            migrated,
            skipped,
        )

        return MigrationResult(
            from_version="1.0",
            to_version="2.0",
            items_migrated=migrated,
            items_skipped=skipped,
            backup_path=backup_path,
        )
