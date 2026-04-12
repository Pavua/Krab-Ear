"""TranscriptWriter — автоматическая запись транскрибаций в .md файлы.

Используется BackendService при AUTO_SAVE_TRANSCRIPTS=True.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.TranscriptWriter")


class TranscriptWriter:
    """Записывает транскрибацию в Markdown-файл в формате Obsidian."""

    @staticmethod
    def _format_duration(seconds: float | None) -> str:
        """Форматирует длительность: '5м 23с'."""
        if not seconds or seconds <= 0:
            return "—"
        total = int(seconds)
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    @staticmethod
    def _format_date_human(ts: str | None) -> str:
        """Форматирует ISO timestamp в '12 апреля 2026, 22:46'."""
        MONTHS_RU = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря",
        ]
        if not ts:
            return datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            dt = datetime.fromisoformat(ts)
            return f"{dt.day} {MONTHS_RU[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')}"
        except (ValueError, TypeError):
            return ts

    @staticmethod
    def _build_text_section(item: dict[str, Any]) -> str:
        """Формирует секцию текста с учётом диаризации."""
        diar = item.get("diarization")
        if diar and isinstance(diar, dict) and diar.get("enabled"):
            turns = diar.get("speaker_turns", [])
            speakers = {t.get("speaker") for t in turns if t.get("speaker")}
            if len(speakers) >= 2 and turns:
                lines = []
                for turn in turns:
                    speaker = turn.get("speaker", "?")
                    turn_text = str(turn.get("text", "")).strip()
                    if turn_text:
                        lines.append(f"**[{speaker}]:** {turn_text}")
                return "\n\n".join(lines) if lines else item.get("text", "")
        return item.get("text", "")

    @classmethod
    def build_content(cls, item: dict[str, Any]) -> str:
        """Генерирует содержимое .md файла для одной транскрибации."""
        ts = item.get("ts")
        try:
            date_str = datetime.fromisoformat(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_str = datetime.now().strftime("%Y-%m-%d")

        date_human = cls._format_date_human(ts)
        duration = cls._format_duration(item.get("audio_duration_sec"))
        confidence = item.get("confidence", 0.0)
        confidence_pct = f"{round((confidence or 0) * 100)}%" if confidence else "—"

        text_section = cls._build_text_section(item)

        lines = [
            f"# Транскрибация ({date_str})",
            "",
            f"**Дата:** {date_human}",
            f"**Длительность:** {duration}",
            f"**Качество:** {confidence_pct}",
            "**Теги:** #transcription #krab-ear",
            "",
            "---",
            "",
            "## Текст",
            "",
            text_section,
        ]

        translated_text = item.get("translated_text", "").strip()
        translation_status = item.get("translation_status", "")
        if translated_text and translation_status == "ok":
            lines += [
                "",
                "---",
                "",
                "## Перевод",
                "",
                translated_text,
            ]

        lines.append("")
        return "\n".join(lines)

    @classmethod
    def write_transcript(cls, item: dict[str, Any], output_dir: Path) -> Path:
        """Записывает транскрибацию в .md файл.

        Аргументы:
            item: словарь с полями транскрибации (text, ts, audio_duration_sec, ...)
            output_dir: директория для сохранения (будет создана при необходимости)

        Возвращает:
            Path: путь к созданному файлу
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = item.get("ts")
        try:
            date_str = datetime.fromisoformat(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_str = datetime.now().strftime("%Y-%m-%d")

        filename = f"{date_str}-Транскрибация.md"
        file_path = output_dir / filename

        # Если файл на эту дату уже есть — добавляем время как суффикс
        if file_path.exists():
            try:
                time_str = datetime.fromisoformat(ts).strftime("%H%M%S") if ts else datetime.now().strftime("%H%M%S")
            except (ValueError, TypeError):
                time_str = datetime.now().strftime("%H%M%S")
            filename = f"{date_str}-Транскрибация-{time_str}.md"
            file_path = output_dir / filename

        content = cls.build_content(item)
        file_path.write_text(content, encoding="utf-8")
        logger.info("Транскрибация сохранена: %s", file_path)
        return file_path
