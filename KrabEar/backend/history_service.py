"""HistoryService — обработчики IPC-методов управления историей Krab Ear.

Выделен из backend/service.py для снижения размера монолитного модуля.
Содержит 13 IPC-обработчиков + форматирующие хелперы.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.HistoryService")


class HistoryService:
    """Обработчики IPC-команд для истории транскрипций."""

    def __init__(
        self,
        store: Any,
        clipboard_history: list[dict] | None = None,
    ) -> None:
        self.store = store
        # Разделяемый список clipboard_history из BackendService (передаётся по ссылке).
        # Если не передан — создаём изолированный список (для тестов).
        self._clipboard_history: list[dict] = clipboard_history if clipboard_history is not None else []

    # ------------------------------------------------------------------
    # История
    # ------------------------------------------------------------------

    def handle_add_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("Пустой текст нельзя добавить в историю")
        paste_status = str(params.get("paste_status", "failed"))
        item = self.store.add_history_item(
            text=text,
            paste_status=paste_status,
            source_text=str(params.get("source_text", "")).strip(),
            translated_text=str(params.get("translated_text", "")).strip(),
            translation_mode=str(params.get("translation_mode", "off")).strip() or "off",
            source_lang=str(params.get("source_lang", "")).strip(),
            target_lang=str(params.get("target_lang", "")).strip(),
            translation_status=str(params.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(params.get("translation_engine", "")).strip(),
        )
        return item.to_dict()

    def handle_get_history_page(self, params: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.get_history_page_filtered(
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def handle_search_history(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.search_history(
            query=query,
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def handle_delete_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise ValueError("id обязателен для удаления")
        ok = self.store.delete_history_item(item_id)
        if not ok:
            raise ValueError(f"Запись не найдена: {item_id}")
        return {"deleted": True}

    def handle_compact_history(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = self.store.compact_with_stats()
        return {"compacted": True, **stats}

    def handle_import_history_ndjson(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует историю из внешнего NDJSON-файла."""
        raw_path = str(params.get("path", "")).strip()
        if not raw_path:
            raise RuntimeError("path обязателен")
        resolved = Path(raw_path).expanduser().resolve()
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            return {"error": {"message": f"Path outside allowed directories: {resolved}"}}
        result = self.store.import_history_ndjson(resolved)
        return {
            "path": raw_path,
            "imported": int(result.get("imported", 0)),
            "skipped": int(result.get("skipped", 0)),
            "errors": int(result.get("errors", 0)),
        }

    def handle_get_history_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает состояние журналов истории и оценку размера."""
        return self.store.get_history_stats()

    def handle_get_history_overview(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает обзорный срез истории для панели управления."""
        return self.store.get_history_overview()

    def handle_get_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает полные детали одной записи истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        for item in items:
            if item.id == item_id:
                result = item.to_dict()
                result["text_length"] = len(item.text)
                result["word_count"] = len(item.text.split()) if item.text else 0
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                matching = list(transcripts_dir.glob(f"*{item_id[:8]}*")) if transcripts_dir.exists() else []
                result["transcript_file"] = str(matching[0]) if matching else None
                return result

        raise RuntimeError(f"Запись {item_id} не найдена")

    # ------------------------------------------------------------------
    # Экспорт истории (markdown / SRT)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration_human(seconds: float | None) -> str:
        """Форматирует длительность аудио в читаемый вид: '5м 23с'."""
        if seconds is None or seconds <= 0:
            return ""
        total = int(seconds)
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    @staticmethod
    def _format_ts_human(iso_ts: str) -> str:
        """Преобразует ISO timestamp в читаемый формат: '2026-04-11 22:46'."""
        try:
            dt = datetime.fromisoformat(iso_ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso_ts

    def handle_export_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует всю историю в формате Markdown с метаданными и диаризацией.

        Параметры:
            limit (int): максимальное количество записей (по умолчанию 500)
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): markdown-текст
            total_items (int): количество экспортированных записей
            path (str|None): путь к файлу, если save_to_file=True
        """
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None, limit=limit,
            paste_status=None, translation_mode=None,
        )
        if not items_dicts:
            return {"content": "# Krab Ear — Экспорт истории\n\nИстория пуста.\n", "total_items": 0, "path": None}

        from backend.models import HistoryItem as _HI
        items = [_HI.from_dict(d) for d in items_dicts]

        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        header_lines = [
            "# Krab Ear — Экспорт истории",
            f"- Записей: {len(items)}",
            f"- Период: {earliest_ts} — {latest_ts}",
            f"- Экспорт: {export_ts}",
            "",
            "---",
            "",
        ]

        sections: list[str] = []
        for idx, item in enumerate(items, start=1):
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)
            title_parts = [f"## {idx}. [{ts_human}]"]
            if duration_str:
                title_parts.append(f"({duration_str})")
            sections.append(" ".join(title_parts))

            meta_parts: list[str] = []
            if item.source_lang:
                meta_parts.append(f"**Язык:** {item.source_lang}")
            diar = item.diarization
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2:
                    meta_parts.append(f"**Спикеры:** {len(speakers)}")
            if meta_parts:
                sections.append(" | ".join(meta_parts))
                sections.append("")

            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2 and turns:
                    for turn in turns:
                        speaker = turn.get("speaker", "?")
                        turn_text = str(turn.get("text", "")).strip()
                        if turn_text:
                            sections.append(f"[{speaker}]: {turn_text}")
                else:
                    sections.append(item.text)
            else:
                sections.append(item.text)

            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or ""
                sections.append("")
                sections.append(f"**Перевод** ({mode_label}):")
                sections.append(item.translated_text)

            sections.append("")

        content = "\n".join(header_lines) + "\n".join(sections)

        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
                file_path = transcripts_dir / filename
                file_path.write_text(content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить экспорт в файл: %s", exc)

        return {"content": content, "total_items": len(items), "path": save_path}

    def handle_export_history_srt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует запись истории в формате SRT-субтитров (по speaker_turns).

        Параметры:
            id (str): идентификатор записи в истории
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): SRT-текст
            item_id (str): ID записи
            speakers (int): количество спикеров
            segments (int): количество сегментов
            path (str|None): путь к файлу, если save_to_file=True
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        from backend.models import HistoryItem as _HI
        target_item: _HI | None = None
        cursor: str | None = None
        for _ in range(200):
            page_dicts, next_cursor = self.store.get_history_page_filtered(
                cursor=cursor, limit=100,
                paste_status=None, translation_mode=None,
            )
            if not page_dicts:
                break
            for d in page_dicts:
                if d.get("id") == item_id:
                    target_item = _HI.from_dict(d)
                    break
            if target_item is not None:
                break
            if next_cursor is None:
                break
            cursor = next_cursor

        if target_item is None:
            raise RuntimeError(f"Запись не найдена: {item_id}")

        diar = target_item.diarization
        if not diar or not isinstance(diar, dict) or not diar.get("enabled"):
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        turns = diar.get("speaker_turns", [])
        if not turns:
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        speakers = {t.get("speaker") for t in turns if t.get("speaker")}
        srt_lines: list[str] = []
        for seq, turn in enumerate(turns, start=1):
            speaker = turn.get("speaker", "SPEAKER_00")
            turn_text = str(turn.get("text", "")).strip()
            if not turn_text:
                continue
            start_sec = float(turn.get("start", 0.0) or 0.0)
            end_sec = float(turn.get("end", start_sec + 1.0) or start_sec + 1.0)
            srt_lines.append(str(seq))
            srt_lines.append(f"{self._srt_timestamp(start_sec)} --> {self._srt_timestamp(end_sec)}")
            srt_lines.append(f"[{speaker}]: {turn_text}")
            srt_lines.append("")

        srt_content = "\n".join(srt_lines)
        return self._finalize_srt_export(
            params, srt_content, item_id,
            speakers=len(speakers), segments=len(turns),
        )

    def _finalize_srt_export(
        self,
        params: dict[str, Any],
        srt_content: str,
        item_id: str,
        speakers: int,
        segments: int,
    ) -> dict[str, Any]:
        """Общая финализация SRT-экспорта: опциональное сохранение в файл."""
        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"srt_{item_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.srt"
                file_path = transcripts_dir / filename
                file_path.write_text(srt_content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("Не удалось сохранить SRT в файл: %s", exc)
        return {
            "content": srt_content,
            "item_id": item_id,
            "speakers": speakers,
            "segments": segments,
            "path": save_path,
        }

    @staticmethod
    def _build_srt_single(text: str, duration: float) -> str:
        """Строит SRT с одним сегментом (без диаризации)."""
        end_ts = HistoryService._srt_timestamp(duration) if duration > 0 else "00:00:01,000"
        return f"1\n00:00:00,000 --> {end_ts}\n{text}\n"

    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        """Конвертирует секунды в SRT-формат: HH:MM:SS,mmm."""
        if seconds < 0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000))
        h, remainder = divmod(total_ms, 3600000)
        m, remainder = divmod(remainder, 60000)
        s, ms = divmod(remainder, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # ------------------------------------------------------------------
    # Clipboard history
    # ------------------------------------------------------------------

    def handle_get_clipboard_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние N вставленных транскрипций из clipboard_history.

        Параметры:
            limit (int): максимальное количество элементов (по умолчанию 10, макс 20)

        Возвращает:
            items (list): список записей {text, ts, history_id}
            count (int): общее количество элементов в истории
        """
        limit = self._coerce_bounded_int(
            value=params.get("limit", 10),
            default=10,
            min_value=1,
            max_value=20,
        )
        return {
            "items": self._clipboard_history[-limit:],
            "count": len(self._clipboard_history),
        }

    def handle_repaste_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Находит текст по history_id в clipboard_history и возвращает его для повторной вставки.

        Параметры:
            history_id (str): идентификатор записи из clipboard_history

        Возвращает:
            text (str): текст для вставки
            history_id (str): подтверждённый идентификатор
            found (bool): True если запись найдена
        """
        history_id = str(params.get("history_id", "")).strip()
        if not history_id:
            raise RuntimeError("history_id обязателен")
        for entry in reversed(self._clipboard_history):
            if entry.get("history_id") == history_id:
                return {
                    "text": entry["text"],
                    "history_id": history_id,
                    "found": True,
                }
        raise RuntimeError(f"Запись не найдена в clipboard_history: {history_id}")

    # ------------------------------------------------------------------
    # Очистка и хранилище
    # ------------------------------------------------------------------

    def handle_cleanup_old_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет записи истории старше N дней (по умолчанию 90).

        Параметры:
            older_than_days (int): порог возраста в днях (по умолчанию 90)

        Возвращает:
            deleted (int): количество удалённых записей
            remaining (int): количество оставшихся активных записей
        """
        older_than_days = int(params.get("older_than_days", 90))
        if older_than_days <= 0:
            raise RuntimeError("older_than_days должен быть положительным числом")

        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.isoformat()

        with self.store._lock():
            active = self.store._load_active_items_unlocked()
            to_delete = [item for item in active if item.ts < cutoff_iso]
            for item in to_delete:
                self.store._append_ndjson(self.store.tombstones_path, {"id": item.id})
            remaining = len(active) - len(to_delete)

        return {"deleted_count": len(to_delete), "remaining": remaining}

    def handle_get_storage_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает информацию о размере файлов данных Krab Ear.

        Возвращает:
            history_bytes (int): размер history.ndjson в байтах
            history_file_size_mb (float): размер history.ndjson в МБ
            transcripts_count (int): количество .md файлов в transcripts/
            transcripts_size_mb (float): суммарный размер transcripts/ в МБ
            reports_count (int): количество файлов-отчётов в data_dir
            total_bytes (int): суммарный размер директории данных в байтах
            total_data_mb (float): суммарный размер директории данных в МБ
        """
        data_dir = Path(self.store.data_dir)

        history_path = self.store.history_path
        history_bytes = history_path.stat().st_size if history_path.exists() else 0
        history_size_mb = history_bytes / (1024 * 1024)

        transcripts_dir = data_dir / "transcripts"
        md_files = list(transcripts_dir.glob("*.md")) if transcripts_dir.exists() else []
        transcripts_count = len(md_files)
        transcripts_size_mb = sum(f.stat().st_size for f in md_files) / (1024 * 1024)

        reports_count = len(list(data_dir.glob("*.report")) + list(data_dir.glob("report_*")))

        total_bytes = sum(
            f.stat().st_size
            for f in data_dir.rglob("*")
            if f.is_file()
        )
        total_data_mb = total_bytes / (1024 * 1024)

        return {
            "history_bytes": history_bytes,
            "history_file_size_mb": round(history_size_mb, 3),
            "transcripts_count": transcripts_count,
            "transcripts_size_mb": round(transcripts_size_mb, 3),
            "reports_count": reports_count,
            "total_bytes": total_bytes,
            "total_data_mb": round(total_data_mb, 3),
        }

    # ------------------------------------------------------------------
    # Статические хелперы (копированы из BackendService для автономности)
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        """Нормализует bool-поля из UI/JSON с поддержкой строковых значений."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False
        return default

    @staticmethod
    def _coerce_bounded(
        value: Any,
        default: int | float,
        min_value: int | float,
        max_value: int | float,
    ) -> int | float:
        """Нормализует числовое значение в допустимый диапазон."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    _coerce_bounded_int = _coerce_bounded
    _coerce_bounded_float = _coerce_bounded
