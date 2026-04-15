"""Тесты для HTMLReportGenerator и IPC-метода export_html_report.

Покрывает:
1.  generate_report возвращает корректный HTML-документ (<!DOCTYPE html>)
2.  HTML содержит секцию заголовка с переданным title
3.  HTML содержит блок сводной статистики
4.  HTML содержит временную линию (timeline)
5.  HTML содержит записи — корректное количество карточек
6.  HTML содержит placeholder облака слов
7.  HTML содержит footer
8.  Пустой список записей — корректный документ без entry-card
9.  Перевод попадает в HTML (entry-translation)
10. Полоса уверенности попадает в HTML (confidence-bar)
11. Значки спикеров (speaker-badge) отрисовываются при наличии диаризации
12. Запись с тегами содержит их в HTML
13. Избранная запись получает класс «favorite»
14. IPC-метод export_html_report зарегистрирован в service.py
15. handle_export_html_report возвращает ok=True и корректную структуру
16. save_to_file создаёт .html файл на диске
"""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore
from backend.html_report import HTMLReportGenerator

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: Path) -> StateStore:
    return StateStore(data_dir=tmp_dir)


def _make_svc(store: StateStore) -> HistoryService:
    return HistoryService(store=store)


def _simple_item(
    text: str = "Тестовая запись",
    ts: str = "2026-04-12T10:00:00",
    confidence: float | None = None,
    translated_text: str = "",
    tags: list | None = None,
    favorite: bool = False,
    diarization: dict | None = None,
) -> dict:
    return {
        "id": "test-id-001",
        "ts": ts,
        "text": text,
        "paste_status": "ok",
        "source_text": "",
        "translated_text": translated_text,
        "translation_mode": "off" if not translated_text else "ru",
        "source_lang": "ru",
        "target_lang": "",
        "translation_status": "ok" if translated_text else "not_requested",
        "translation_engine": "hf_marian" if translated_text else "",
        "chat_id": "",
        "message_id": "",
        "cleaned_text": "",
        "llm_applied": False,
        "llm_latency_ms": 0,
        "diarization": diarization,
        "audio_duration_sec": None,
        "confidence": confidence,
        "tags": tags or [],
        "favorite": favorite,
    }


# ---------------------------------------------------------------------------
# 1-8: HTMLReportGenerator unit tests
# ---------------------------------------------------------------------------

class TestHTMLReportGeneratorBasic(unittest.TestCase):
    """Базовые тесты структуры HTML-отчёта."""

    def setUp(self) -> None:
        self.gen = HTMLReportGenerator()
        self.items = [_simple_item(text=f"Запись {i}", ts=f"2026-04-12T10:0{i}:00") for i in range(3)]

    # 1. Корректный DOCTYPE
    def test_output_is_valid_html_doctype(self) -> None:
        result = self.gen.generate_report(self.items)
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip().startswith("<!DOCTYPE html>"),
                        "HTML должен начинаться с <!DOCTYPE html>")

    # 2. title попадает в документ
    def test_title_present_in_html(self) -> None:
        result = self.gen.generate_report(self.items, title="My Custom Report")
        self.assertIn("My Custom Report", result)

    # 3. Секция статистики
    def test_stats_section_present(self) -> None:
        result = self.gen.generate_report(self.items)
        self.assertIn("stats-card", result)
        self.assertIn("Сводная статистика", result)

    # 4. Временная линия
    def test_timeline_section_present(self) -> None:
        result = self.gen.generate_report(self.items)
        self.assertIn("timeline", result)
        self.assertIn("Временная линия", result)

    # 5. Корректное количество карточек записей
    def test_entries_count_matches_items(self) -> None:
        result = self.gen.generate_report(self.items)
        # Каждая карточка имеет атрибут class="entry-card"
        # Считаем вхождения внутри HTML-тегов (не в CSS)
        import re
        count = len(re.findall(r'class="entry-card', result))
        self.assertEqual(count, len(self.items),
                         f"Ожидалось {len(self.items)} карточек, найдено {count}")

    # 6. Placeholder облака слов
    def test_wordcloud_section_present(self) -> None:
        result = self.gen.generate_report(self.items)
        self.assertIn("Облако слов", result)
        self.assertIn("wordcloud", result)

    # 7. Footer
    def test_footer_present(self) -> None:
        result = self.gen.generate_report(self.items)
        self.assertIn("report-footer", result)
        self.assertIn("Krab Ear", result)

    # 8. Пустой список — корректный документ
    def test_empty_items_produces_valid_html(self) -> None:
        import re
        result = self.gen.generate_report([])
        self.assertIn("<!DOCTYPE html>", result)
        # При пустом списке — нет HTML-элементов с классом entry-card
        self.assertEqual(len(re.findall(r'class="entry-card', result)), 0)
        self.assertIn("stats-card", result)


