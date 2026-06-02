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
_ARCHIVE_LOCK_FILE = "archive.ndjson.lock"  # sibling lock file for cross-process flock


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
    """

    def __init__(self, store: Any, semantic_searcher: Any | None = None, transcript_versioner: Any | None = None) -> None:
        self._store = store
        self._semantic_searcher = semantic_searcher
        self._transcript_versioner = transcript_versioner  # W1259
        data_dir = Path(getattr(store, "data_dir", "."))
        self._archive_dir = data_dir / _ARCHIVE_SUBDIR
        self._archive_path = self._archive_dir / _ARCHIVE_FILE
        self._lock_path = self._archive_dir / _ARCHIVE_LOCK_FILE
        self._lock = threading.Lock()
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._archive_path.touch(exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        # Late-injection: RecordingChainManager для каскадной очистки ghost item_ids (W1253 RC-3).
        self._recording_chain_mgr = None

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
        """Атомарный append JSON-строки с cross-process flock на sibling lock file."""
        with self._lock_path.open("a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    def _rewrite_archive(self, items: list[dict[str, Any]]) -> None:
        """Перезаписывает файл архива атомарно через tmp-файл с cross-process flock."""
        tmp = self._archive_path.with_suffix(".ndjson.tmp")
        with self._lock_path.open("a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    with tmp.open("w", encoding="utf-8") as fh:
                        for item in items:
                            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                    tmp.replace(self._archive_path)
                except Exception:
                    tmp.unlink(missing_ok=True)
                    raise
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def clear_all(self) -> int:
        """Полностью очищает архив (privacy-purge / wipe-all).

        Перезаписывает archive.ndjson пустым файлом под cross-process flock,
        гарантируя что НИ ОДНОГО транскрипта не остаётся на диске.

        Используется ТОЛЬКО из handle_purge_all_data. Не вызывать из других мест.

        Returns:
            Количество удалённых архивных записей (до очистки).
        """
        with self._lock:
            archived_before = len(self._read_archive())
            tmp = self._archive_path.with_suffix(".ndjson.tmp")
            with self._lock_path.open("a", encoding="utf-8") as lock_f:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
                try:
                    try:
                        # Truncate to empty via atomic tmp-replace
                        tmp.write_text("", encoding="utf-8")
                        tmp.replace(self._archive_path)
                    except Exception:
                        tmp.unlink(missing_ok=True)
                        raise
                finally:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        return archived_before

    def _store_supports_atomic_archive(self, store: Any) -> bool:
        """Проверяет, что store предоставляет unlocked-API StateStore для атомарного пути.

        Реальный StateStore выставляет `_lock()` (fcntl.flock, сериализующий ВСЕ
        записи истории), `_load_active_items_unlocked()`, статический `_append_ndjson`
        и `tombstones_path`.  Тестовые двойники (FakeStore) их не имеют — для них
        используется устаревший per-item fallback через публичный API.
        """
        return (
            hasattr(store, "_lock")
            and hasattr(store, "_load_active_items_unlocked")
            and hasattr(store, "_append_ndjson")
            and hasattr(store, "tombstones_path")
        )

    def _archive_side_effects(self, clean_id: str) -> None:
        """Побочные эффекты после удаления записи из активной истории.

        Каскадная очистка версий транскрипта, semantic-индекса и цепочек записей.
        Любая ошибка здесь не должна прерывать архивирование (data-loss было бы
        хуже, чем рассинхрон вторичных индексов) — поэтому всё в try/except.
        """
        if self._transcript_versioner is not None:
            try:
                self._transcript_versioner.purge_versions_for_item(clean_id)
            except Exception:
                logger.exception("archive_items: версии id=%s", clean_id)
        if self._semantic_searcher is not None:
            try:
                self._semantic_searcher.remove_item(clean_id)
            except Exception as exc:
                logger.warning(
                    "archive_items: не удалось удалить %s из semantic index: %s",
                    clean_id, exc,
                )
        # Каскадное удаление ghost item_id из цепочек (W1253 RC-3).
        if self._recording_chain_mgr is not None:
            self._recording_chain_mgr.remove_item_from_all_chains(clean_id)

    def archive_items(self, item_ids: list[str], store: Any | None = None) -> ArchiveResult:
        """Перемещает записи из активной истории в архив.

        W1768 (data-loss race fix): чтение активной записи, дозапись в архив и
        tombstone-удаление выполняются под ОДНИМ захватом `store._lock()` —
        той же межпроцессной fcntl.flock, что сериализует все записи/компактирование
        истории.  Раньше read (`get_history_item_by_id`) и delete
        (`delete_history_item`) были отдельными locked-операциями: конкурентная
        запись или `compact()` между ними могла потерять или продублировать записи
        (TOCTOU).  Архив дописывается ВНУТРИ этого lock через `_append_ndjson` —
        если он упадёт, tombstone не пишется и запись остаётся в активной истории
        (fail-safe: дублирование лучше потери).

        Внимание: `store._lock()` — fcntl.flock и НЕ реентрантна (повторный
        `LOCK_EX` на втором file-description из этого же процесса заблокируется
        навсегда).  Поэтому внутри lock используются ТОЛЬКО `_unlocked`-внутренности
        StateStore, а не публичные `get_history_item_by_id` / `delete_history_item`,
        которые сами захватывают `_lock()`.

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
        # self._lock сериализует архивные операции (archive/unarchive/list) внутри процесса.
        with self._lock:
            if self._store_supports_atomic_archive(_store):
                # Атомарный путь: весь read-modify-delete под единым store._lock()
                # (межпроцессная fcntl.flock). Снимок активных записей берётся один
                # раз, чтобы конкурентный compact() не вклинился между чтениями.
                with _store._lock():
                    active_by_id = {
                        item.id: item
                        for item in _store._load_active_items_unlocked()
                    }
                    for item_id in item_ids:
                        clean_id = str(item_id).strip()
                        if not clean_id:
                            continue
                        item = active_by_id.get(clean_id)
                        if item is None:
                            logger.debug("archive_items: запись не найдена id=%s", clean_id)
                            continue
                        item_dict = item.to_dict() if hasattr(item, "to_dict") else item
                        item_dict = dict(item_dict)
                        item_dict["archived_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        # Сначала дозапись в архив (отдельный sibling-flock на архивном
                        # файле — другой файл, конфликта с store._lock нет). Только если
                        # архивная запись прошла — пишем tombstone (фиксируем удаление).
                        self._append_ndjson(self._archive_path, item_dict)
                        _store._append_ndjson(_store.tombstones_path, {"id": clean_id})
                        archived_count += 1
                        # Снимаем из снимка, чтобы повторный id в item_ids не дублировался.
                        active_by_id.pop(clean_id, None)
                        self._archive_side_effects(clean_id)
            else:
                # Fallback для тестовых двойников без unlocked-API StateStore.
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
                    self._append_ndjson(self._archive_path, item_dict)
                    _store.delete_history_item(clean_id)
                    archived_count += 1
                    self._archive_side_effects(clean_id)

        size_bytes = self._archive_path.stat().st_size if self._archive_path.exists() else 0
        return ArchiveResult(
            archived_count=archived_count,
            archive_path=str(self._archive_path),
            size_mb=round(size_bytes / (1024 * 1024), 3),
        )

    def unarchive_items(self, item_ids: list[str], store: Any | None = None) -> dict[str, Any]:
        """Восстанавливает записи из архива обратно в активную историю.

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

        with self._lock:
            all_archived = self._read_archive()
            found_ids: set[str] = set()
            remaining: list[dict[str, Any]] = []

            for item in all_archived:
                item_id = item.get("id", "")
                if item_id in ids_set:
                    found_ids.add(item_id)
                    # W1047/W1542b: восстанавливаем полный словарь без поля archived_at,
                    # сохраняя ВСЕ оригинальные поля (id, ts, chat_id, message_id, llm_applied,
                    # diarization, tags, favorite и т.д.). Без этого ветка add_history_item
                    # роняла 10+ полей метаданных и генерировала новый UUID.
                    restore_dict = {k: v for k, v in item.items() if k != "archived_at"}
                    try:
                        if hasattr(_store, "restore_history_item_raw"):
                            # Предпочтительный путь: сохранить все поля + оригинальный id.
                            # При коллизии id со существующей активной записью store
                            # добавляет суффикс '-restored' (новый UUID не генерируется).
                            _store.restore_history_item_raw(restore_dict)
                        else:
                            # Фоллбэк для FakeStore-стабов в тестах без restore_history_item_raw.
                            # ВНИМАНИЕ: эта ветка теряет 10+ полей метаданных — используется
                            # только для обратной совместимости с устаревшими тестовыми двойниками.
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
                        unarchived_count += 1
                        if self._semantic_searcher is not None:
                            restore_text = restore_dict.get("text", "")
                            if restore_text and restore_text.strip():
                                try:
                                    self._semantic_searcher.index_item(item_id, restore_text)
                                except Exception as exc:
                                    logger.warning(
                                        "unarchive_items: не удалось переиндексировать %s: %s",
                                        item_id, exc,
                                    )
                    except Exception as exc:
                        logger.error("Не удалось восстановить запись id=%s: %s", item_id, exc)
                        remaining.append(item)
                else:
                    remaining.append(item)

            not_found = sorted(ids_set - found_ids)
            self._rewrite_archive(remaining)

        return {"unarchived_count": unarchived_count, "not_found": not_found}

    def list_archived(self, limit: int = 50) -> list[dict[str, Any]]:
        """Возвращает список архивированных записей (от новых к старым).

        Args:
            limit: Максимальное количество записей (1–500).

        Returns:
            Список словарей записей с полем archived_at.
        """
        safe_limit = max(1, min(limit, 500))
        with self._lock:
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
        with self._lock:
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
