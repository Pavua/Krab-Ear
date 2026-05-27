"""ArchiveManager — архивирование старых записей истории Krab Ear.

Архивированные записи перемещаются из активной истории в отдельный файл
{data_dir}/archive/archive.ndjson. Записи можно восстановить обратно.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ArchiveManager")

_ARCHIVE_SUBDIR = "archive"
_ARCHIVE_FILE = "archive.ndjson"
_ARCHIVE_LOCK_FILE = "archive.ndjson.lock"


@dataclass
class ArchiveResult:
    """Результат операции архивирования."""

    archived_count: int
    archive_path: str
    size_mb: float


class ArchiveManager:
    """Управление архивным хранилищем записей истории.

    Архив хранится в {data_dir}/archive/archive.ndjson отдельно от активной
    истории. Удалённые из активной истории записи могут быть восстановлены.

    Файловая блокировка через fcntl.flock на archive.ndjson.lock обеспечивает
    безопасность при одновременном доступе нескольких процессов к одному data_dir.
    """

    def __init__(self, store: Any, semantic_searcher: Any | None = None) -> None:
        self._store = store
        self._semantic_searcher = semantic_searcher
        data_dir = Path(getattr(store, "data_dir", "."))
        self._archive_dir = data_dir / _ARCHIVE_SUBDIR
        self._archive_path = self._archive_dir / _ARCHIVE_FILE
        self._lock_path = self._archive_dir / _ARCHIVE_LOCK_FILE
        self._thread_lock = threading.Lock()
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._archive_path.touch(exist_ok=True)
        self._lock_path.touch(exist_ok=True)

    # ------------------------------------------------------------------
    # Файловая блокировка (межпроцессная)
    # ------------------------------------------------------------------

    def _flock(self):
        """Контекстный менеджер: POSIX flock на archive.ndjson.lock.

        Паттерн из state_store.py: отдельный lock-файл, чтобы избежать
        stat-race на самом data-файле. Блокировка удерживается на всё
        время записи.
        """
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            self._lock_path.touch(exist_ok=True)
            with self._lock_path.open("r+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

        return _ctx()

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _read_archive(self) -> list[dict[str, Any]]:
        """Загружает все записи архива."""
        items: list[dict[str, Any]] = []
        try:
            for line in self._archive_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("id"):
                        items.append(obj)
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.warning("Не удалось прочитать архив: %s", exc)
        return items

    def _append_ndjson(self, path: Path, payload: dict[str, Any]) -> None:
        """Атомарный append JSON-строки с fcntl.flock."""
        with self._flock():
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _rewrite_archive(self, items: list[dict[str, Any]]) -> None:
        """Перезаписывает файл архива атомарно через tmp-файл с fcntl.flock."""
        tmp = self._archive_path.with_suffix(".ndjson.tmp")
        with self._flock():
            try:
                with tmp.open("w", encoding="utf-8") as fh:
                    for item in items:
                        fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                tmp.replace(self._archive_path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def archive_items(self, item_ids: list[str], store: Any | None = None) -> ArchiveResult:
        """Перемещает записи из активной истории в архив.

        Порядок операций (write-first, delete-second):
        1. Запись добавляется в архив.
        2. Запись удаляется из активной истории.
        При сбое удаления (шаг 2) выполняется откат: архив перезаписывается
        без только что добавленной записи, чтобы не допустить дублирования.

        Args:
            item_ids: Список ID записей для архивирования.
            store: StateStore (по умолчанию используется self._store).

        Returns:
            ArchiveResult с количеством архивированных записей, путём и размером.
        """
        _store = store if store is not None else self._store
        if not item_ids:
            return ArchiveResult(
                archived_count=0,
                archive_path=str(self._archive_path),
                size_mb=0.0,
            )

        archived_count = 0
        with self._thread_lock:
            for item_id in item_ids:
                clean_id = str(item_id).strip()
                if not clean_id:
                    continue
                item = _store.get_history_item_by_id(clean_id)
                if item is None:
                    logger.debug("archive_items: запись не найдена id=%s", clean_id)
                    continue
                item_dict = item.to_dict() if hasattr(item, "to_dict") else item
                item_dict = dict(item_dict)
                item_dict["archived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

                # Шаг 1: записываем в архив первым делом.
                self._append_ndjson(self._archive_path, item_dict)

                # Шаг 2: удаляем из активной истории.
                # При сбое — откатываем архивную запись.
                try:
                    _store.delete_history_item(clean_id)
                except Exception as exc:
                    logger.error(
                        "archive_items: не удалось удалить id=%s из активной истории, "
                        "откатываем архивную запись: %s",
                        clean_id,
                        exc,
                    )
                    # Откат: перечитываем архив и убираем только что добавленную запись.
                    try:
                        existing = self._read_archive()
                        rollback = [r for r in existing if r.get("id") != clean_id]
                        self._rewrite_archive(rollback)
                    except Exception as rb_exc:
                        logger.critical(
                            "archive_items: откат не удался для id=%s — запись может "
                            "присутствовать в обоих хранилищах: %s",
                            clean_id,
                            rb_exc,
                        )
                    continue

                # Шаг 3: удаляем из индекса семантического поиска (W1449 F1).
                if self._semantic_searcher is not None:
                    try:
                        self._semantic_searcher.remove_item(clean_id)
                    except Exception:
                        logger.warning(
                            "archive_items: semantic remove failed for %s", clean_id
                        )

                archived_count += 1

        size_bytes = self._archive_path.stat().st_size if self._archive_path.exists() else 0
        return ArchiveResult(
            archived_count=archived_count,
            archive_path=str(self._archive_path),
            size_mb=round(size_bytes / (1024 * 1024), 3),
        )

    def unarchive_items(self, item_ids: list[str], store: Any | None = None) -> dict[str, Any]:
        """Восстанавливает записи из архива обратно в активную историю.

        Порядок операций (restore-first, remove-second):
        1. Запись добавляется в активную историю.
        2. Архив перезаписывается без восстановленных записей.
        При сбое перезаписи архива (шаг 2) все успешно восстановленные записи уже
        находятся в активной истории; архив содержит их копии — логируем CRITICAL
        для последующего ручного устранения.

        Args:
            item_ids: Список ID записей для восстановления.
            store: StateStore (по умолчанию используется self._store).

        Returns:
            Словарь с ключами unarchived_count, not_found.
        """
        _store = store if store is not None else self._store
        ids_set = {str(i).strip() for i in item_ids if str(i).strip()}
        if not ids_set:
            return {"unarchived_count": 0, "not_found": []}

        unarchived_count = 0
        not_found: list[str] = []
        # Записи, успешно восстановленные в активную историю (для отката архива).
        restored_ids: set[str] = set()

        with self._thread_lock:
            all_archived = self._read_archive()
            found_ids: set[str] = set()
            remaining: list[dict[str, Any]] = []

            for item in all_archived:
                item_id = item.get("id", "")
                if item_id in ids_set:
                    found_ids.add(item_id)
                    # Шаг 1: восстанавливаем в активную историю без поля archived_at.
                    restore_dict = {k: v for k, v in item.items() if k != "archived_at"}
                    try:
                        _store.add_history_item(
                            text=restore_dict.get("text", ""),
                            paste_status=restore_dict.get("paste_status", "failed"),
                            source_text=restore_dict.get("source_text", ""),
                            translated_text=restore_dict.get("translated_text", ""),
                            translation_mode=restore_dict.get("translation_mode", "off"),
                            source_lang=restore_dict.get("source_lang", ""),
                            target_lang=restore_dict.get("target_lang", ""),
                            translation_status=restore_dict.get("translation_status", "not_requested"),
                            translation_engine=restore_dict.get("translation_engine", ""),
                        )
                        restored_ids.add(item_id)
                        unarchived_count += 1
                        # Успешно восстановлено — не включаем в remaining.
                    except Exception as exc:
                        logger.error("Не удалось восстановить запись id=%s: %s", item_id, exc)
                        # Восстановление не удалось — оставляем в архиве.
                        remaining.append(item)
                else:
                    remaining.append(item)

            not_found = sorted(ids_set - found_ids)

            # Шаг 2: перезаписываем архив без успешно восстановленных записей.
            # При сбое — записи уже в активной истории, но архив содержит их копии.
            if restored_ids:
                try:
                    self._rewrite_archive(remaining)
                except Exception as exc:
                    logger.critical(
                        "unarchive_items: не удалось перезаписать архив после восстановления "
                        "%d записей (ids=%s). Записи существуют в обоих хранилищах — "
                        "требуется ручное устранение: %s",
                        len(restored_ids),
                        sorted(restored_ids),
                        exc,
                    )

        return {"unarchived_count": unarchived_count, "not_found": not_found}

    def list_archived(self, limit: int = 50) -> list[dict[str, Any]]:
        """Возвращает список архивированных записей (от новых к старым).

        Args:
            limit: Максимальное количество записей (1–500).

        Returns:
            Список словарей записей с полем archived_at.
        """
        safe_limit = max(1, min(limit, 500))
        with self._thread_lock:
            items = self._read_archive()
        # Сортируем по archived_at (новые первыми)
        items_sorted = sorted(
            items,
            key=lambda x: x.get("archived_at", ""),
            reverse=True,
        )
        return items_sorted[:safe_limit]

    def get_archive_stats(self) -> dict[str, Any]:
        """Возвращает статистику архива.

        Returns:
            Словарь с ключами:
            - total_archived: общее количество архивированных записей
            - size_mb: размер файла архива в МБ
            - oldest_ts: временная метка самой старой записи (ISO8601) или None
            - newest_ts: временная метка самой новой записи (ISO8601) или None
            - archive_path: путь к файлу архива
        """
        with self._thread_lock:
            items = self._read_archive()

        total = len(items)
        size_bytes = self._archive_path.stat().st_size if self._archive_path.exists() else 0

        oldest_ts: str | None = None
        newest_ts: str | None = None
        if items:
            timestamps = [item.get("archived_at") or item.get("ts", "") for item in items]
            timestamps = [t for t in timestamps if t]
            if timestamps:
                oldest_ts = min(timestamps)
                newest_ts = max(timestamps)

        return {
            "total_archived": total,
            "size_mb": round(size_bytes / (1024 * 1024), 3),
            "oldest_ts": oldest_ts,
            "newest_ts": newest_ts,
            "archive_path": str(self._archive_path),
        }

    # ------------------------------------------------------------------
    # IPC-обработчики
    # ------------------------------------------------------------------

    def handle_archive_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик archive_items."""
        raw_ids = params.get("item_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("Параметр item_ids должен быть списком")
        result = self.archive_items(item_ids=raw_ids)
        return {
            "archived_count": result.archived_count,
            "archive_path": result.archive_path,
            "size_mb": result.size_mb,
        }

    def handle_unarchive_items(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик unarchive_items."""
        raw_ids = params.get("item_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("Параметр item_ids должен быть списком")
        return self.unarchive_items(item_ids=raw_ids)

    def handle_list_archived(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик list_archived."""
        limit = int(params.get("limit", 50))
        items = self.list_archived(limit=limit)
        return {"items": items, "total": len(items)}

    def handle_get_archive_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_archive_stats."""
        return self.get_archive_stats()