class TestHTMLReportGeneratorContent(unittest.TestCase):
    """Тесты содержимого карточек записей."""

    def setUp(self) -> None:
        self.gen = HTMLReportGenerator()

    # 9. Перевод попадает в HTML
    def test_translation_present_in_entry(self) -> None:
        items = [_simple_item(
            text="Hello world",
            translated_text="Привет мир",
        )]
        result = self.gen.generate_report(items)
        self.assertIn("entry-translation", result)
        self.assertIn("Привет мир", result)

    # 10. Полоса уверенности
    def test_confidence_bar_present(self) -> None:
        items = [_simple_item(text="Запись", confidence=0.87)]
        result = self.gen.generate_report(items)
        self.assertIn("confidence-bar", result)
        self.assertIn("87%", result)

    # 11. Значки спикеров (диаризация)
    def test_speaker_badges_from_diarization(self) -> None:
        diar = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Добрый день", "start": 0.0, "end": 2.0},
                {"speaker": "SPEAKER_01", "text": "Здравствуйте", "start": 2.5, "end": 4.0},
            ],
        }
        items = [_simple_item(text="Диалог", diarization=diar)]
        result = self.gen.generate_report(items)
        self.assertIn("speaker-badge", result)
        self.assertIn("SPEAKER_00", result)
        self.assertIn("SPEAKER_01", result)

    # 12. Теги попадают в HTML
    def test_tags_present_in_entry(self) -> None:
        items = [_simple_item(text="С тегами", tags=["важное", "встреча"])]
        result = self.gen.generate_report(items)
        self.assertIn("важное", result)
        self.assertIn("встреча", result)
        self.assertIn("tag", result)

    # 13. Избранная запись — класс «favorite»
    def test_favorite_entry_has_favorite_class(self) -> None:
        items = [_simple_item(text="Избранное", favorite=True)]
        result = self.gen.generate_report(items)
        self.assertIn("entry-card favorite", result)

    # XSS protection — текст эскейпится
    def test_xss_escape_in_entry_text(self) -> None:
        items = [_simple_item(text='<script>alert("xss")</script>')]
        result = self.gen.generate_report(items)
        self.assertNotIn("<script>", result)
        self.assertIn("&lt;script&gt;", result)

    # dark/light theme CSS
    def test_prefers_color_scheme_dark_in_css(self) -> None:
        result = self.gen.generate_report([])
        self.assertIn("prefers-color-scheme: dark", result)


# ---------------------------------------------------------------------------
# Stats computation tests
# ---------------------------------------------------------------------------

class TestHTMLReportStats(unittest.TestCase):
    """Тесты вычисления статистики."""

    def setUp(self) -> None:
        self.gen = HTMLReportGenerator()

    def test_stats_empty_items(self) -> None:
        stats = self.gen._compute_stats([])
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["avg_confidence"])
        self.assertEqual(stats["total_words"], 0)

    def test_stats_counts_translated(self) -> None:
        items = [
            _simple_item(text="A", translated_text="Б"),
            _simple_item(text="B"),
        ]
        stats = self.gen._compute_stats(items)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["translated"], 1)

    def test_stats_avg_confidence(self) -> None:
        items = [
            _simple_item(text="A", confidence=0.8),
            _simple_item(text="B", confidence=0.6),
        ]
        stats = self.gen._compute_stats(items)
        self.assertIsNotNone(stats["avg_confidence"])
        self.assertAlmostEqual(stats["avg_confidence"], 0.7, places=2)

    def test_stats_counts_favorites(self) -> None:
        items = [
            _simple_item(text="A", favorite=True),
            _simple_item(text="B", favorite=False),
            _simple_item(text="C", favorite=True),
        ]
        stats = self.gen._compute_stats(items)
        self.assertEqual(stats["favorites"], 2)

    def test_stats_unique_speakers(self) -> None:
        diar = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "A"},
                {"speaker": "SPEAKER_01", "text": "B"},
                {"speaker": "SPEAKER_00", "text": "C"},
            ],
        }
        items = [_simple_item(text="Диалог", diarization=diar)]
        stats = self.gen._compute_stats(items)
        self.assertEqual(stats["speakers"], 2)

    def test_stats_total_words(self) -> None:
        items = [
            _simple_item(text="раз два три"),
            _simple_item(text="четыре пять"),
        ]
        stats = self.gen._compute_stats(items)
        self.assertEqual(stats["total_words"], 5)


