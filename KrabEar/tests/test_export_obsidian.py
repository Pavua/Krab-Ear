"""Тесты handle_export_obsidian в HistoryService.

Проверяют структуру Obsidian-совместимого .md файла: YAML frontmatter,
секция Summary, секция транскрибации с тегами спикеров, переводы,
параметры title/output_dir/tags, и обработку ошибок.
"""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import tempfile
import unittest
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ExportObsidianTestCase(unittest.TestCase):
    """Тесты экспорта в Obsidian-формат через handle_export_obsidian."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_dir = Path(self.tmp.name) / "data"
        self.store = StateStore(data_dir)
        self.svc = HistoryService(store=self.store)

    # ------------------------------------------------------------------
    # 1. Базовый случай: одна запись, возвращает путь + entries
    # ------------------------------------------------------------------

    def test_basic_export_returns_file_and_entries(self) -> None:
        """Экспорт одной записи создаёт файл и возвращает корректные поля."""
        item = self.store.add_history_item(text="Привет, как дела?", paste_status="ok")
        result = self.svc.handle_export_obsidian({"ids": [item.id]})

        self.assertIn("file", result)
        self.assertIn("entries", result)
        self.assertIn("content", result)
        self.assertEqual(result["entries"], 1)
        self.assertTrue(Path(result["file"]).exists())

    # ------------------------------------------------------------------
    # 2. YAML frontmatter присутствует
    # ------------------------------------------------------------------

    def test_yaml_frontmatter_present(self) -> None:
        """Файл содержит корректный YAML frontmatter (--- ... ---)."""
        item = self.store.add_history_item(text="Тестовый текст.", paste_status="ok")
        result = self.svc.handle_export_obsidian({"ids": [item.id]})
        content = result["content"]

        self.assertTrue(content.startswith("---"), "frontmatter должен начинаться с ---")
        # Закрывающий --- после открывающего
        lines = content.splitlines()
        closing = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
        self.assertGreaterEqual(len(closing), 2, "frontmatter должен иметь открывающий и закрывающий ---")

        # Обязательные поля
        self.assertIn("title:", content)
        self.assertIn("date:", content)
        self.assertIn("tags:", content)
        self.assertIn("entries:", content)

    # ------------------------------------------------------------------
    # 3. Секция Summary присутствует
    # ------------------------------------------------------------------

    def test_summary_section_present(self) -> None:
        """Файл содержит секцию '## Краткое содержание (Summary)'."""
        item = self.store.add_history_item(text="Обсуждали планы на неделю.", paste_status="ok")
        result = self.svc.handle_export_obsidian({"ids": [item.id]})
        self.assertIn("## Краткое содержание (Summary)", result["content"])

    # ------------------------------------------------------------------
    # 4. Секция транскрибации с тегом спикера
    # ------------------------------------------------------------------

    def test_transcript_section_with_speaker_tag(self) -> None:
        """Секция '## Улучшенная транскрибация' содержит тег [Спикер (timestamp)]."""
        item = self.store.add_history_item(text="Раз два три.", paste_status="ok")
        result = self.svc.handle_export_obsidian({"ids": [item.id]})
        content = result["content"]

        self.assertIn("## Улучшенная транскрибация", content)
        self.assertIn("[Спикер (", content)

    # ------------------------------------------------------------------
    # 5. Диаризация: реплики с отметками времени и спикерами
    # ------------------------------------------------------------------

    def test_diarization_speaker_turns_formatted(self) -> None:
        """При наличии диаризации реплики форматируются как [SPEAKER_XX (HH:MM:SS)]."""
        diarization = {
            "enabled": True,
            "speaker_turns": [
                {"speaker": "SPEAKER_00", "text": "Привет!", "start": 0.0, "end": 1.5},
                {"speaker": "SPEAKER_01", "text": "Здравствуйте.", "start": 1.5, "end": 3.0},
            ],
        }
        item = self.store.add_history_item(
            text="Привет! Здравствуйте.",
            paste_status="ok",
        )
        # Обновляем запись с диаризацией через store напрямую
        item_dict = item.to_dict()
        item_dict["diarization"] = diarization
        from backend.models import HistoryItem as _HI
        updated = _HI.from_dict(item_dict)

        # Экспортируем через метод напрямую с готовым item
        content = self.svc._build_obsidian_content_for_items([updated], "Тест", [])
        self.assertIn("SPEAKER_00", content)
        self.assertIn("SPEAKER_01", content)
        self.assertIn("Привет!", content)
        self.assertIn("Здравствуйте.", content)

    # ------------------------------------------------------------------
    # 6. Параметр title меняет заголовок
    # ------------------------------------------------------------------

    def test_custom_title_in_content(self) -> None:
        """Параметр title используется как заголовок документа."""
        item = self.store.add_history_item(text="Звонок с Дашей.", paste_status="ok")
        result = self.svc.handle_export_obsidian({
            "ids": [item.id],
            "title": "Транскрибация звонка с Дашулькой",
        })
        self.assertIn("Транскрибация звонка с Дашулькой", result["content"])

    # ------------------------------------------------------------------
    # 7. Параметр output_dir: файл создаётся в указанной директории
    # ------------------------------------------------------------------

    def test_output_dir_respected(self) -> None:
        """Файл создаётся в указанной директории output_dir."""
        custom_dir = Path(self.tmp.name) / "obsidian_vault"
        item = self.store.add_history_item(text="Тест директории.", paste_status="ok")
        result = self.svc.handle_export_obsidian({
            "ids": [item.id],
            "output_dir": str(custom_dir),
        })
        file_path = Path(result["file"])
        self.assertTrue(str(file_path).startswith(str(custom_dir.resolve())))
        self.assertTrue(file_path.exists())

    # ------------------------------------------------------------------
    # 8. Параметр tags добавляет теги в frontmatter
    # ------------------------------------------------------------------

    def test_extra_tags_in_frontmatter(self) -> None:
        """Дополнительные теги из параметра tags попадают в frontmatter."""
        item = self.store.add_history_item(text="Важный звонок.", paste_status="ok")
        result = self.svc.handle_export_obsidian({
            "ids": [item.id],
            "tags": ["#call", "важный"],
        })
        self.assertIn("call", result["content"])
        self.assertIn("важный", result["content"])

    # ------------------------------------------------------------------
    # 9. Перевод включается в экспорт
    # ------------------------------------------------------------------

    def test_translation_included_when_present(self) -> None:
        """Если у записи есть перевод, он включается в экспорт."""
        item = self.store.add_history_item(
            text="Добрый день.",
            paste_status="ok",
            translated_text="Buenos días.",
            translation_status="ok",
            translation_mode="ru_es",
        )
        result = self.svc.handle_export_obsidian({"ids": [item.id]})
        self.assertIn("Buenos días.", result["content"])
        self.assertIn("Перевод", result["content"])

    # ------------------------------------------------------------------
    # 10. Ошибка при пустом ids
    # ------------------------------------------------------------------

    def test_empty_ids_raises(self) -> None:
        """Пустой список ids приводит к RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_obsidian({"ids": []})

    # ------------------------------------------------------------------
    # 11. Ошибка при несуществующих ids
    # ------------------------------------------------------------------

    def test_nonexistent_ids_raises(self) -> None:
        """Несуществующие ids приводят к RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_export_obsidian({"ids": ["nonexistent-id-xyz"]})

    # ------------------------------------------------------------------
    # 12. Экспорт нескольких записей
    # ------------------------------------------------------------------

    def test_multiple_items_export(self) -> None:
        """Несколько записей экспортируются в один файл."""
        items = [
            self.store.add_history_item(text=f"Запись {i}.", paste_status="ok")
            for i in range(3)
        ]
        ids = [it.id for it in items]
        result = self.svc.handle_export_obsidian({"ids": ids})
        self.assertEqual(result["entries"], 3)
        content = result["content"]
        for i in range(3):
            self.assertIn(f"Запись {i}.", content)


class ObsidianContentBuilderTestCase(unittest.TestCase):
    """Тесты внутреннего метода _build_obsidian_content_for_items без IO."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def _make_item(self, text: str, **kwargs):
        item = self.store.add_history_item(text=text, paste_status="ok", **kwargs)
        return item

    def test_content_starts_with_frontmatter(self) -> None:
        item = self._make_item("Текст.")
        content = self.svc._build_obsidian_content_for_items([item], "Тест", [])
        self.assertTrue(content.startswith("---"))

    def test_content_has_title_heading(self) -> None:
        item = self._make_item("Текст.")
        content = self.svc._build_obsidian_content_for_items([item], "Мой заголовок", [])
        self.assertIn("# Мой заголовок", content)

    def test_content_has_transcript_section(self) -> None:
        item = self._make_item("Текст для проверки.")
        content = self.svc._build_obsidian_content_for_items([item], "Тест", [])
        self.assertIn("Текст для проверки.", content)


if __name__ == "__main__":
    unittest.main()
