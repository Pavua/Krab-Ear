"""RecordingMerger — объединение нескольких записей истории в одну.

Поддерживает предварительный просмотр (preview_merge) и финальное слияние
(merge_items) с опциональным удалением исходных записей.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("KrabEar.Backend.RecordingMerger")

_TIMESTAMP_SEP = "\n\n"


class MergeRollbackError(RuntimeError):
    """Raised when the delete phase of merge_items fails mid-loop.

    The new merged item has been created but some originals could not be
    tombstoned.  A best-effort rollback tombstone was applied to the merged
    item; callers should surface this error to the user rather than silently
    swallowing it.

    Attributes:
        new_item_id    — ID of the merged item that was created.
        deleted_ids    — IDs of originals successfully tombstoned before failure.
        failed_id      — ID of the original whose delete triggered the exception.
        rollback_ok    — True if the best-effort rollback tombstone succeeded.
    """

    def __init__(
        self,
        message: str,
        *,
        new_item_id: str,
        deleted_ids: list[str],
        failed_id: str,
        rollback_ok: bool,
        cause: BaseException,
    ) -> None:
        super().__init__(message)
        self.new_item_id = new_item_id
        self.deleted_ids = deleted_ids
        self.failed_id = failed_id
        self.rollback_ok = rollback_ok
        self.__cause__ = cause


class RecordingMerger:
    """Объединяет несколько записей истории в одну.

    Конструктор не требует store — он передаётся явно в каждый метод,
    чтобы упростить тестирование и следовать паттерну других сервисов.

    Атрибут ``recording_chain_mgr`` — поздняя инъекция (late-injection):
    инициализируется None, устанавливается из BackendService после создания
    обоих объектов, чтобы избежать циклической зависимости при конструировании.
    Когда установлен, merge_items автоматически обновляет цепочки при
    ``delete_originals=True``.
    """

    def __init__(self) -> None:
        self.recording_chain_mgr: Any | None = None

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

        Idempotency: при delete_originals=False повторный вызов с теми же
        item_ids возвращает уже существующую объединённую запись без создания
        дубликата. Ключ идемпотентности хранится в метаданных записи как
        ``merge_key``. При delete_originals=True проверка пропускается — вызов
        всегда создаёт новую запись (оригиналы уже удалены).
        """
        # --- Idempotency guard (только когда оригиналы не удаляются) ---
        merge_key = hashlib.sha256(
            ",".join(sorted(item_ids)).encode()
        ).hexdigest()[:16]

        if not delete_originals:
            existing = self._find_by_merge_key(merge_key, store)
            if existing is not None:
                logger.info(
                    "Идемпотентный merge: merge_key=%s уже существует → %s",
                    merge_key,
                    existing.id,
                )
                result = existing.to_dict()
                result["merged_from"] = list(sorted(item_ids))
                result["deleted_originals"] = False
                result["idempotent"] = True
                return result

        items = self._load_items(item_ids, store)
        merged_data = self._build_merged_data(items, separator)

        new_item = store.add_history_item(
            text=merged_data["text"],
            paste_status="merged",
            source_text=merged_data["source_text"],
            translated_text=merged_data["translated_text"],
            translation_mode=merged_data["translation_mode"],
            source_lang=merged_data["source_lang"],
            target_lang=merged_data["target_lang"],
            translation_status=merged_data["translation_status"],
            diarization=merged_data["diarization"],
            audio_duration_sec=merged_data["audio_duration_sec"],
            confidence=merged_data["confidence"],
        )

        # Теги сохраняем отдельным вызовом: StateStore.add_history_item не
        # принимает параметр tags (W1237 не смёрджен), поэтому используем
        # выделенный метод update_history_item_tags.
        if merged_data["tags"]:
            store.update_history_item_tags(new_item.id, merged_data["tags"])

        if delete_originals:
            original_ids = [item.id for item in items]

            # --- Step 1: capture chain memberships BEFORE deletion ---
            chain_membership: dict[str, list[str]] = {}
            if self.recording_chain_mgr is not None:
                try:
                    chain_membership = self.recording_chain_mgr.find_chains_containing(
                        original_ids
                    )
                except Exception:
                    logger.exception(
                        "Не удалось получить цепочки для %s — пропускаем обновление цепочек",
                        original_ids,
                    )

            # Phase 2: delete originals — transactional with rollback.
            # If any delete raises mid-loop the new merged item is tombstoned
            # (best-effort) and a MergeRollbackError is re-raised so the caller
            # can surface the failure to the user.
            deleted_ids: list[str] = []
            _last_item = None
            try:
                for item in items:
                    _last_item = item
                    if store.delete_history_item(item.id):
                        deleted_ids.append(item.id)
            except Exception as exc:  # noqa: BLE001
                failed_id = _last_item.id if _last_item is not None else "unknown"
                logger.exception(
                    "Ошибка удаления оригинала %s при слиянии → %s; "
                    "откат: tombstone новой записи",
                    failed_id,
                    new_item.id,
                )
                # Best-effort rollback: tombstone the newly created merged item.
                rollback_ok = False
                try:
                    store.delete_history_item(new_item.id)
                    rollback_ok = True
                    logger.info(
                        "Откат слияния выполнен: запись %s помечена tombstone",
                        new_item.id,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Не удалось откатить объединённую запись %s — "
                        "требуется ручная очистка",
                        new_item.id,
                    )
                raise MergeRollbackError(
                    f"Слияние прервано при удалении оригинала {failed_id!r}; "
                    f"откат {'выполнен' if rollback_ok else 'НЕ ВЫПОЛНЕН'}",
                    new_item_id=new_item.id,
                    deleted_ids=deleted_ids,
                    failed_id=failed_id,
                    rollback_ok=rollback_ok,
                    cause=exc,
                ) from exc

            # --- Step 2: replace originals with merged item in each chain ---
            if chain_membership and self.recording_chain_mgr is not None:
                for chain_id, matched_ids in chain_membership.items():
                    try:
                        changed = self.recording_chain_mgr.replace_items_in_chain(
                            chain_id, matched_ids, new_item.id
                        )
                        if changed:
                            logger.info(
                                "Цепочка %s: заменены %s → %s",
                                chain_id,
                                matched_ids,
                                new_item.id,
                            )
                    except Exception:
                        logger.exception(
                            "Не удалось обновить цепочку %s — ghost refs остаются",
                            chain_id,
                        )

            logger.info(
                "Объединено %d записей → %s; удалено %d оригиналов; цепочек обновлено %d",
                len(items),
                new_item.id,
                len(deleted_ids),
                len(chain_membership),
            )
        else:
            logger.info(
                "Объединено %d записей → %s; оригиналы сохранены",
                len(items),
                new_item.id,
            )

        result = new_item.to_dict()
        result["merged_from"] = [i.id for i in items]
        result["deleted_originals"] = delete_originals
        return result

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
        """
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
        return self.preview_merge(item_ids, store, separator=separator)

    # ------------------------------------------------------------------
    # Приватные хелперы
    # ------------------------------------------------------------------

    def _find_by_merge_key(self, merge_key: str, store: Any) -> Any | None:
        """Ищет существующую объединённую запись с данным merge_key в store.

        Использует ``get_merged_item_by_key`` если оно доступно (быстрый путь),
        иначе делает полный перебор через ``get_history_items``.
        Возвращает первый найденный элемент или None.
        """
        # Быстрый путь — некоторые реализации store предоставляют индексированный доступ
        if hasattr(store, "get_merged_item_by_key"):
            return store.get_merged_item_by_key(merge_key)

        # Медленный путь — полный перебор
        if hasattr(store, "get_history_items"):
            try:
                all_items = store.get_history_items()
            except Exception:
                return None
            for item in (all_items or []):
                item_meta = getattr(item, "merge_key", None)
                if item_meta == merge_key:
                    return item
        return None

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
