"""Тесты валидации форматов экспорта Krab Ear.

Проверяет, что каждый формат экспорта производит корректный вывод:
  - CSV   : парсится csv.reader, правильное число колонок, кавычки
  - JSON  : валидный JSON, обязательные ключи, ISO 8601 timestamps
  - SRT   : корректный SRT (последовательные номера, timestamps, текст)
  - Markdown: заголовки, корректный синтаксис Markdown
  - HTML  : структура HTML (doctype, html, head, body)
  - Obsidian: YAML frontmatter (---), секция тегов
  - SVG timeline: валидный XML, элемент svg
  - iCal  : начинается с VCALENDAR, содержит VEVENT
  - Prometheus: строки # HELP и # TYPE
"""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.history_service import HistoryService
from backend.timeline_export import TimelineExporter
from backend.rest_server import _build_prometheus_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _make_svc(store: StateStore) -> HistoryService:
    return HistoryService(store=store)


def _make_block(
    start_time: str = "2026-04-10T14:00:00+00:00",
    end_time: str = "2026-04-10T15:00:00+00:00",
    items_count: int = 5,
    total_duration_sec: float = 120.0,
    languages: list[str] | None = None,
    summary_text: str = "test recording",
) -> dict:
    return {
        "start_time": start_time,
        "end_time": end_time,
        "items_count": items_count,
        "total_duration_sec": total_duration_sec,
        "total_words": 50,
        "languages": languages if languages is not None else ["ru"],
        "summary_text": summary_text,
    }


