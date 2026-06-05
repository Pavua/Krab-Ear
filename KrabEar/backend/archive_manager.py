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

# wave-25 (B1-b): жёсткие границы на batch архивирования, чтобы держать store-flock
# bounded. Гигантский item_ids держал бы межпроцессную fcntl.flock истории на всё
# время итерации (lock starvation для записи/компактирования). Все id валидируются
# ДО захвата lock; превышение MAX_ARCHIVE_BATCH отклоняется.
_MAX_ARCHIVE_BATCH = 100        # макс. item_ids за один archive_items()
_MAX_ITEM_ID_LEN = 200          # макс. длина одного id (отсекает мусорные строки)
# wave-25 (B1-c): защита от unbounded-load archive.ndjson на каждый list/stats/unarchive.
_MAX_ARCHIVE_LOAD = 50_000      # выше — warn + truncate (защита памяти)


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

    def __init__(
        self,
        store: Any,
        semantic_searcher: Any | None = None,
        transcript_versioner: Any | None = None,
        settings_get: Any | None = None,
    ) -> None:
        self._store = store
        self._semantic_searcher = semantic_searcher
        self._transcript_versioner = transcript_versioner  # W1259
        # settings_get(key, default) → runtime settings lookup (privacy_mode_enabled gate).
        self._settings_get = settings_get or (lambda k, d: d)
        data_dir = Path(getattr(store, "data_dir", "."))
        self._archive_dir = data_dir / _ARCHIVE_SUBDIR
        self._archive_path = self._archive_dir / _ARCHIVE_FILE
        self._lock_path = self._archive_dir / _ARCHIVE_LOCK_FILE
        self._lock = threading.Lock()
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._archive_path.touch(exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        # wave-25 (B1-a): purge-epoch счётчик для закрытия TOCTOU-окна между
        # снимком активной истории и записью в архив. Инкрементируется в clear_all()
        # (privacy-purge). archive_items() снимает значение ДО захвата store-flock и
        # перепроверяет ПОСЛЕ — если purge произошёл, операция отменяется, иначе
        # purge-followed-by-archive воскресил бы PII-записи. Защищён _epoch_lock,
        # т.к. clear_all() и archive_items() могут гонять из разных потоков.
        self._epoch_lock = threading.Lock()
        self._purge_epoch = 0
        # Late-injection: RecordingChainManager для каскадной очистки ghost item_ids (W1253 RC-3).
        self._recording_chain_mgr = None

    def _current_epoch(self) -> int:
        """Текущее значение purge-epoch (thread-safe чтение)."""
        with self._epoch_lock:
            return self._purge_epoch

    # ------------------------------------------------------------------
    # Внутренние хелперы
    # ------------------------------------------------------------------

    def _read_archive(self) -> list[dict[str, Any]]:
        """Загружает записи архива.

        wave-25 (B1-c): на каждый list/stats/unarchive вызов мы парсим весь
        archive.ndjson. На раздутом архиве это unbounded-память. Жёстко обрезаем
        при _MAX_ARCHIVE_LOAD (warn + truncate), чтобы один гигантский файл не
        исчерпал RAM бэкенда.
        """
        items: list[dict[str, Any]] = []
        truncated = False
        try:
            for line in self._archive_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict) and obj.get("id"):
                        items.append(obj)
                        if len(items) >= _MAX_ARCHIVE_LOAD:
                            truncated = True
                            break
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            logger.warning("Не удалось прочитать архив: %s", exc)
        if truncated:
            logger.warning(
                "archive: загружено %d записей (достигнут предел %d) — список усечён; "
                "архив следует уплотнить/проредить",
                len(items),
                _MAX_ARCHIVE_LOAD,
            )
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
        # wave-25 (B1-a): инкремент purge-epoch ДО любой работы. archive_items(),
        # снявший старое значение и ещё не дошедший до записи, перепроверит epoch
        # под store-flock, увидит расхождение и отменит запись — закрывает TOCTOU,
        # при котором purge-followed-by-archive воскресил бы PII в archive.ndjson.
        with self._epoch_lock:
            self._purge_epoch += 1
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

    def _validate_archive_ids(self, item_ids: list[str]) -> list[str] | dict[str, Any]:
        """Валидирует item_ids ДО захвата store-flock (wave-25 B1-b).

        Lock starvation fix: раньше итерация по item_ids (включая мусорные/гигантские
        списки) шла ВНУТРИ store._lock() — межпроцессной fcntl.flock, сериализующей
        ВСЕ записи/компактирование истории. Огромный список держал бы lock сколь угодно
        долго. Теперь все id чистятся и проверяются заранее; flock держится только на
        ограниченный (≤ _MAX_ARCHIVE_BATCH) валидный набор.

        Отклоняем не-строки, пустые после strip, длиннее _MAX_ITEM_ID_LEN. Если число
        валидных id превышает _MAX_ARCHIVE_BATCH — возвращаем ошибку (батч слишком велик).

        Returns:
            Список очищенных id (с сохранением порядка, без дубликатов) ИЛИ
            dict с ok=False и reason при превышении лимита.
        """
        clean_ids: list[str] = []
        seen: set[str] = set()
        for raw in item_ids:
            if not isinstance(raw, str):
                # Молча пропускаем не-строки (мусор), чтобы не держать lock на них.
                continue
            clean = raw.strip()
            if not clean or len(clean) > _MAX_ITEM_ID_LEN:
                continue
            if clean in seen:
                continue
            seen.add(clean)
            clean_ids.append(clean)
        if len(clean_ids) > _MAX_ARCHIVE_BATCH:
            return {
                "ok": False,
                "reason": "too_many_ids",
                "max": _MAX_ARCHIVE_BATCH,
                "got": len(clean_ids),
            }
        return clean_ids

    def archive_items(
        self, item_ids: list[str], store: Any | None = None
    ) -> ArchiveResult | dict[str, Any]:
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

        wave-25 (B1-a/b): item_ids валидируются и кэпируются (≤ _MAX_ARCHIVE_BATCH)
        ДО захвата lock (bounded flock-hold). purge-epoch снимается ДО store-flock и
        перепроверяется ПОСЛЕ его захвата — если конкурентный privacy-purge произошёл
        в окне, операция отменяется (purge-followed-by-archive воскресил бы PII).

        Args:
            item_ids: Список ID записей для архивирования.
            store: StateStore (по умолчанию используется self._store).

        Returns:
            ArchiveResult при успехе; либо dict с ok=False и reason при
            переполнении батча (too_many_ids) или гонке с purge (purge_in_progress).
        """
        _store = store if store is not None else self._store
        if not item_ids:
            return ArchiveResult(
                archived_count=0,
                archive_path=str(self._archive_path),
                size_mb=0.0,
            )

        # wave-25 (B1-b): валидация + кэп ДО любого lock — flock-hold ограничен.
        validated = self._validate_archive_ids(item_ids)
        if isinstance(validated, dict):
            return validated  # too_many_ids
        if not validated:
            return ArchiveResult(
                archived_count=0,
                archive_path=str(self._archive_path),
                size_mb=0.0,
            )

        # wave-25 (B1-a): снимок purge-epoch ДО захвата store-flock.
        epoch_before = self._current_epoch()

        archived_count = 0
        # self._lock сериализует архивные операции (archive/unarchive/list) внутри процесса.
        with self._lock:
            if self._store_supports_atomic_archive(_store):
                # Атомарный путь: весь read-modify-delete под единым store._lock()
                # (межпроцессная fcntl.flock). Снимок активных записей берётся один
                # раз, чтобы конкурентный compact() не вклинился между чтениями.
                with _store._lock():
                    # wave-25 (B1-a): перепроверяем epoch ПОД store-flock. Если purge
                    # инкрементировал его в окне между снимком и захватом — отменяем,
                    # иначе только что очищенный архив получил бы PII обратно.
                    if self._current_epoch() != epoch_before:
                        logger.warning(
                            "archive_items: обнаружен конкурентный purge (epoch %d→%d) — отмена",
                            epoch_before, self._current_epoch(),
                        )
                        return {"ok": False, "reason": "purge_in_progress"}
                    active_by_id = {
                        item.id: item
                        for item in _store._load_active_items_unlocked()
                    }
                    for clean_id in validated:
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
                # wave-25 (B1-a): epoch-перепроверка и здесь (purge мог пройти между
                # снимком и началом работы); store-flock в этой ветке нет.
                if self._current_epoch() != epoch_before:
                    logger.warning(
                        "archive_items: обнаружен конкурентный purge (epoch %d→%d) — отмена",
                        epoch_before, self._current_epoch(),
                    )
                    return {"ok": False, "reason": "purge_in_progress"}
                for clean_id in validated:
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

        wave-33 (B2): зеркалит purge-epoch проверку из archive_items.
        Без этой проверки конкурентный purge_all_data между чтением архива и
        записью в активную историю мог бы воскресить только что очищенные PII-записи.
        Снимок epoch берётся ДО захвата self._lock; перепроверка — внутри.

        Args:
            item_ids: Список ID записей для восстановления.
            store: StateStore (по умолчанию используется self._store).

        Returns:
            Словарь с ключами unarchived_count, not_found.
            При обнаружении конкурентного purge возвращает ok=False, reason=purge_in_progress.
        """
        _store = store if store is not None else self._store
        ids_set = {str(i).strip() for i in item_ids if str(i).strip()}
        if not ids_set:
            return {"unarchived_count": 0, "not_found": []}

        # wave-33 (B2): снимок purge-epoch ДО захвата self._lock.
        epoch_before = self._current_epoch()

        unarchived_count = 0
        not_found: list[str] = []

        with self._lock:
            # wave-33 (B2): перепроверяем epoch под self._lock. Если конкурентный
            # purge инкрементировал его между снимком и захватом — отменяем:
            # иначе только что очищенный архив получил бы PII обратно в active.
            if self._current_epoch() != epoch_before:
                logger.warning(
                    "unarchive_items: обнаружен конкурентный purge (epoch %d→%d) — отмена",
                    epoch_before, self._current_epoch(),
                )
                return {"ok": False, "reason": "purge_in_progress"}

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
        """IPC-обработчик archive_items.

        wave-25: archive_items может вернуть dict ошибки (ok=False) при переполнении
        батча (too_many_ids) или гонке с privacy-purge (purge_in_progress) — в этом
        случае прокидываем dict как есть; иначе нормализуем ArchiveResult в dict.

        wave-33 (B3): в режиме privacy_mode архивирование запрещено — оно перемещало
        бы PII-записи в archive.ndjson, скрывая их от handle_purge_all_data.
        """
        # wave-33 (B3): privacy gate — не архивировать в режиме приватности.
        # FIXED: was checking params.get("privacy_mode") which is never injected;
        # use runtime settings lookup instead (same pattern as all other gates).
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_enabled"}

        raw_ids = params.get("item_ids", [])
        if not isinstance(raw_ids, list):
            raise ValueError("Параметр item_ids должен быть списком")
        result = self.archive_items(item_ids=raw_ids)
        if isinstance(result, dict):
            return result  # {"ok": False, "reason": ...}
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
        """IPC-обработчик list_archived.

        Privacy gate (wave-1770 HIGH): returns archived items including transcript
        text and translated_text — must be blocked in privacy mode.
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {"items": [], "total": 0, "reason": "privacy_mode_active"}
        limit = int(params.get("limit", 50))
        items = self.list_archived(limit=limit)
        return {"items": items, "total": len(items)}

    def handle_get_archive_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """IPC-обработчик get_archive_stats.

        Privacy gate (wave-1770 HIGH): archive stats expose item count/timing
        which reveals recording activity patterns in privacy mode.
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "total_archived": 0,
                "archive_size_mb": 0.0,
                "oldest_ts": None,
                "newest_ts": None,
                "reason": "privacy_mode_active",
            }
        return self.get_archive_stats()
