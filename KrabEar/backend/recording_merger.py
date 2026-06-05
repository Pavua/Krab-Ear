"""RecordingMerger — объединение нескольких записей истории в одну.

Поддерживает предварительный просмотр (preview_merge) и финальное слияние
(merge_items) с опциональным удалением исходных записей.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("KrabEar.Backend.RecordingMerger")

_TIMESTAMP_SEP = "\n\n"

# wave-27 MED (DoS): hard cap on how many history items a single merge may pull.
# Each id triggers a per-item store lookup + full transcript read; an unbounded
# item_ids list (accidental or hostile) could fan out into a heavy I/O storm and
# build an arbitrarily large merged record. 50 covers every realistic UI flow.
MAX_MERGE_ITEMS = 50
# wave-1770 HIGH: cap separator length to prevent merged text overflowing the
# 1 MB IPC_MAX_MESSAGE_BYTES limit. 50 items × 10 KB each = 500 KB text max;
# a 300 KB separator alone would push the payload far beyond the limit, causing
# silent IPC failure with no error returned to the Swift caller.
MAX_SEPARATOR_LEN = 1000


class RecordingMerger:
    """Объединяет несколько записей истории в одну.

    Конструктор не требует store — он передаётся явно в каждый метод,
    чтобы упростить тестирование и следовать паттерну других сервисов.

    Поздние инъекции (late-injection) из BackendService после конструирования
    всех зависимых объектов (избегаем циклической зависимости):

    * ``cascade_delete_fn`` (wave1776 HIGH 1) — каноническая cascade-функция
      ``HistoryService.cascade_delete_artifacts(item_id, item_ts)``: выполняет ВСЕ
      шаги удаления КРОМE самого tombstone'а (его merge пишет атомарно сам):
      стирание .md-транскрипта (W1762, privacy gap) + удаление эмбеддинга +
      ghost-ссылки в цепочках + playback-статистика + версии транскрипта.
      ``item_ts`` захватывается ПОКА оригинал ещё активен (после tombstone'а его
      ts не найти → .md erase молча пропустится).  Если None — fallback на прямой
      ``store.delete_history_item`` + локальный semantic-каскад (standalone/тесты).
    * ``_semantic_searcher`` — нужен для индексации НОВОЙ объединённой записи
      (это не delete-cascade, а самостоятельная потребность merged item).
      В fallback-режиме также удаляет эмбеддинги оригиналов.
    * ``recording_chain_mgr`` — merge-специфичная ЗАМЕНА (replace) оригиналов на
      merged item_id в цепочках.  Каноническое удаление лишь УДАЛЯЕТ ghost-ссылки;
      замена сохраняет членство merged-записи в цепочке (W1278 RC-A / W1282).
    """

    def __init__(
        self,
        semantic_searcher: Any | None = None,
        privacy_mode_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._semantic_searcher = semantic_searcher
        # wave-27 MED (privacy): когда privacy_mode активен, merge/preview не должны
        # читать и собирать полный текст транскриптов. Колбэк опрашивается в начале
        # каждой публичной операции; None → гейт выключен (standalone/тесты).
        self._privacy_mode_fn = privacy_mode_fn
        # Late-injection: см. docstring класса.
        self.recording_chain_mgr: Any | None = None
        # wave1776 HIGH 1: каноническая cascade-функция (item_id, item_ts) -> None.
        self.cascade_delete_fn: Callable[[str, str], None] | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def merge_items(
        self,
        item_ids: list[str],
        store: Any,
        *,
        delete_originals: bool = False,
        separator: str = _TIMESTAMP_SEP,
    ) -> dict[str, Any]:
        """Объединяет записи и сохраняет результат в историю.

        Параметры:
            item_ids         — упорядоченный список ID записей для объединения.
            store            — экземпляр StateStore.
            delete_originals — если True, исходные записи помечаются tombstone.
            separator        — разделитель между фрагментами текста.

        Возвращает словарь новой записи.
        Генерирует ValueError при < 2 записях или если часть ID не найдена.

        wave1776 MED 5 (атомарность): для реального StateStore запись merged-записи
        и tombstone'ы всех удаляемых оригиналов выполняются под одним
        ``store._lock()`` через *unlocked*-внутренности (lock — flock на отдельном
        fd, НЕ реентрантен, поэтому вложенные публичные методы взяли бы его второй
        раз → deadlock).  Богатые delete-каскады (.md/semantic/versions/chains)
        выполняются ПОСЛЕ освобождения lock'а — это best-effort шаги, не входящие
        в атомарный инвариант «merged записан И оригиналы tombstoned».

        wave-27 MED (privacy): при активном privacy_mode возвращаем
        ``{"ok": False, "reason": "privacy_mode_active"}`` БЕЗ чтения текста.
        wave-27 MED (DoS): при ``len(item_ids) > MAX_MERGE_ITEMS`` возвращаем
        ``{"ok": False, "reason": "too_many_items", ...}`` ДО любых store-lookup'ов.
        """
        # wave-27 MED (privacy): гейт ДО чтения транскриптов из store.
        if self._privacy_mode_fn is not None and self._privacy_mode_fn():
            return {"ok": False, "reason": "privacy_mode_active"}

        # wave-27 MED (DoS): ограничиваем количество объединяемых записей. Проверяем
        # ДО _load_items, чтобы огромный список не вызвал лавину per-item lookup'ов.
        if len(item_ids) > MAX_MERGE_ITEMS:
            return {
                "ok": False,
                "reason": "too_many_items",
                "max_items": MAX_MERGE_ITEMS,
                "requested": len(item_ids),
            }

        items = self._load_items(item_ids, store)

        # wave1776 MED 4: защищённые (is_protected) оригиналы никогда не удаляются.
        protected_set = {
            item.id for item in items if getattr(item, "is_protected", False)
        }
        protected_ids = [item.id for item in items if item.id in protected_set]
        deletable = [item for item in items if item.id not in protected_set]
        deletable_ids = [item.id for item in deletable]
        # ts захватываем ПОКА оригиналы ещё активны — нужно для .md erase после
        # tombstone'а (после него _load_active_items_unlocked их уже не вернёт).
        ts_by_id = {item.id: getattr(item, "ts", "") for item in deletable}

        merged_data = self._build_merged_data(items, separator)

        if self._supports_atomic_write(store):
            new_item = self._atomic_create_and_tombstone(
                deletable_ids, merged_data, store, delete_originals
            )
        else:
            # Fallback для fake-store в тестах: без единого lock'а.
            new_item = store.add_history_item(**self._merged_kwargs(merged_data))

        # Индексация НОВОЙ объединённой записи в семантическом поиске.
        if self._semantic_searcher is not None:
            try:
                self._semantic_searcher.index_item(new_item.id, new_item.text)
            except Exception:
                logger.warning("semantic_searcher.index_item failed for %s", new_item.id, exc_info=True)

        deleted_ids: list[str] = []
        if delete_originals:
            deleted_ids = self._run_delete_cascades(
                deletable_ids, ts_by_id, new_item, store
            )
            logger.info(
                "Объединено %d записей → %s; удалено %d оригиналов; пропущено защищённых %d",
                len(items), new_item.id, len(deleted_ids), len(protected_ids),
            )
        else:
            logger.info(
                "Объединено %d записей → %s; оригиналы сохранены",
                len(items), new_item.id,
            )

        result = new_item.to_dict()
        result["merged_from"] = [i.id for i in items]
        result["deleted_originals"] = delete_originals
        result["deleted_ids"] = deleted_ids
        result["skipped_protected_ids"] = list(protected_ids)
        return result

    # ------------------------------------------------------------------
    # Atomicity (wave1776 MED 5)
    # ------------------------------------------------------------------

    @staticmethod
    def _supports_atomic_write(store: Any) -> bool:
        """True если store — реальный StateStore с unlocked-внутренностями."""
        return all(
            callable(getattr(store, attr, None))
            for attr in ("_lock", "_append_ndjson")
        ) and all(
            getattr(store, attr, None) is not None
            for attr in ("history_path", "tombstones_path")
        )

    def _atomic_create_and_tombstone(
        self,
        deletable_ids: list[str],
        merged_data: dict[str, Any],
        store: Any,
        delete_originals: bool,
    ) -> Any:
        """Под одним ``store._lock()``: append merged + tombstone оригиналов.

        Возвращает ``new_item``.  Использует unlocked-append, чтобы не брать flock
        второй раз (он не реентрантен → иначе deadlock в одном процессе).
        """
        from backend.models import HistoryItem

        new_item = HistoryItem.create(**self._merged_kwargs(merged_data))

        with store._lock():
            store._append_ndjson(store.history_path, new_item.to_dict())
            if delete_originals:
                for oid in deletable_ids:
                    store._append_ndjson(store.tombstones_path, {"id": oid})

        return new_item

    def _run_delete_cascades(
        self,
        deletable_ids: list[str],
        ts_by_id: dict[str, str],
        new_item: Any,
        store: Any,
    ) -> list[str]:
        """Выполняет богатые delete-каскады для оригиналов (вне lock'а).

        Chain-replace (orig→merged) выполняется ПЕРВЫМ — каноническое удаление
        лишь УДАЛЯЕТ ghost-ссылки, поэтому замену нужно сделать до него (W1282).
        ``ts_by_id`` несёт ts оригиналов, захваченный ДО tombstone'а — нужен для
        стирания .md (после tombstone'а ts уже не найти).
        """
        if not deletable_ids:
            return []

        # wave1776: заменяем оригиналы на merged item_id в цепочках ДО удаления.
        if self.recording_chain_mgr is not None:
            chain_membership: dict[str, list[str]] = {}
            try:
                chain_membership = self.recording_chain_mgr.find_chains_containing(
                    deletable_ids
                )
            except Exception:
                logger.exception(
                    "merge_items: не удалось получить цепочки для %s — пропускаем обновление",
                    deletable_ids,
                )
            for chain_id, matched_ids in chain_membership.items():
                try:
                    changed = self.recording_chain_mgr.replace_items_in_chain(
                        chain_id, matched_ids, new_item.id
                    )
                    if changed:
                        logger.info(
                            "Цепочка %s: заменены %s → %s",
                            chain_id, matched_ids, new_item.id,
                        )
                except Exception:
                    logger.exception(
                        "merge_items: не удалось обновить цепочку %s — ghost refs остаются",
                        chain_id,
                    )

        deleted_ids: list[str] = []
        for oid in deletable_ids:
            if self._delete_one(oid, ts_by_id.get(oid, ""), store):
                deleted_ids.append(oid)
        return deleted_ids

    def _delete_one(self, item_id: str, item_ts: str, store: Any) -> bool:
        """Запускает delete-каскады для одного оригинала.

        wave1776 HIGH 1: при подключённом ``cascade_delete_fn`` tombstone уже
        записан (атомарным append'ом / fallback'ом ниже) — каноническая
        ``cascade_delete_artifacts`` доделывает .md erase (privacy gap), удаление
        эмбеддинга, версий, playback и ghost-refs (по захваченному ts).
        Если cascade_delete_fn None — fallback: прямой tombstone + semantic remove.
        """
        if self.cascade_delete_fn is not None:
            # Гарантируем tombstone (для fake-store без атомарного append'а).
            if not self._supports_atomic_write(store):
                store.delete_history_item(item_id)
            try:
                self.cascade_delete_fn(item_id, item_ts)
            except Exception:
                logger.exception(
                    "merge_items: каскадное удаление не удалось для id=%s", item_id
                )
                return False
            return True

        # Fallback (standalone/тесты): прямой tombstone + semantic remove.
        if self._supports_atomic_write(store):
            # tombstone уже записан атомарной секцией; не дублируем.
            ok = True
        else:
            ok = store.delete_history_item(item_id)
        if not ok:
            return False
        if self._semantic_searcher is not None:
            try:
                self._semantic_searcher.remove_item(item_id)
            except Exception:
                logger.warning("semantic_searcher.remove_item failed for %s", item_id, exc_info=True)
        return True

    def preview_merge(
        self,
        item_ids: list[str],
        store: Any,
        *,
        separator: str = _TIMESTAMP_SEP,
    ) -> dict[str, Any]:
        """Возвращает предпросмотр объединённой записи без записи в историю.

        Ответ содержит те же поля, что вернул бы merge_items, плюс
        ``preview: true`` и ``merged_from`` со списком ID.

        wave-27 MED (privacy): при активном privacy_mode возвращаем
        ``{"ok": False, "reason": "privacy_mode_active"}`` БЕЗ чтения текста.
        """
        # wave-27 MED (privacy): гейт ДО чтения транскриптов из store.
        if self._privacy_mode_fn is not None and self._privacy_mode_fn():
            return {"ok": False, "reason": "privacy_mode_active"}

        items = self._load_items(item_ids, store)
        merged_data = self._build_merged_data(items, separator)

        result: dict[str, Any] = {
            "preview": True,
            "merged_from": [i.id for i in items],
            "item_count": len(items),
        }
        result.update(merged_data)
        return result

    # ------------------------------------------------------------------
    # IPC-обработчики (вызываются из BackendService.handle_request)
    # ------------------------------------------------------------------

    def handle_merge_recordings(self, params: dict[str, Any], store: Any) -> dict[str, Any]:
        """IPC-обёртка для merge_items.

        Ожидаемые параметры:
            item_ids         — список строк-идентификаторов.
            delete_originals — булево (по умолчанию false).
            separator        — необязательный разделитель текста.
        """
        item_ids = self._extract_ids(params)
        delete_originals = bool(params.get("delete_originals", False))
        separator = str(params.get("separator", _TIMESTAMP_SEP))
        # wave-1770 HIGH: reject oversized separator before building merged text.
        if len(separator) > MAX_SEPARATOR_LEN:
            return {
                "ok": False,
                "reason": "separator_too_long",
                "max_separator_len": MAX_SEPARATOR_LEN,
            }
        return self.merge_items(
            item_ids,
            store,
            delete_originals=delete_originals,
            separator=separator,
        )

    def handle_preview_merge(self, params: dict[str, Any], store: Any) -> dict[str, Any]:
        """IPC-обёртка для preview_merge.

        Ожидаемые параметры:
            item_ids  — список строк-идентификаторов.
            separator — необязательный разделитель текста.
        """
        item_ids = self._extract_ids(params)
        separator = str(params.get("separator", _TIMESTAMP_SEP))
        # wave-1770 HIGH: same separator cap as handle_merge_recordings.
        if len(separator) > MAX_SEPARATOR_LEN:
            return {
                "ok": False,
                "reason": "separator_too_long",
                "max_separator_len": MAX_SEPARATOR_LEN,
            }
        return self.preview_merge(item_ids, store, separator=separator)

    # ------------------------------------------------------------------
    # Приватные хелперы
    # ------------------------------------------------------------------

    def _extract_ids(self, params: dict[str, Any]) -> list[str]:
        raw = params.get("item_ids")
        if not isinstance(raw, list):
            raise ValueError("Параметр 'item_ids' должен быть списком строк")
        return [str(x).strip() for x in raw if str(x).strip()]

    def _load_items(self, item_ids: list[str], store: Any) -> list[Any]:
        """Загружает и валидирует список записей по ID."""
        if len(item_ids) < 2:
            raise ValueError("Для объединения нужно минимум 2 записи")

        items = []
        missing: list[str] = []
        for iid in item_ids:
            item = store.get_history_item_by_id(iid)
            if item is None:
                missing.append(iid)
            else:
                items.append(item)

        if missing:
            raise ValueError(f"Записи не найдены: {missing}")

        # Сортируем по времени (от старых к новым), чтобы порядок текста
        # был хронологическим вне зависимости от переданного item_ids.
        items.sort(key=lambda i: i.ts)
        return items

    @staticmethod
    def _merged_kwargs(merged_data: dict[str, Any]) -> dict[str, Any]:
        """Преобразует merged_data в kwargs для add_history_item / HistoryItem.create."""
        return {
            "text": merged_data["text"],
            "paste_status": "merged",
            "source_text": merged_data["source_text"],
            "translated_text": merged_data["translated_text"],
            "translation_mode": merged_data["translation_mode"],
            "source_lang": merged_data["source_lang"],
            "target_lang": merged_data["target_lang"],
            "translation_status": merged_data["translation_status"],
            "diarization": merged_data["diarization"],
            "audio_duration_sec": merged_data["audio_duration_sec"],
            "confidence": merged_data["confidence"],
            "tags": merged_data["tags"],
            # wave1776 MED 3: ранее молча терялись.
            "favorite": merged_data["favorite"],
            "is_protected": merged_data["is_protected"],
            "privacy_mode": merged_data["privacy_mode"],
            "audio_path": merged_data["audio_path"],
            "word_timestamps": merged_data["word_timestamps"],
            "speaker_turns": merged_data["speaker_turns"],
        }

    def _build_merged_data(self, items: list[Any], separator: str) -> dict[str, Any]:
        """Вычисляет агрегированные поля для новой объединённой записи."""
        # --- Текст с временны́ми метками ---
        text_parts: list[str] = []
        source_parts: list[str] = []
        translated_parts: list[str] = []

        for item in items:
            ts_label = self._format_ts_label(item.ts)
            text_parts.append(f"[{ts_label}] {item.text}" if ts_label else item.text)
            if item.source_text:
                source_parts.append(f"[{ts_label}] {item.source_text}" if ts_label else item.source_text)
            if item.translated_text:
                translated_parts.append(f"[{ts_label}] {item.translated_text}" if ts_label else item.translated_text)

        merged_text = separator.join(text_parts)
        merged_source = separator.join(source_parts) if source_parts else ""
        merged_translated = separator.join(translated_parts) if translated_parts else ""

        # --- Длительность: сумма ---
        durations = [i.audio_duration_sec for i in items if i.audio_duration_sec is not None]
        total_duration: float | None = round(sum(durations), 3) if durations else None

        # --- Уверенность: среднее по тем, у кого она есть ---
        confidences = [i.confidence for i in items if i.confidence is not None]
        avg_confidence: float | None = (
            round(sum(confidences) / len(confidences), 4) if confidences else None
        )

        # --- Дiarization: объединяем speaker_segments ---
        merged_diarization = self._merge_diarization([i.diarization for i in items])

        # --- Теги: объединяем уникальные ---
        all_tags: list[str] = []
        seen_tags: set[str] = set()
        for item in items:
            for tag in (item.tags or []):
                t = str(tag).strip()
                if t and t not in seen_tags:
                    seen_tags.add(t)
                    all_tags.append(t)

        # --- Язык/режим перевода берём у первой записи с непустым значением ---
        translation_mode = next(
            (i.translation_mode for i in items if i.translation_mode and i.translation_mode != "off"),
            "off",
        )
        source_lang = next((i.source_lang for i in items if i.source_lang), "")
        target_lang = next((i.target_lang for i in items if i.target_lang), "")
        translation_status = next(
            (i.translation_status for i in items if i.translation_status and i.translation_status != "not_requested"),
            "not_requested",
        )

        # --- wave1776 MED 3: ранее молча терялись ---
        # is_protected / privacy_mode / favorite: OR-агрегация (True если хотя бы у
        # одного источника True).  Для protected/privacy это fail-safe: объединение
        # не должно понижать защиту/приватность.
        merged_favorite = any(bool(getattr(i, "favorite", False)) for i in items)
        merged_is_protected = any(bool(getattr(i, "is_protected", False)) for i in items)
        merged_privacy_mode = any(bool(getattr(i, "privacy_mode", False)) for i in items)

        # word_timestamps / speaker_turns: конкатенация в хронологическом порядке.
        merged_word_timestamps: list = []
        merged_speaker_turns: list = []
        for item in items:
            wt = getattr(item, "word_timestamps", None)
            if isinstance(wt, list):
                merged_word_timestamps.extend(wt)
            st = getattr(item, "speaker_turns", None)
            if isinstance(st, list):
                merged_speaker_turns.extend(st)

        # audio_path: первый непустой путь — нельзя слить несколько файлов в один,
        # поэтому сохраняем первый осмысленный для воспроизведения/повторов.
        merged_audio_path = next(
            (str(getattr(i, "audio_path", "")) for i in items if str(getattr(i, "audio_path", "")).strip()),
            "",
        )

        return {
            "text": merged_text,
            "source_text": merged_source,
            "translated_text": merged_translated,
            "audio_duration_sec": total_duration,
            "confidence": avg_confidence,
            "diarization": merged_diarization,
            "tags": all_tags,
            "translation_mode": translation_mode,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "translation_status": translation_status,
            "favorite": merged_favorite,
            "is_protected": merged_is_protected,
            "privacy_mode": merged_privacy_mode,
            "word_timestamps": merged_word_timestamps or None,
            "speaker_turns": merged_speaker_turns or None,
            "audio_path": merged_audio_path,
        }

    @staticmethod
    def _merge_diarization(diarizations: list[dict | None]) -> dict | None:
        """Объединяет сегменты дiarization из нескольких записей."""
        valid = [d for d in diarizations if isinstance(d, dict)]
        if not valid:
            return None

        merged_segments: list[dict] = []
        for d in valid:
            segs = d.get("speaker_segments") or d.get("segments") or []
            if isinstance(segs, list):
                merged_segments.extend(segs)

        if not merged_segments:
            # Возвращаем первый не-None diarization целиком как запасной вариант
            return valid[0]

        return {"speaker_segments": merged_segments, "merged": True}

    @staticmethod
    def _format_ts_label(ts: str) -> str:
        """Форматирует ISO-timestamp в читаемую метку вида «HH:MM»."""
        try:
            dt = datetime.fromisoformat(ts)
            return dt.strftime("%H:%M")
        except Exception:
            return ts