def _write_ndjson_item(store: StateStore, **kwargs) -> None:
    """Записывает произвольный элемент напрямую в NDJSON-файл хранилища."""
    entry = {
        "type": "item",
        "id": kwargs.pop("id", "test-id-001"),
        "ts": kwargs.pop("ts", "2026-04-12T10:00:00+00:00"),
        "text": kwargs.pop("text", "test text"),
        "paste_status": kwargs.pop("paste_status", "ok"),
        "tags": kwargs.pop("tags", []),
        "favorite": kwargs.pop("favorite", False),
    }
    entry.update(kwargs)
    with open(store.history_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ===========================================================================
# 1. CSV export validation
# ===========================================================================

class CsvExportValidationTestCase(unittest.TestCase):
    """CSV-экспорт производит парсируемый, структурированный вывод."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.svc = _make_svc(self.store)

    def _csv_content(self) -> str:
        """Возвращает содержимое последнего сохранённого CSV-файла."""
        transcripts_dir = Path(self.tmp.name) / "transcripts"
        csv_files = sorted(transcripts_dir.glob("*.csv")) if transcripts_dir.exists() else []
        self.assertTrue(csv_files, "CSV файл не создан")
        return csv_files[-1].read_text(encoding="utf-8")

    def test_csv_is_parseable_by_csv_reader(self) -> None:
        """CSV-вывод должен без ошибок парситься csv.reader."""
        self.store.add_history_item(text="hello world", paste_status="ok")
        self.svc.handle_export_history_csv({"save_to_file": True})
        content = self._csv_content()
        rows = list(csv.reader(io.StringIO(content)))
        self.assertGreater(len(rows), 0)

    def test_csv_correct_column_count(self) -> None:
        """Каждая строка CSV должна иметь 8 колонок."""
        self.store.add_history_item(text="column count test", paste_status="ok")
        self.svc.handle_export_history_csv({"save_to_file": True})
        content = self._csv_content()
        rows = list(csv.reader(io.StringIO(content)))
        for row in rows:
            self.assertEqual(
                len(row), 8,
                f"Неожиданное число колонок в строке: {row!r}"
            )

    def test_csv_header_columns_match_expected(self) -> None:
        """Заголовок CSV должен содержать все ожидаемые колонки."""
        self.store.add_history_item(text="header test", paste_status="ok")
        self.svc.handle_export_history_csv({"save_to_file": True})
        content = self._csv_content()
        rows = list(csv.reader(io.StringIO(content)))
        header = rows[0]
        expected_columns = [
            "timestamp", "text", "translation", "language",
            "confidence", "duration_sec", "paste_status", "speakers",
        ]
        self.assertEqual(header, expected_columns)

    def test_csv_text_field_with_comma_is_properly_quoted(self) -> None:
        """Текст, содержащий запятые, должен быть корректно заключён в кавычки."""
        text_with_comma = "hello, world, how are you"
        self.store.add_history_item(text=text_with_comma, paste_status="ok")
        self.svc.handle_export_history_csv({"save_to_file": True})
        content = self._csv_content()
        # csv.reader должен корректно распарсить текст с запятыми как один field
        rows = list(csv.reader(io.StringIO(content)))
        data_rows = rows[1:]  # skip header
        texts = [r[1] for r in data_rows if len(r) > 1]
        self.assertIn(text_with_comma, texts)


# ===========================================================================
# 2. JSON export validation
# ===========================================================================

class JsonExportValidationTestCase(unittest.TestCase):
    """JSON-экспорт производит валидный, структурированный JSON."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.svc = _make_svc(self.store)

    def _export_parsed(self) -> dict:
        result = self.svc.handle_export_history_json({"save_to_file": True, "pretty": True})
        self.assertIsNotNone(result["path"])
        return json.loads(Path(result["path"]).read_text())

    def test_json_export_is_valid_json(self) -> None:
        """Экспортированный файл должен быть валидным JSON."""
        self.store.add_history_item(text="валидный JSON тест")
        result = self.svc.handle_export_history_json({"save_to_file": True})
        self.assertIsNotNone(result["path"])
        content = Path(result["path"]).read_text()
        parsed = json.loads(content)  # не должно бросать исключение
        self.assertIsInstance(parsed, dict)

    def test_json_export_has_required_top_level_keys(self) -> None:
        """Экспорт должен содержать ключи export_info и entries."""
        self.store.add_history_item(text="key test")
        data = self._export_parsed()
        self.assertIn("export_info", data)
        self.assertIn("entries", data)

    def test_json_export_info_has_required_fields(self) -> None:
        """export_info должен содержать version, exported_at, total_entries, filters."""
        self.store.add_history_item(text="info fields test")
        data = self._export_parsed()
        info = data["export_info"]
        for key in ("version", "exported_at", "total_entries", "filters"):
            self.assertIn(key, info, f"Отсутствует ключ export_info.{key}")

    def test_json_exported_at_is_iso8601(self) -> None:
        """Поле exported_at должно быть в формате ISO 8601."""
        from datetime import datetime
        self.store.add_history_item(text="timestamp format test")
        data = self._export_parsed()
        exported_at = data["export_info"]["exported_at"]
        # Должно парситься как datetime
        dt = datetime.fromisoformat(exported_at)
        self.assertIsNotNone(dt)

    def test_json_entry_has_required_fields(self) -> None:
        """Каждая запись в entries должна содержать обязательные ключи."""
        _write_ndjson_item(
            self.store,
            id="val-test-001",
            ts="2026-04-12T10:00:00+00:00",
            text="entry field test",
        )
        data = self._export_parsed()
        entry = next(e for e in data["entries"] if e["id"] == "val-test-001")
        required_keys = {"id", "timestamp", "text", "language", "confidence",
                         "duration_sec", "paste_status", "tags", "favorite"}
        for key in required_keys:
            self.assertIn(key, entry, f"Отсутствует ключ entry.{key}")

    def test_json_entry_timestamp_is_iso8601(self) -> None:
        """Timestamp в записи должен быть в формате ISO 8601."""
        from datetime import datetime
        _write_ndjson_item(
            self.store,
            id="ts-test-001",
            ts="2026-04-12T10:00:00+00:00",
            text="timestamp test",
        )
        data = self._export_parsed()
        entry = next(e for e in data["entries"] if e["id"] == "ts-test-001")
        ts = entry["timestamp"]
        self.assertIsNotNone(ts)
        dt = datetime.fromisoformat(ts)
        self.assertIsNotNone(dt)


# ===========================================================================
# 3. SRT export validation
# ===========================================================================

class SrtExportValidationTestCase(unittest.TestCase):
    """SRT-экспорт производит корректный формат SubRip."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.svc = _make_svc(self.store)

    def _add_item_with_diarization(self) -> str:
        """Добавляет запись с диаризацией и возвращает её ID."""
        diar = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Добрый день коллеги", "start": 0.0, "end": 2.5},
                {"speaker": "SPEAKER_01", "text": "Здравствуйте", "start": 3.0, "end": 5.0},
            ],
        }
        item_id = "srt-test-diar-001"
        _write_ndjson_item(
            self.store,
            id=item_id,
            ts="2026-04-12T10:00:00+00:00",
            text="Добрый день коллеги",
            diarization=diar,
        )
        return item_id

    def test_srt_sequential_numbers(self) -> None:
        """Номера субтитров должны быть последовательными, начиная с 1."""
        item_id = self._add_item_with_diarization()
        result = self.svc.handle_export_history_srt({"id": item_id})
        content = result["content"]
        lines = content.strip().split("\n")
        # Первая непустая строка должна быть "1"
        non_empty = [l for l in lines if l.strip()]
        self.assertEqual(non_empty[0].strip(), "1")

    def test_srt_timestamps_format(self) -> None:
        """SRT timestamps должны соответствовать формату HH:MM:SS,mmm --> HH:MM:SS,mmm."""
        import re
        item_id = self._add_item_with_diarization()
        result = self.svc.handle_export_history_srt({"id": item_id})
        content = result["content"]
        # Ищем строки с временными метками
        ts_pattern = re.compile(
            r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$"
        )
        lines = content.strip().split("\n")
        ts_lines = [l for l in lines if " --> " in l]
        self.assertGreater(len(ts_lines), 0, "Не найдено ни одной строки с timestamp")
        for ts_line in ts_lines:
            self.assertRegex(
                ts_line.strip(), ts_pattern,
                f"Некорректный формат timestamp: {ts_line!r}"
            )

    def test_srt_has_text_content(self) -> None:
        """SRT должен содержать текстовые строки субтитров."""
        item_id = self._add_item_with_diarization()
        result = self.svc.handle_export_history_srt({"id": item_id})
        content = result["content"]
        self.assertIn("SPEAKER_00", content)
        self.assertIn("Добрый день коллеги", content)

    def test_srt_single_item_without_diarization(self) -> None:
        """SRT без диаризации должен содержать одну запись с номером 1."""
        item = self.store.add_history_item(
            text="Простой текст без диаризации",
            paste_status="ok",
        )
        result = self.svc.handle_export_history_srt({"id": item.id})
        content = result["content"]
        self.assertIn("1\n", content)
        self.assertIn("Простой текст без диаризации", content)


# ===========================================================================
# 4. Markdown export validation
# ===========================================================================

class MarkdownExportValidationTestCase(unittest.TestCase):
    """Markdown-экспорт производит корректный Markdown-документ."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.svc = _make_svc(self.store)

    def _export_md_content(self, params: dict | None = None) -> str:
        """Получает содержимое Markdown-экспорта через handle_export_history_markdown."""
        # handle_export_history_markdown не возвращает content напрямую в ответе,
        # используем handle_export_history для получения content
        result = self.svc.handle_export_history({})
        return result.get("content", "")

    def test_markdown_has_h1_header(self) -> None:
        """Markdown должен начинаться с заголовка H1 (# ...)."""
        self.store.add_history_item(text="markdown header test", paste_status="ok")
        content = self._export_md_content()
        self.assertTrue(
            content.startswith("# "),
            f"Документ не начинается с H1. Начало: {content[:80]!r}"
        )

    def test_markdown_has_h2_section_headers(self) -> None:
        """Markdown должен содержать заголовки H2 для каждой записи."""
        self.store.add_history_item(text="section test", paste_status="ok")
        content = self._export_md_content()
        self.assertIn("## ", content)

    def test_markdown_contains_item_text(self) -> None:
        """Текст записи должен присутствовать в Markdown-экспорте."""
        unique_text = "уникальный текст для проверки 99887"
        self.store.add_history_item(text=unique_text, paste_status="ok")
        content = self._export_md_content()
        self.assertIn(unique_text, content)

    def test_markdown_horizontal_rule_separator(self) -> None:
        """Markdown должен использовать --- как разделитель секций."""
        self.store.add_history_item(text="separator test", paste_status="ok")
        content = self._export_md_content()
        self.assertIn("---", content)


# ===========================================================================
# 5. HTML export validation
# ===========================================================================

class HtmlExportValidationTestCase(unittest.TestCase):
    """HTML-экспорт производит корректный HTML-документ."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        from backend.html_report import HTMLReportGenerator
        self.generator = HTMLReportGenerator()

    def _make_item(
        self,
        text: str = "Test entry",
        ts: str = "2026-04-12T10:00:00",
    ) -> dict:
        return {
            "id": "html-test-001",
            "text": text,
            "ts": ts,
            "source_lang": "ru",
            "confidence": 0.9,
            "audio_duration_sec": 5.0,
            "paste_status": "ok",
            "translated_text": "",
            "translation_status": "not_requested",
            "translation_mode": "off",
            "diarization": None,
            "tags": [],
            "favorite": False,
        }

    def test_html_has_doctype(self) -> None:
        """HTML-документ должен начинаться с <!DOCTYPE html>."""
        html = self.generator.generate_report([self._make_item()], title="Test")
        self.assertTrue(
            html.lower().startswith("<!doctype html>"),
            f"Документ не начинается с DOCTYPE. Начало: {html[:80]!r}"
        )

    def test_html_has_html_tag(self) -> None:
        """HTML-документ должен содержать тег <html>."""
        html = self.generator.generate_report([self._make_item()], title="Test")
        self.assertIn("<html", html.lower())

    def test_html_has_head_tag(self) -> None:
        """HTML-документ должен содержать тег <head>."""
        html = self.generator.generate_report([self._make_item()], title="Test")
        self.assertIn("<head>", html.lower())

    def test_html_has_body_tag(self) -> None:
        """HTML-документ должен содержать тег <body>."""
        html = self.generator.generate_report([self._make_item()], title="Test")
        self.assertIn("<body>", html.lower())

    def test_html_contains_item_text(self) -> None:
        """HTML должен содержать текст записи."""
        unique_text = "уникальный html текст 77665"
        html = self.generator.generate_report(
            [self._make_item(text=unique_text)], title="Test"
        )
        self.assertIn(unique_text, html)


# ===========================================================================
# 6. Obsidian export validation
# ===========================================================================

class ObsidianExportValidationTestCase(unittest.TestCase):
    """Obsidian-экспорт производит .md с YAML frontmatter."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = _make_store(Path(self.tmp.name))
        self.svc = _make_svc(self.store)

    def _export_obsidian_content(self) -> str:
        self.store.add_history_item(text="obsidian export test", paste_status="ok")
        result = self.svc.handle_export_obsidian({})
        return result["content"]

    def test_obsidian_has_yaml_frontmatter_delimiters(self) -> None:
        """Obsidian-экспорт должен начинаться и содержать закрывающий разделитель ---."""
        content = self._export_obsidian_content()
        self.assertTrue(
            content.startswith("---"),
            f"Frontmatter не начинается с ---. Начало: {content[:80]!r}"
        )
        # Должно быть как минимум две строки --- (открывающая и закрывающая)
        lines = content.split("\n")
        dashes_count = sum(1 for l in lines if l.strip() == "---")
        self.assertGreaterEqual(dashes_count, 2)

    def test_obsidian_has_tags_in_frontmatter(self) -> None:
        """Frontmatter должен содержать поле tags."""
        content = self._export_obsidian_content()
        self.assertIn("tags:", content)

    def test_obsidian_has_title_in_frontmatter(self) -> None:
        """Frontmatter должен содержать поле title."""
        content = self._export_obsidian_content()
        self.assertIn("title:", content)

    def test_obsidian_has_transcription_tag(self) -> None:
        """Базовый тег transcription должен присутствовать в frontmatter."""
        content = self._export_obsidian_content()
        self.assertIn("transcription", content)

    def test_obsidian_has_krab_ear_tag(self) -> None:
        """Базовый тег krab-ear должен присутствовать в frontmatter."""
        content = self._export_obsidian_content()
        self.assertIn("krab-ear", content)

    def test_obsidian_has_h1_header_after_frontmatter(self) -> None:
        """После frontmatter должен быть заголовок H1."""
        content = self._export_obsidian_content()
        # Ищем # после закрывающего ---
        end_fm = content.find("---\n", 1)  # ищем второй ---
        self.assertGreater(end_fm, 0)
        body = content[end_fm + 4:]
        self.assertIn("# ", body)


# ===========================================================================
# 7. SVG timeline validation
# ===========================================================================

class SvgTimelineValidationTestCase(unittest.TestCase):
    """SVG timeline-экспорт производит валидный XML с элементом svg."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_svg_is_valid_xml(self) -> None:
        """SVG должен быть валидным XML."""
        blocks = [_make_block()]
        svg = self.exporter.export_svg(blocks)
        # Не должно бросать исключение
        root = ET.fromstring(svg)
        self.assertIsNotNone(root)

    def test_svg_has_svg_element(self) -> None:
        """Корневой элемент должен быть <svg>."""
        blocks = [_make_block()]
        svg = self.exporter.export_svg(blocks)
        root = ET.fromstring(svg)
        # Учитываем namespace
        self.assertIn("svg", root.tag)

    def test_svg_has_xml_declaration(self) -> None:
        """SVG должен начинаться с XML-декларации."""
        blocks = [_make_block()]
        svg = self.exporter.export_svg(blocks)
        self.assertTrue(
            svg.startswith("<?xml"),
            f"SVG не начинается с <?xml. Начало: {svg[:60]!r}"
        )

    def test_svg_has_width_and_height(self) -> None:
        """SVG должен содержать атрибуты width и height."""
        blocks = [_make_block()]
        svg = self.exporter.export_svg(blocks, width=800, height=300)
        root = ET.fromstring(svg)
        self.assertIn("width", root.attrib)
        self.assertIn("height", root.attrib)
        self.assertEqual(root.attrib["width"], "800")
        self.assertEqual(root.attrib["height"], "300")

    def test_svg_empty_blocks_returns_valid_svg(self) -> None:
        """Пустой список блоков должен возвращать валидный SVG."""
        svg = self.exporter.export_svg([])
        root = ET.fromstring(svg)
        self.assertIn("svg", root.tag)


# ===========================================================================
# 8. iCal export validation
# ===========================================================================

class ICalExportValidationTestCase(unittest.TestCase):
    """iCal-экспорт производит корректный iCalendar-документ."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def _make_ical_item(
        self,
        start_time: str = "2026-04-10T14:00:00+00:00",
        end_time: str = "2026-04-10T15:00:00+00:00",
        summary_text: str = "test event",
    ) -> dict:
        return {
            "start_time": start_time,
            "end_time": end_time,
            "items_count": 3,
            "total_duration_sec": 60.0,
            "languages": ["ru"],
            "summary_text": summary_text,
        }

    def test_ical_starts_with_vcalendar(self) -> None:
        """iCal должен начинаться с BEGIN:VCALENDAR."""
        ical = self.exporter.export_ical([self._make_ical_item()])
        self.assertTrue(
            ical.startswith("BEGIN:VCALENDAR"),
            f"iCal не начинается с BEGIN:VCALENDAR. Начало: {ical[:80]!r}"
        )

    def test_ical_ends_with_vcalendar(self) -> None:
        """iCal должен заканчиваться на END:VCALENDAR."""
        ical = self.exporter.export_ical([self._make_ical_item()])
        self.assertIn("END:VCALENDAR", ical)

    def test_ical_has_vevent(self) -> None:
        """iCal должен содержать хотя бы один VEVENT."""
        ical = self.exporter.export_ical([self._make_ical_item()])
        self.assertIn("BEGIN:VEVENT", ical)
        self.assertIn("END:VEVENT", ical)

    def test_ical_has_version(self) -> None:
        """iCal должен содержать VERSION:2.0."""
        ical = self.exporter.export_ical([self._make_ical_item()])
        self.assertIn("VERSION:2.0", ical)

    def test_ical_multiple_items_multiple_vevents(self) -> None:
        """Несколько элементов должны дать несколько VEVENT."""
        items = [
            self._make_ical_item("2026-04-10T10:00:00+00:00", "2026-04-10T11:00:00+00:00"),
            self._make_ical_item("2026-04-10T12:00:00+00:00", "2026-04-10T13:00:00+00:00"),
        ]
        ical = self.exporter.export_ical(items)
        vevent_count = ical.count("BEGIN:VEVENT")
        self.assertEqual(vevent_count, 2)

    def test_ical_empty_items_no_vevent(self) -> None:
        """Пустой список элементов → нет VEVENT."""
        ical = self.exporter.export_ical([])
        self.assertNotIn("BEGIN:VEVENT", ical)
        self.assertIn("BEGIN:VCALENDAR", ical)


