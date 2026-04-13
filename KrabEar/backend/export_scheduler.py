"""Планировщик автоматического экспорта истории Krab Ear.

ExportScheduler выполняет экспорт оппортунистически — при вызове
check_and_export() — без фоновых потоков.
Расписание хранится в файле export_schedule.json в data_dir.
Поддерживает форматы: srt, csv, markdown, json, obsidian, html.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ExportScheduler")

# Форматы, поддерживаемые планировщиком
SUPPORTED_FORMATS = {"srt", "csv", "markdown", "json", "obsidian", "html"}

# Максимальное количество хранимых экспортов
MAX_EXPORTS_DEFAULT = 30


class ExportScheduler:
    """Управляет автоматическим плановым экспортом истории.

    Thread-safe. Не создаёт фоновых потоков — экспорт происходит
    только при явном вызове check_and_export().
    """

    SCHEDULE_FILENAME = "export_schedule.json"

    def __init__(self, data_dir: Path | str, max_exports: int = MAX_EXPORTS_DEFAULT) -> None:
        """
        Args:
            data_dir: директория данных (та же, что у StateStore).
            max_exports: максимальное количество файлов экспорта на диске.
        """
        self.data_dir = Path(data_dir)
        self.max_exports = max_exports
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Вспомогательные свойства
    # ------------------------------------------------------------------

    @property
    def _schedule_path(self) -> Path:
        return self.data_dir / self.SCHEDULE_FILENAME

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "auto_exports"

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _load_schedule(self) -> dict:
        """Загружает расписание из файла."""
        if self._schedule_path.exists():
            try:
                return json.loads(self._schedule_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "enabled": False,
            "format": "json",
            "interval_hours": 24,
            "output_dir": None,
            "last_export_ts": None,
            "exports": [],
        }

    def _save_schedule(self, schedule: dict) -> None:
        """Сохраняет расписание в файл атомарно."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._schedule_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._schedule_path)

    def _effective_output_dir(self, schedule: dict) -> Path:
        """Возвращает реальную папку для экспортов."""
        if schedule.get("output_dir"):
            return Path(schedule["output_dir"])
        return self.exports_dir

    def _prune_old_exports(self, schedule: dict) -> dict:
        """Удаляет старые файлы экспорта, оставляя не более max_exports.

        Обновляет список exports в расписании и возвращает обновлённый dict.
        """
        exports: list[dict] = schedule.get("exports", [])

        # Удаляем записи с несуществующими файлами
        valid_exports = []
        for entry in exports:
            p = Path(entry.get("path", ""))
            if p.exists():
                valid_exports.append(entry)
            else:
                logger.debug("Удалена запись об удалённом экспорте: %s", entry.get("path"))

        # Отрезаем старые файлы сверх лимита
        to_remove = valid_exports[: max(0, len(valid_exports) - self.max_exports)]
        for entry in to_remove:
            p = Path(entry.get("path", ""))
            try:
                p.unlink(missing_ok=True)
                logger.info("Удалён старый авто-экспорт: %s", p)
            except Exception as exc:
                logger.warning("Не удалось удалить авто-экспорт %s: %s", p, exc)

        schedule["exports"] = valid_exports[max(0, len(valid_exports) - self.max_exports) :]
        return schedule

    def _do_export(self, store: Any, fmt: str, output_dir: Path) -> dict:
        """Выполняет один экспорт и возвращает метаданные файла."""
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        ext_map = {
            "srt": "srt",
            "csv": "csv",
            "markdown": "md",
            "json": "json",
            "obsidian": "md",
            "html": "html",
        }
        ext = ext_map.get(fmt, "txt")
        filename = f"auto_export_{ts}.{ext}"
        file_path = output_dir / filename

        content = self._generate_content(store, fmt)
        file_path.write_text(content, encoding="utf-8")
        size_bytes = file_path.stat().st_size

        logger.info("Авто-экспорт создан: %s (%d байт)", file_path, size_bytes)
        return {
            "path": str(file_path),
            "format": fmt,
            "ts": ts,
            "size_bytes": size_bytes,
            "date": datetime.now(timezone.utc).isoformat(),
        }

    def _generate_content(self, store: Any, fmt: str) -> str:
        """Генерирует содержимое экспорта в указанном формате."""
        # Загружаем активные записи
        try:
            items_dicts, _ = store.get_history_page_filtered(
                cursor=None,
                limit=5000,
                paste_status=None,
                translation_mode=None,
            )
        except Exception:
            items_dicts = []

        if fmt == "json":
            return self._format_json(items_dicts)
        elif fmt == "csv":
            return self._format_csv(items_dicts)
        elif fmt in ("markdown", "obsidian"):
            return self._format_markdown(items_dicts, fmt)
        elif fmt == "srt":
            return self._format_srt(items_dicts)
        elif fmt == "html":
            return self._format_html(items_dicts)
        else:
            return self._format_json(items_dicts)

    def _format_json(self, items_dicts: list[dict]) -> str:
        """Форматирует записи как JSON."""
        export_ts = datetime.now(timezone.utc).isoformat()
        return json.dumps(
            {
                "export_ts": export_ts,
                "total": len(items_dicts),
                "items": items_dicts,
            },
            ensure_ascii=False,
            indent=2,
        )

    def _format_csv(self, items_dicts: list[dict]) -> str:
        """Форматирует записи как CSV."""
        import csv
        import io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["timestamp", "text", "translation", "language", "confidence",
                         "duration_sec", "paste_status"])
        for item in items_dicts:
            translation = ""
            if item.get("translation_status") == "ok":
                translation = item.get("translated_text") or item.get("translation", "")
            writer.writerow([
                item.get("ts", ""),
                item.get("text", ""),
                translation,
                item.get("source_lang") or item.get("lang", ""),
                item.get("confidence", ""),
                item.get("audio_duration_sec") or item.get("duration", ""),
                item.get("paste_status", ""),
            ])
        return output.getvalue()

    def _format_markdown(self, items_dicts: list[dict], fmt: str) -> str:
        """Форматирует записи как Markdown (обычный или Obsidian)."""
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines: list[str] = [
            f"# Krab Ear — Авто-экспорт ({export_ts})",
            f"- Записей: {len(items_dicts)}",
            "",
            "---",
            "",
        ]

        if fmt == "obsidian":
            # Добавляем YAML frontmatter
            frontmatter = [
                "---",
                "tags: [transcription, krab-ear, auto-export]",
                f"export_date: {export_ts}",
                f"total: {len(items_dicts)}",
                "---",
                "",
            ]
            lines = frontmatter + lines

        for idx, item in enumerate(items_dicts, start=1):
            ts = item.get("ts", "")
            text = item.get("text", "")
            lines.append(f"## {idx}. [{ts}]")
            lines.append("")
            lines.append(text)
            if item.get("translated_text") and item.get("translation_status") == "ok":
                lines.append("")
                lines.append(f"**Перевод:** {item['translated_text']}")
            lines.append("")

        return "\n".join(lines)

    def _format_srt(self, items_dicts: list[dict]) -> str:
        """Форматирует записи как SRT-субтитры."""
        lines: list[str] = []
        for idx, item in enumerate(items_dicts, start=1):
            dur = item.get("audio_duration_sec") or item.get("duration") or 5.0
            try:
                dur = float(dur)
            except (TypeError, ValueError):
                dur = 5.0
            start_sec = 0.0
            start_str = self._sec_to_srt(start_sec)
            end_str = self._sec_to_srt(dur)
            text = item.get("text", "")
            lines.append(str(idx))
            lines.append(f"{start_str} --> {end_str}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines)

    def _format_html(self, items_dicts: list[dict]) -> str:
        """Форматирует записи как автономный HTML-отчёт."""
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows: list[str] = []
        for item in items_dicts:
            ts = item.get("ts", "")
            text = item.get("text", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            rows.append(f"<tr><td>{ts}</td><td>{text}</td></tr>")
        rows_html = "\n".join(rows)
        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Krab Ear — Авто-экспорт {export_ts}</title>
<style>body{{font-family:sans-serif;padding:20px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:8px;text-align:left}}</style>
</head>
<body>
<h1>Krab Ear — Авто-экспорт</h1>
<p>Дата: {export_ts} | Записей: {len(items_dicts)}</p>
<table>
<thead><tr><th>Время</th><th>Текст</th></tr></thead>
<tbody>
{rows_html}
</tbody>
</table>
</body>
</html>"""

    @staticmethod
    def _sec_to_srt(seconds: float) -> str:
        """Конвертирует секунды в формат SRT-временной метки."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def configure(
        self,
        fmt: str,
        interval_hours: int = 24,
        output_dir: str | None = None,
        enabled: bool = True,
    ) -> dict:
        """Настраивает расписание экспорта.

        Args:
            fmt: формат экспорта (srt, csv, markdown, json, obsidian, html).
            interval_hours: интервал между экспортами в часах (мин. 1).
            output_dir: папка для файлов экспорта (None = авто).
            enabled: True чтобы включить авто-экспорт.

        Returns:
            Обновлённый статус расписания.
        """
        fmt = str(fmt).lower().strip()
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(f"Неподдерживаемый формат: {fmt!r}. Допустимые: {sorted(SUPPORTED_FORMATS)}")
        interval_hours = max(1, int(interval_hours))

        with self._lock:
            schedule = self._load_schedule()
            schedule["format"] = fmt
            schedule["interval_hours"] = interval_hours
            schedule["output_dir"] = str(output_dir) if output_dir else None
            schedule["enabled"] = bool(enabled)
            self._save_schedule(schedule)

        return self.get_schedule_status()

    def check_and_export(self, store: Any) -> dict | None:
        """Выполняет экспорт, если с последнего прошло >= interval_hours.

        Args:
            store: StateStore для загрузки записей истории.

        Returns:
            dict с метаданными нового экспорта, или None если пропущен.
        """
        with self._lock:
            schedule = self._load_schedule()

            if not schedule.get("enabled", False):
                return None

            interval_hours = int(schedule.get("interval_hours", 24))
            last_ts_str: str | None = schedule.get("last_export_ts")

            if last_ts_str is not None:
                try:
                    last_dt = datetime.fromisoformat(last_ts_str)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    now = datetime.now(timezone.utc)
                    elapsed_hours = (now - last_dt).total_seconds() / 3600
                    if elapsed_hours < interval_hours:
                        return None
                except Exception:
                    pass  # Повреждённые метаданные — делаем экспорт

            fmt = str(schedule.get("format", "json"))
            output_dir = self._effective_output_dir(schedule)

            entry = self._do_export(store, fmt, output_dir)

            schedule["last_export_ts"] = datetime.now(timezone.utc).isoformat()
            exports: list[dict] = schedule.get("exports", [])
            exports.append(entry)
            schedule["exports"] = exports
            schedule = self._prune_old_exports(schedule)
            self._save_schedule(schedule)

            return entry

    def get_schedule_status(self) -> dict:
        """Возвращает текущий статус расписания.

        Returns:
            dict с ключами:
                enabled (bool)
                format (str)
                interval_hours (int)
                output_dir (str | None)
                last_export_ts (str | None)
                next_export_ts (str | None)
                total_exports (int)
        """
        with self._lock:
            schedule = self._load_schedule()

        last_ts_str: str | None = schedule.get("last_export_ts")
        next_ts_str: str | None = None

        if last_ts_str is not None:
            try:
                last_dt = datetime.fromisoformat(last_ts_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                interval_hours = int(schedule.get("interval_hours", 24))
                next_dt = last_dt + timedelta(hours=interval_hours)
                next_ts_str = next_dt.isoformat()
            except Exception:
                pass

        return {
            "enabled": bool(schedule.get("enabled", False)),
            "format": str(schedule.get("format", "json")),
            "interval_hours": int(schedule.get("interval_hours", 24)),
            "output_dir": schedule.get("output_dir"),
            "last_export_ts": last_ts_str,
            "next_export_ts": next_ts_str,
            "total_exports": len(schedule.get("exports", [])),
        }

    def list_exports(self) -> list[dict]:
        """Возвращает список прошлых экспортов.

        Returns:
            Список dict с ключами: path, format, ts, size_bytes, date.
            Записи о несуществующих файлах исключаются.
        """
        with self._lock:
            schedule = self._load_schedule()

        result = []
        for entry in schedule.get("exports", []):
            p = Path(entry.get("path", ""))
            if p.exists():
                result.append(dict(entry))

        # Новейшие — первыми
        result.sort(key=lambda x: x.get("ts", ""), reverse=True)
        return result
