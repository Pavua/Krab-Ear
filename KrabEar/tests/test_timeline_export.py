"""Unit-тесты для TimelineExporter.

Покрывает export_svg, export_json, export_ical и граничные случаи.
"""

from __future__ import annotations
from backend.timeline_export import TimelineExporter

import json
import sys
import unittest
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup — стандартный паттерн для тестов Krab Ear
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_block(
    start_time: str = "2026-04-10T14:00:00+00:00",
    end_time: str = "2026-04-10T15:00:00+00:00",
    items_count: int = 5,
    total_duration_sec: float = 120.0,
    total_words: int = 80,
    languages: list[str] | None = None,
    summary_text: str = "тест запись аудио",
) -> dict[str, Any]:
    """Создаёт минимальный dict, соответствующий TimelineBlock.to_dict()."""
    return {
        "start_time": start_time,
        "end_time": end_time,
        "items_count": items_count,
        "total_duration_sec": total_duration_sec,
        "total_words": total_words,
        "languages": languages if languages is not None else ["ru"],
        "summary_text": summary_text,
    }


# ---------------------------------------------------------------------------
# Тесты export_json
# ---------------------------------------------------------------------------

class ExportJsonTestCase(unittest.TestCase):
    """Тесты export_json: структура, мета-поля, сериализация."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_empty_blocks_valid_json(self) -> None:
        """Пустой список → валидный JSON со структурой по умолчанию."""
        result = self.exporter.export_json([])
        parsed = json.loads(result)
        self.assertIn("blocks", parsed)
        self.assertEqual(parsed["blocks"], [])

    def test_meta_fields_present(self) -> None:
        """JSON содержит мета-поля: schema_version, exported_at, total_blocks и др."""
        blocks = [_make_block()]
        result = self.exporter.export_json(blocks)
        parsed = json.loads(result)
        for key in ("schema_version", "exported_at", "total_blocks",
                    "total_recordings", "total_duration_sec"):
            self.assertIn(key, parsed, f"Поле {key!r} отсутствует в JSON")

    def test_total_blocks_matches(self) -> None:
        """total_blocks в мета-данных соответствует длине списка блоков."""
        blocks = [_make_block(), _make_block(start_time="2026-04-11T10:00:00+00:00")]
        result = self.exporter.export_json(blocks)
        parsed = json.loads(result)
        self.assertEqual(parsed["total_blocks"], 2)

    def test_total_recordings_aggregated(self) -> None:
        """total_recordings — сумма items_count всех блоков."""
        blocks = [
            _make_block(items_count=3),
            _make_block(items_count=7, start_time="2026-04-11T00:00:00+00:00"),
        ]
        result = self.exporter.export_json(blocks)
        parsed = json.loads(result)
        self.assertEqual(parsed["total_recordings"], 10)

    def test_total_duration_aggregated(self) -> None:
        """total_duration_sec — сумма total_duration_sec всех блоков."""
        blocks = [
            _make_block(total_duration_sec=60.0),
            _make_block(total_duration_sec=40.5, start_time="2026-04-11T00:00:00+00:00"),
        ]
        result = self.exporter.export_json(blocks)
        parsed = json.loads(result)
        self.assertAlmostEqual(parsed["total_duration_sec"], 100.5, places=1)

    def test_blocks_preserved_in_output(self) -> None:
        """Исходные блоки сохраняются без изменений в поле 'blocks'."""
        block = _make_block(items_count=42, summary_text="hello world")
        result = self.exporter.export_json([block])
        parsed = json.loads(result)
        self.assertEqual(len(parsed["blocks"]), 1)
        self.assertEqual(parsed["blocks"][0]["items_count"], 42)
        self.assertEqual(parsed["blocks"][0]["summary_text"], "hello world")

    def test_json_is_valid_utf8(self) -> None:
        """JSON содержит кириллицу без экранирования (ensure_ascii=False)."""
        blocks = [_make_block(summary_text="тест кириллица")]
        result = self.exporter.export_json(blocks)
        self.assertIn("тест кириллица", result)


# ---------------------------------------------------------------------------
# Тесты export_svg
# ---------------------------------------------------------------------------

class ExportSvgTestCase(unittest.TestCase):
    """Тесты export_svg: структура SVG, размеры, пустой список."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_empty_blocks_returns_svg(self) -> None:
        """Пустой список → SVG с заглушкой 'No data'."""
        result = self.exporter.export_svg([])
        self.assertIn("<svg", result)
        self.assertIn("No data", result)

    def test_svg_contains_xml_declaration(self) -> None:
        """SVG начинается с XML-декларации."""
        result = self.exporter.export_svg([_make_block()])
        self.assertTrue(result.startswith("<?xml"))

    def test_svg_width_height_in_output(self) -> None:
        """SVG содержит заданные width и height."""
        result = self.exporter.export_svg([_make_block()], width=800, height=300)
        self.assertIn('width="800"', result)
        self.assertIn('height="300"', result)

    def test_svg_has_rect_elements(self) -> None:
        """SVG содержит <rect> элементы для каждого блока."""
        blocks = [_make_block(), _make_block(start_time="2026-04-11T10:00:00+00:00")]
        result = self.exporter.export_svg(blocks)
        rect_count = result.count("<rect ")
        self.assertGreaterEqual(rect_count, 2)

    def test_svg_contains_title_element(self) -> None:
        """SVG содержит заголовок 'Recording Timeline'."""
        result = self.exporter.export_svg([_make_block()])
        self.assertIn("Recording Timeline", result)

    def test_svg_default_dimensions(self) -> None:
        """SVG по умолчанию 1200×400."""
        result = self.exporter.export_svg([_make_block()])
        self.assertIn('width="1200"', result)
        self.assertIn('height="400"', result)

    def test_svg_special_chars_escaped(self) -> None:
        """Спецсимволы XML в summary_text экранируются в tooltip."""
        block = _make_block(summary_text="test <script> & 'hack'")
        result = self.exporter.export_svg([block])
        self.assertNotIn("<script>", result.split("<title>", 1)[-1].split("</title>")[0])

    def test_svg_min_dimensions_clamped(self) -> None:
        """Минимальные размеры SVG: ширина >= 200, высота >= 100."""
        result = self.exporter.export_svg([_make_block()], width=1, height=1)
        self.assertIn('width="200"', result)
        self.assertIn('height="100"', result)


