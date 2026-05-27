"""TranscriptWriter — автоматическая запись транскрибаций в .md файлы.

Используется BackendService при AUTO_SAVE_TRANSCRIPTS=True.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.TranscriptWriter")


class TranscriptWriter:
    """Записывает транскрибацию в Markdown-файл в формате Obsidian."""

    # Класс-уровневый мьютекс: сериализует collision-resolution + запись,
    # исключая TOCTOU-гонку при одновременных вызовах write_transcript.
    _write_lock: threading.Lock = threading.Lock()

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
    def _atomic_write(cls, path: Path, content: str) -> None:
        """Записывает content в path атомарно: tmp → fsync → rename.

        Гарантирует отсутствие частичных файлов при сбое процесса.
        """
        tmp_path = path.with_suffix(".md.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        except Exception:
            # Убираем мусорный .tmp при любой ошибке
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    @classmethod
    def _reserve_path(cls, output_dir: Path, date_str: str, ts: str | None) -> Path:
        """Атомарно резервирует уникальный путь для нового файла.

        Алгоритм:
        1. Пробуем создать базовый файл через O_CREAT|O_EXCL (атомарно).
        2. При FileExistsError добавляем временной суффикс, затем числовые -1/-2/... пока не получим эксклюзивный доступ.

        Возвращает Path к пустому зарезервированному файлу (будет перезаписан через _atomic_write).
        """
        # Шаг 1: базовое имя
        base_path = output_dir / f"{date_str}-Транскрибация.md"
        try:
            fd = os.open(base_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return base_path
        except FileExistsError:
            pass

        # Шаг 2: добавляем время из ts, если доступно
        try:
            time_str = datetime.fromisoformat(ts).strftime("%H%M%S") if ts else datetime.now().strftime("%H%M%S")
        except (ValueError, TypeError):
            time_str = datetime.now().strftime("%H%M%S")

        time_path = output_dir / f"{date_str}-Транскрибация-{time_str}.md"
        try:
            fd = os.open(time_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return time_path
        except FileExistsError:
            pass

        # Шаг 3: числовые суффиксы -1, -2, ... до победы
        counter = 1
        while True:
            candidate = output_dir / f"{date_str}-Транскрибация-{time_str}-{counter}.md"
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return candidate
            except FileExistsError:
                counter += 1
                if counter > 9999:  # защита от бесконечного цикла
                    raise RuntimeError(f"Не удалось зарезервировать уникальный путь в {output_dir}")

    @classmethod
    def write_transcript(cls, item: dict[str, Any], output_dir: Path) -> Path:
        """Записывает транскрибацию в .md файл.

        Аргументы:
            item: словарь с полями транскрибации (text, ts, audio_duration_sec, ...)
            output_dir: директория для сохранения (будет создана при необходимости)

        Возвращает:
            Path: путь к созданному файлу

        Потокобезопасность:
            _write_lock сериализует reservation + write, устраняя TOCTOU-гонку.
        Атомарность:
            Запись идёт через tmp → fsync → os.replace — нет частичных файлов.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = item.get("ts")
        try:
            date_str = datetime.fromisoformat(ts).strftime("%Y-%m-%d") if ts else datetime.now().strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_str = datetime.now().strftime("%Y-%m-%d")

        content = cls.build_content(item)

        with cls._write_lock:
            file_path = cls._reserve_path(output_dir, date_str, ts)
            try:
                cls._atomic_write(file_path, content)
            except Exception:
                # Снимаем резервацию при ошибке записи
                try:
                    file_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise

        logger.info("Транскрибация сохранена: %s", file_path)
        return file_path
