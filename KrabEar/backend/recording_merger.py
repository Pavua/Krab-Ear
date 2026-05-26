"""RecordingMerger — объединение нескольких записей истории в одну.

Поддерживает предварительный просмотр (preview_merge) и финальное слияние
(merge_items) с опциональным удалением исходных записей.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger("KrabEar.Backend.RecordingMerger")

_TIMESTAMP_SEP = "\n\n"


class RecordingMerger:
    """Объединяет несколько записей истории в одну.

    Конструктор не требует store — он передаётся явно в каждый метод,
    чтобы упростить тестирование и следовать паттерну других сервисов.
    """

    def __init__(self, transcript_versioner: Any | None = None) -> None:
        self._transcript_versioner = transcript_versioner

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
        """
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
            tags=merged_data["tags"],
        )

        if delete_originals:
            deleted_ids: list[str] = []
            for item in items:
                if store.delete_history_item(item.id):
                    deleted_ids.append(item.id)
                    # W1254 F1: purge version cascade on merge-delete
                    if self._transcript_versioner is not None:
                        try:
                            self._transcript_versioner.purge_versions_for_item(item.id)
                        except Exception:
                            logger.exception(
                                "merge_items: не удалось удалить версии для id=%s", item.id
                            )
            logger.info(
                "Объединено %d записей → %s; удалено %d оригиналов",
                len(items),
                new_item.id,
                len(deleted_ids),
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