# ---------------------------------------------------------------------------
# Тесты export_ical
# ---------------------------------------------------------------------------

class ExportIcalTestCase(unittest.TestCase):
    """Тесты export_ical: заголовок, VEVENT, обязательные поля."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_empty_items_valid_ical(self) -> None:
        """Пустой список → валидный iCal с BEGIN/END VCALENDAR."""
        result = self.exporter.export_ical([])
        self.assertIn("BEGIN:VCALENDAR", result)
        self.assertIn("END:VCALENDAR", result)

    def test_ical_version_prodid(self) -> None:
        """iCal содержит VERSION:2.0 и PRODID."""
        result = self.exporter.export_ical([])
        self.assertIn("VERSION:2.0", result)
        self.assertIn("PRODID:", result)

    def test_vevent_created_for_block(self) -> None:
        """Для каждого блока создаётся VEVENT."""
        result = self.exporter.export_ical([_make_block()])
        self.assertIn("BEGIN:VEVENT", result)
        self.assertIn("END:VEVENT", result)

    def test_vevent_has_required_fields(self) -> None:
        """VEVENT содержит UID, DTSTAMP, DTSTART, DTEND, SUMMARY."""
        result = self.exporter.export_ical([_make_block()])
        for field in ("UID:", "DTSTAMP:", "DTSTART:", "DTEND:", "SUMMARY:"):
            self.assertIn(field, result, f"Поле {field!r} отсутствует в VEVENT")

    def test_dtstart_format_utc(self) -> None:
        """DTSTART имеет формат UTC (заканчивается на Z)."""
        result = self.exporter.export_ical([_make_block(start_time="2026-04-10T14:00:00+00:00")])
        # Ищем DTSTART строку
        for line in result.splitlines():
            if line.startswith("DTSTART:"):
                self.assertTrue(
                    line.endswith("Z"),
                    f"DTSTART должен заканчиваться на Z: {line}",
                )
                break
        else:
            self.fail("DTSTART не найден в iCal")

    def test_ical_uses_crlf_line_endings(self) -> None:
        """iCal использует CRLF-окончания строк (RFC 5545)."""
        result = self.exporter.export_ical([_make_block()])
        self.assertIn("\r\n", result)

    def test_multiple_blocks_multiple_vevents(self) -> None:
        """Несколько блоков → несколько VEVENT."""
        blocks = [
            _make_block(start_time="2026-04-10T10:00:00+00:00"),
            _make_block(start_time="2026-04-11T10:00:00+00:00"),
            _make_block(start_time="2026-04-12T10:00:00+00:00"),
        ]
        result = self.exporter.export_ical(blocks)
        vevent_count = result.count("BEGIN:VEVENT")
        self.assertEqual(vevent_count, 3)

    def test_item_without_start_time_skipped(self) -> None:
        """Элемент без start_time / ts пропускается (нет VEVENT)."""
        item = {"summary_text": "no date", "items_count": 1}
        result = self.exporter.export_ical([item])
        self.assertNotIn("BEGIN:VEVENT", result)

    def test_ical_summary_uses_summary_text(self) -> None:
        """SUMMARY VEVENT берётся из summary_text блока."""
        block = _make_block(summary_text="важная встреча")
        result = self.exporter.export_ical([block])
        self.assertIn("важная встреча", result)

    def test_ical_description_includes_languages(self) -> None:
        """DESCRIPTION содержит список языков блока."""
        block = _make_block(languages=["ru", "es"])
        result = self.exporter.export_ical([block])
        self.assertIn("ru", result)
        self.assertIn("es", result)


# ---------------------------------------------------------------------------
# Тесты xml/ical escaping
# ---------------------------------------------------------------------------

class EscapingTestCase(unittest.TestCase):
    """Тесты вспомогательных методов экранирования."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_xml_escape_ampersand(self) -> None:
        self.assertEqual(self.exporter._xml_escape("a & b"), "a &amp; b")

    def test_xml_escape_lt_gt(self) -> None:
        self.assertEqual(self.exporter._xml_escape("<tag>"), "&lt;tag&gt;")

    def test_xml_escape_quotes(self) -> None:
        result = self.exporter._xml_escape('"hello"')
        self.assertNotIn('"hello"', result)

    def test_ical_escape_semicolon(self) -> None:
        self.assertIn("\\;", self.exporter._ical_escape("a;b"))

    def test_ical_escape_comma(self) -> None:
        self.assertIn("\\,", self.exporter._ical_escape("a,b"))

    def test_ical_escape_newline(self) -> None:
        self.assertIn("\\n", self.exporter._ical_escape("a\nb"))