# ---------------------------------------------------------------------------
# IPC integration tests via HistoryService
# ---------------------------------------------------------------------------

class TestExportHtmlReportIPC(unittest.TestCase):
    """Тесты IPC-метода handle_export_html_report."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self._tmp.name))
        self.svc = _make_svc(self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # 14. IPC зарегистрирован в service.py
    def test_ipc_method_registered_in_service(self) -> None:
        service_path = Path(__file__).resolve().parents[1] / "backend" / "service.py"
        content = service_path.read_text(encoding="utf-8")
        self.assertIn('"export_html_report"', content,
                      "IPC-метод export_html_report не зарегистрирован в service.py")

    # 15. Базовый вызов возвращает корректную структуру
    def test_basic_call_returns_valid_structure(self) -> None:
        self.store.add_history_item(text="Тестовая запись")
        result = self.svc.handle_export_html_report({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        self.assertGreater(result["chars"], 0)
        self.assertIsNone(result["path"])
        html_content = result["html"]
        self.assertIn("<!DOCTYPE html>", html_content)

    # 16. save_to_file создаёт .html файл
    def test_save_to_file_creates_html_file(self) -> None:
        self.store.add_history_item(text="Файловый экспорт")
        result = self.svc.handle_export_html_report({"save_to_file": True})
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["path"])
        path = Path(result["path"])
        self.assertTrue(path.exists())
        self.assertEqual(path.suffix, ".html")
        content = path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", content)

    def test_empty_history_returns_ok(self) -> None:
        result = self.svc.handle_export_html_report({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 0)
        self.assertIn("<!DOCTYPE html>", result["html"])

    def test_custom_title_appears_in_html(self) -> None:
        self.store.add_history_item(text="Запись")
        result = self.svc.handle_export_html_report({"title": "Мой отчёт 2026"})
        self.assertIn("Мой отчёт 2026", result["html"])

    def test_limit_param_restricts_entries(self) -> None:
        for i in range(5):
            self.store.add_history_item(text=f"Запись {i}")
        result = self.svc.handle_export_html_report({"limit": 2})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 2)

    def test_html_contains_all_required_sections(self) -> None:
        self.store.add_history_item(text="Раз два три четыре пять слов тут")
        result = self.svc.handle_export_html_report({})
        html_content = result["html"]
        # Все ключевые секции присутствуют
        self.assertIn("stats-card", html_content)
        self.assertIn("timeline", html_content)
        self.assertIn("entries-list", html_content)
        self.assertIn("wordcloud", html_content)
        self.assertIn("report-footer", html_content)

    def test_chars_matches_actual_html_length(self) -> None:
        self.store.add_history_item(text="Проверка длины")
        result = self.svc.handle_export_html_report({})
        self.assertEqual(result["chars"], len(result["html"]))

    def test_multiple_entries_all_appear(self) -> None:
        texts = ["Первая запись", "Вторая запись", "Третья запись"]
        for t in texts:
            self.store.add_history_item(text=t)
        result = self.svc.handle_export_html_report({})
        html_content = result["html"]
        for t in texts:
            self.assertIn(t, html_content)

    def test_translation_in_html_output(self) -> None:
        self.store.add_history_item(
            text="Hello world",
            translated_text="Привет мир",
            translation_mode="ru",
            translation_status="ok",
            translation_engine="hf_marian",
            source_lang="en",
            target_lang="ru",
        )
        result = self.svc.handle_export_html_report({})
        self.assertIn("entry-translation", result["html"])
        self.assertIn("Привет мир", result["html"])


if __name__ == "__main__":
    unittest.main()