# ===========================================================================
# 9. Prometheus metrics validation
# ===========================================================================

class PrometheusMetricsValidationTestCase(unittest.TestCase):
    """Prometheus-экспорт производит корректный text exposition format."""

    def _make_summary(self, total: int = 10, error_rate: float = 0.1) -> dict:
        return {
            "total_requests": total,
            "error_rate": error_rate,
            "window_size": total,
            "stt_metrics": {
                "confidence": {"avg": 0.88},
                "latency_ms": {"p50": 200, "p95": 500, "p99": 800, "avg": 250},
            },
        }

    def test_prometheus_has_help_lines(self) -> None:
        """Prometheus-вывод должен содержать строки # HELP."""
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("# HELP", text)

    def test_prometheus_has_type_lines(self) -> None:
        """Prometheus-вывод должен содержать строки # TYPE."""
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("# TYPE", text)

    def test_prometheus_has_counter_type(self) -> None:
        """Prometheus-вывод должен содержать метрики типа counter."""
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("counter", text)

    def test_prometheus_has_gauge_type(self) -> None:
        """Prometheus-вывод должен содержать метрики типа gauge."""
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("gauge", text)

    def test_prometheus_transcriptions_total_metric(self) -> None:
        """krab_ear_transcriptions_total должна присутствовать в выводе."""
        text = _build_prometheus_text(self._make_summary(total=42))
        self.assertIn("krab_ear_transcriptions_total", text)
        self.assertIn("42", text)

    def test_prometheus_each_help_has_corresponding_type(self) -> None:
        """Каждая # HELP строка должна иметь соответствующую # TYPE строку."""
        text = _build_prometheus_text(self._make_summary())
        lines = text.split("\n")
        help_metrics = set()
        type_metrics = set()
        for line in lines:
            if line.startswith("# HELP "):
                metric_name = line.split()[2]
                help_metrics.add(metric_name)
            elif line.startswith("# TYPE "):
                metric_name = line.split()[2]
                type_metrics.add(metric_name)
        # Каждая HELP должна иметь TYPE
        for metric in help_metrics:
            self.assertIn(
                metric, type_metrics,
                f"Метрика {metric!r} имеет HELP но не имеет TYPE"
            )

    def test_prometheus_ends_with_newline(self) -> None:
        """Prometheus-вывод должен заканчиваться символом новой строки."""
        text = _build_prometheus_text(self._make_summary())
        self.assertTrue(text.endswith("\n"))

    def test_prometheus_histogram_has_bucket_lines(self) -> None:
        """Histogram-метрика должна содержать строки _bucket."""
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("_bucket{", text)


if __name__ == "__main__":
    unittest.main()