# ---------------------------------------------------------------------------
# Тесты concurrent export + unicode
# ---------------------------------------------------------------------------

class ConcurrentAndUnicodeTestCase(unittest.TestCase):
    """Тесты параллельного экспорта и юникодных заголовков."""

    def setUp(self) -> None:
        self.exporter = TimelineExporter()

    def test_unicode_event_titles(self) -> None:
        """Unicode-символы в summary_text сохраняются во всех форматах."""
        block = _make_block(summary_text="会議メモ — 日本語テスト 🎙️")
        # JSON
        result_json = self.exporter.export_json([block])
        self.assertIn("会議メモ", result_json)
        # iCal
        result_ical = self.exporter.export_ical([block])
        self.assertIn("会議メモ", result_ical)
        # SVG (tooltip)
        result_svg = self.exporter.export_svg([block])
        self.assertIn("<svg", result_svg)  # должен быть валидным SVG

    def test_unicode_in_ical_escape(self) -> None:
        """_ical_escape корректно обрабатывает строки с кириллицей и CJK."""
        text = "Привет, мир; foo\nbar"
        escaped = self.exporter._ical_escape(text)
        self.assertIn("\\,", escaped)
        self.assertIn("\\;", escaped)
        self.assertIn("\\n", escaped)
        # Кириллица сохраняется
        self.assertIn("Привет", escaped)

    def test_concurrent_export(self) -> None:
        """Параллельный вызов export_json/export_svg/export_ical безопасен."""
        import threading

        blocks = [_make_block(start_time=f"2026-04-{10 + i}T10:00:00+00:00") for i in range(5)]
        errors: list[Exception] = []
        results: list[str] = []
        lock = threading.Lock()

        def _run_json() -> None:
            try:
                r = self.exporter.export_json(blocks)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def _run_svg() -> None:
            try:
                r = self.exporter.export_svg(blocks)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def _run_ical() -> None:
            try:
                r = self.exporter.export_ical(blocks)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_run_json),
            threading.Thread(target=_run_svg),
            threading.Thread(target=_run_ical),
            threading.Thread(target=_run_json),
            threading.Thread(target=_run_ical),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 5)


if __name__ == "__main__":
    unittest.main()
