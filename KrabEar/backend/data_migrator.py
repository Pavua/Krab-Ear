"""Инструмент миграции данных Krab Ear между форматами/версиями.

Поддерживаемые миграции:
- v1.0 → v2.0: добавляет поля tags, favorite, annotation к записям, где они отсутствуют.

Всегда создаёт резервную копию данных перед миграцией.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from backend.history_encryption_policy import (
    OPERATION_UNAVAILABLE_REASON as _ENC_OP_UNAVAILABLE,
    data_dir_policy_reader,
    policy_blocks,
)
from backend.state_store import history_flock
from core.atomic_io import atomic_binary_writer, atomic_write_text

logger = logging.getLogger("KrabEar.Backend.DataMigrator")

# Текущая поддерживаемая версия схемы
LATEST_VERSION = "2.0"

# C2 DoS guard: максимальное число хранимых migration-бэкапов.
# Сверх лимита — самые старые удаляются при создании нового.
_MAX_MIGRATION_BACKUPS = 5

_MIGRATION_FILES = (
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
)

# Поля v2.0, которых не было в v1.0
_V2_DEFAULTS: dict[str, Any] = {
    "tags": [],
    "favorite": False,
    "annotation": "",
}

# A5.2b DEBT (adversarial review MINOR-2, 2026-09-26): service.py startup
# логирует "data_migrator: migration complete ..." безусловно, даже когда
# migrate() вернул reason=history_encryption_operation_unavailable на смешанном
# v1.0 + encryption-ON профиле — потому что не смотрит MigrationResult.reason.
# Caller-side правка намеренно вне A5.2a (service.py заморожен баном карточки).
# Machine-readable сигнал уже есть: MigrationResult.reason /
# handle_run_migration["reason"]. A5.2b обязан сделать startup-лог честным.
A5_2B_CALLER_SUCCESS_LOG_DEBT = (
    "service.py startup must not log migration success when reason is set"
)


@dataclass
class MigrationResult:
    """Результат миграции данных."""

    from_version: str
    to_version: str
    items_migrated: int
    items_skipped: int
    backup_path: str
    # A5.2a: машинно-читаемая причина отказа, когда legacy schema migration
    # запрещена политикой шифрования. None = миграция не блокировалась.
    reason: str | None = None


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


def _enc_blocked(reader, data_dir: Path) -> bool:
    """Политика блокирует ИЛИ в журналах остались ENC1-строки.

    ``_read_ndjson`` пропускает не-JSON строки, поэтому raw-rewrite (migrate,
    rollback) молча стёр бы ENC1-остаток даже при явном флаге OFF.
    """
    from backend.state_store import has_encrypted_history_in, history_journal_paths

    return policy_blocks(reader) or has_encrypted_history_in(history_journal_paths(data_dir))


def _parse_ndjson_object(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _raw_ndjson_lines(path: Path) -> Iterator[bytes]:
    """Читает LF/CRLF/CR, сохраняя исходные байты и окончания строк."""
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as source:
        for line in source:
            yield line.encode("utf-8", errors="surrogateescape")


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    """Читает объекты NDJSON. Невалидные строки игнорируются при определении схемы."""
    if not path.exists():
        return []

    items: list[dict[str, Any]] = []
    for line_no, line in enumerate(_raw_ndjson_lines(path), start=1):
        if not line.strip():
            continue
        item = _parse_ndjson_object(line)
        if item is None:
            logger.warning("Некорректная запись в %s строка %d", path.name, line_no)
            continue
        items.append(item)
    return items


def _copy_private_file(src: Path, dest: Path) -> None:
    """Копирует обычный файл, атомарно публикуя приватную копию."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"Ожидался обычный файл: {src.name}")
        with atomic_binary_writer(dest) as output:
            shutil.copyfileobj(source, output)


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

        # A5.2a: schema migration создаёт plaintext backup, raw rewrite и prune.
        # При Encryption ON — отказ. Guard не трогает Keychain. Возвращаем
        # MigrationResult (startup ожидает его, а не произвольный failure dict).
        reader = data_dir_policy_reader(data_dir)
        if _enc_blocked(reader, data_dir):
            logger.warning(
                "DataMigrator.migrate: history encryption on — legacy schema "
                "migration refused (to=%s, %s)",
                target_version, _ENC_OP_UNAVAILABLE,
            )
            return MigrationResult(
                from_version="unknown",
                to_version=target_version,
                items_migrated=0,
                items_skipped=0,
                backup_path="",
                reason=_ENC_OP_UNAVAILABLE,
            )

        current = self.get_schema_version(data_dir)

        # C2 DoS guard: если миграция не нужна — возвращаем ранний ответ БЕЗ создания
        # резервной копии. Без этой проверки IPC-спам run_migration создавал бы бэкап
        # при каждом вызове, заполняя диск.
        if current == target_version:
            logger.info("Миграция не требуется: текущая версия %s == целевой %s", current, target_version)
            return MigrationResult(
                from_version=current,
                to_version=target_version,
                items_migrated=0,
                items_skipped=0,
                backup_path="",
            )

        # CRITICAL-1 (A5.2a adversarial review): backup + rewrite обязаны
        # выполняться под тем же history.lock flock, что и save_settings, а
        # политика — перепроверяться ПОСЛЕ захвата lock. Иначе второй
        # StateStore/IPC-клиент может включить шифрование между guard и copy.
        with history_flock(data_dir):
            if _enc_blocked(reader, data_dir):
                logger.warning(
                    "DataMigrator.migrate: encryption включён до захвата lock — "
                    "legacy schema migration refused (%s)",
                    _ENC_OP_UNAVAILABLE,
                )
                return MigrationResult(
                    from_version=current,
                    to_version=target_version,
                    items_migrated=0,
                    items_skipped=0,
                    backup_path="",
                    reason=_ENC_OP_UNAVAILABLE,
                )
            current = self.get_schema_version(data_dir)
            if current == target_version:
                return MigrationResult(current, target_version, 0, 0, "")
            backup_path = self._create_backup(data_dir)

            # Миграция v1.0 → v2.0 (lock уже удерживается этим контекстом).
            if current == "1.0" and target_version == "2.0":
                return self._migrate_v1_to_v2(
                    data_dir, backup_path, _lock_held=True
                )

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
            "reason": result.reason,
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
            confirm (bool): ОБЯЗАТЕЛЬНО True — без явного подтверждения откат не выполняется
                (защита от случайного/автоматического вызова, аналогично purge_all_data).

        Returns:
            restored_files (list[str]), backup_path (str)

        Raises:
            ValueError: если подтверждён откат без указания backup_path.
        """
        # C3: require explicit confirmation (mirrors purge_all_data pattern)
        if params.get("confirm") is not True:
            return {
                "ok": False,
                "reason": "set confirm=true to execute rollback",
            }

        data_dir = self._require_configured_data_dir()

        backup_path = str(params.get("backup_path", "")).strip()
        if not backup_path:
            raise ValueError("Параметр backup_path обязателен")

        # Защита от path traversal через backup_path: разрешаем только пути
        # внутри <data_dir>/backups/ (директория, куда _create_backup пишет).
        resolved_backup = Path(backup_path).expanduser().resolve()
        backups_dir = data_dir / "backups"
        if backups_dir.is_symlink():
            raise ValueError("rollback_migration: backups не может быть символической ссылкой")
        backups_root = backups_dir.resolve()
        if resolved_backup.parent != backups_root or not resolved_backup.name.startswith("migration_backup_"):
            raise RuntimeError(
                f"rollback_migration: backup_path {resolved_backup!s} "
                f"находится за пределами разрешённой директории {backups_root!s}"
            )

        result = self.rollback_migration(data_dir, str(resolved_backup))
        return {**result, "backup_path": backup_path}

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
        data_dir = Path(data_dir)

        # A5.2a: rollback восстанавливает plaintext-файлы из миграционного
        # backup. При Encryption ON — отказ до copy/prune.
        reader = data_dir_policy_reader(data_dir)
        if _enc_blocked(reader, data_dir):
            logger.warning(
                "rollback_migration: history encryption on — legacy plaintext "
                "rollback refused (%s)",
                _ENC_OP_UNAVAILABLE,
            )
            return {
                "ok": False,
                "reason": _ENC_OP_UNAVAILABLE,
                "restored_files": [],
                "backup_path": str(backup_path),
            }

        if not backup_dir.is_dir():
            raise ValueError(f"Директория резервной копии не найдена: {backup_path!r}")

        restored: list[str] = []

        # CRITICAL-1: copy под тем же history.lock flock, что и save_settings, с
        # повторной проверкой политики ПОСЛЕ захвата (OFF→ON вторым StateStore).
        with history_flock(data_dir):
            if _enc_blocked(reader, data_dir):
                logger.warning(
                    "rollback_migration: encryption включён до захвата lock — "
                    "legacy plaintext rollback refused (%s)",
                    _ENC_OP_UNAVAILABLE,
                )
                return {
                    "ok": False,
                    "reason": _ENC_OP_UNAVAILABLE,
                    "restored_files": [],
                    "backup_path": str(backup_path),
                }
            manifest_path = backup_dir / "migration_meta.json"
            try:
                fd = os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as manifest:
                    if not stat.S_ISREG(os.fstat(manifest.fileno()).st_mode):
                        raise ValueError("Некорректный формат метаданных")
                    metadata = json.loads(manifest.read(8193))
                names = metadata["files"]
                if (
                    not isinstance(names, list)
                    or any(not isinstance(name, str) for name in names)
                    or len(set(names)) != len(names)
                    or "history.ndjson" not in names
                    or not set(names).issubset(_MIGRATION_FILES)
                    or set(names) != {src.name for src in backup_dir.iterdir() if src.name in _MIGRATION_FILES}
                ):
                    raise ValueError("Неполный набор файлов")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValueError("rollback_migration: некорректная резервная копия миграции") from exc

            sources = [backup_dir / name for name in names]
            for src in sources:
                if not stat.S_ISREG(src.lstat().st_mode):
                    raise ValueError(f"rollback_migration: недопустимый файл: {src.name}")
                if (data_dir / src.name).is_symlink():
                    raise ValueError(f"rollback_migration: назначение является символической ссылкой: {src.name}")
            for src in sources:
                _copy_private_file(src, data_dir / src.name)
                restored.append(src.name)
                logger.info("Откат: восстановлен файл %s", src.name)

        logger.info("Откат миграции завершён: восстановлено %d файлов из %s", len(restored), backup_dir)
        return {"restored_files": restored, "backup_path": str(backup_dir)}

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _create_backup(self, data_dir: Path) -> str:
        """Создаёт резервную копию данных перед миграцией.

        После создания удаляет самые старые migration_backup_* директории,
        чтобы число бэкапов не превышало _MAX_MIGRATION_BACKUPS (C2 DoS guard).

        Returns:
            Строковый путь к директории резервной копии.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backups_dir = data_dir / "backups"
        if backups_dir.is_symlink():
            raise ValueError("Каталог резервных копий является символической ссылкой")
        backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup_dir = Path(tempfile.mkdtemp(prefix=f"migration_backup_{ts}_", dir=backups_dir))

        try:
            backed_up: list[str] = []
            for fname in _MIGRATION_FILES:
                src = data_dir / fname
                if src.exists():
                    _copy_private_file(src, backup_dir / fname)
                    backed_up.append(fname)

            meta = {
                "migration_backup_ts": ts,
                "files": backed_up,
            }
            atomic_write_text(backup_dir / "migration_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        except BaseException:
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise

        logger.info("Резервная копия для миграции создана: %s", backup_dir)

        # C2 DoS guard: прореживаем старые бэкапы, оставляя только _MAX_MIGRATION_BACKUPS
        # последних (сортировка по имени == хронологическая, т.к. имя содержит timestamp).
        try:
            existing = sorted(
                [d for d in backups_dir.iterdir() if d.is_dir() and d.name.startswith("migration_backup_")],
                key=lambda d: d.name,
            )
            excess = existing[: max(0, len(existing) - _MAX_MIGRATION_BACKUPS)]
            for old_dir in excess:
                shutil.rmtree(old_dir, ignore_errors=True)
                logger.info("Удалён старый migration-бэкап (лимит %d): %s", _MAX_MIGRATION_BACKUPS, old_dir)
        except Exception:
            logger.warning("Не удалось прочистить старые migration-бэкапы", exc_info=True)

        return str(backup_dir)

    def _migrate_v1_to_v2(
        self,
        data_dir: Path,
        backup_path: str,
        *,
        _lock_held: bool = False,
    ) -> MigrationResult:
        """Применяет миграцию v1.0 → v2.0.

        Добавляет поля tags=[], favorite=false, annotation="" ко всем записям,
        которым они не хватает. Перезаписывает history.ndjson атомарно.

        ``_lock_held=True`` — вызывающий уже держит ``history_flock(data_dir)``
        (migrate вызывает под одним lock'ом для guard+backup+rewrite).
        """
        if _lock_held:
            return self._do_migrate_v1_to_v2(data_dir, backup_path)
        with history_flock(data_dir):
            return self._do_migrate_v1_to_v2(data_dir, backup_path)

    def _do_migrate_v1_to_v2(self, data_dir: Path, backup_path: str) -> MigrationResult:
        """Внутренний метод миграции — вызывается под удержанием history.lock."""
        history_path = data_dir / "history.ndjson"
        migrated = 0
        skipped = 0
        with atomic_binary_writer(history_path) as output:
            for line in _raw_ndjson_lines(history_path):
                item = _parse_ndjson_object(line)
                if item is None:
                    # Миграция обновляет схему. Повреждённые строки обрабатывает repair.
                    output.write(line)
                    continue
                if "text" not in item:
                    # Tombstone остаётся без изменений.
                    output.write(line)
                    skipped += 1
                    continue

                missing = {field: value for field, value in _V2_DEFAULTS.items() if field not in item}
                if missing:
                    item.update(missing)
                    ending = line[len(line.rstrip(b"\r\n")):] or b"\n"
                    output.write(json.dumps(item, ensure_ascii=False).encode("utf-8") + ending)
                    migrated += 1
                else:
                    output.write(line)
                    skipped += 1

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
