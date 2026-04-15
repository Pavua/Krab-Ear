"""Тесты handle_export_history_csv — экспорт транскрипций в CSV."""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CsvExportBasicTestCase(unittest.TestCase):
    """Базовый экспорт CSV."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def _parse_csv(self, result: dict, delimiter: str = ",") -> list[list[str]]:
        """Вспомогательный метод для парсинга CSV из HistoryService."""
        # Получаем содержимое через save_to_file
        transcripts_dir = Path(self.tmp.name) / "data" / "transcripts"
        csv_files = sorted(transcripts_dir.glob("*.csv")) if transcripts_dir.exists() else []
        if not csv_files:
            return []
        content = csv_files[-1].read_text(encoding="utf-8")
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)
        return list(reader)

    def test_empty_history_returns_ok(self) -> None:
        """Пустая история возвращает ok=True и entries=0."""
        result = self.svc.handle_export_history_csv({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 0)
        self.assertIsNone(result["file"])

    def test_basic_export_entries_count(self) -> None:
        """Количество entries совпадает с числом добавленных записей."""
        self.store.add_history_item(text="first entry", paste_status="ok")
        self.store.add_history_item(text="second entry", paste_status="ok")
        result = self.svc.handle_export_history_csv({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 2)

    def test_header_included_by_default(self) -> None:
        """По умолчанию заголовок включён в CSV-файл."""
        self.store.add_history_item(text="test text", paste_status="ok", source_lang="ru")
        result = self.svc.handle_export_history_csv({"save_to_file": True})
        self.assertIsNotNone(result["file"])

        rows = self._parse_csv(result)
        self.assertGreater(len(rows), 1)
        # Первая строка — заголовок
        header = rows[0]
        self.assertIn("timestamp", header)
        self.assertIn("text", header)
        self.assertIn("translation", header)
        self.assertIn("language", header)
        self.assertIn("confidence", header)
        self.assertIn("duration_sec", header)
        self.assertIn("paste_status", header)
        self.assertIn("speakers", header)

    def test_header_excluded_when_false(self) -> None:
        """При include_header=False заголовок отсутствует."""
        self.store.add_history_item(text="hello world", paste_status="ok")
        result = self.svc.handle_export_history_csv({"include_header": False, "save_to_file": True})
        rows = self._parse_csv(result)
        self.assertGreater(len(rows), 0)
        # Первая строка не должна содержать "timestamp"
        self.assertNotIn("timestamp", rows[0])

    def test_text_content_in_rows(self) -> None:
        """Текст записи присутствует в CSV-строке."""
        self.store.add_history_item(text="unique transcript text 12345", paste_status="ok")
        result = self.svc.handle_export_history_csv({"save_to_file": True})
        rows = self._parse_csv(result)
        # Ищем текст в строках данных (пропускаем заголовок)
        data_rows = rows[1:]
        texts = [r[1] for r in data_rows if len(r) > 1]
        self.assertIn("unique transcript text 12345", texts)

    def test_custom_delimiter_tab(self) -> None:
        """Пользовательский разделитель (TAB) применяется корректно."""
        self.store.add_history_item(text="tab delimited", paste_status="ok")
        result = self.svc.handle_export_history_csv({"delimiter": "\t", "save_to_file": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)

        transcripts_dir = Path(self.tmp.name) / "data" / "transcripts"
        csv_files = sorted(transcripts_dir.glob("*.csv")) if transcripts_dir.exists() else []
        self.assertTrue(csv_files)
        content = csv_files[-1].read_text(encoding="utf-8")
        # TAB должен быть разделителем
        self.assertIn("\t", content)

    def test_save_to_file_creates_file(self) -> None:
        """При save_to_file=True создаётся файл и возвращается путь."""
        self.store.add_history_item(text="save test", paste_status="ok")
        result = self.svc.handle_export_history_csv({"save_to_file": True})
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["file"])
        self.assertTrue(Path(result["file"]).exists())
        self.assertTrue(result["file"].endswith(".csv"))

    def test_limit_restricts_entries(self) -> None:
        """Параметр limit ограничивает количество экспортируемых записей."""
        for i in range(8):
            self.store.add_history_item(text=f"item {i}", paste_status="ok")
        result = self.svc.handle_export_history_csv({"limit": 3})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 3)

    def test_translation_column_populated(self) -> None:
        """Колонка translation заполняется при наличии перевода."""
        _item = self.store.add_history_item(  # noqa: F841
            text="оригинальный текст",
            paste_status="ok",
            translated_text="translated text",
            translation_status="ok",
            translation_mode="ru_es",
        )
        result = self.svc.handle_export_history_csv({"save_to_file": True})
        rows = self._parse_csv(result)
        data_rows = rows[1:]
        translations = [r[2] for r in data_rows if len(r) > 2]
        self.assertIn("translated text", translations)

    def test_translation_column_empty_when_not_ok(self) -> None:
        """Колонка translation пуста, если translation_status != ok."""
        self.store.add_history_item(
            text="no translation",
            paste_status="ok",
            translated_text="should not appear",
            translation_status="failed",
        )
        result = self.svc.handle_export_history_csv({"save_to_file": True})
        rows = self._parse_csv(result)
        data_rows = rows[1:]
        translations = [r[2] for r in data_rows if len(r) > 2]
        self.assertNotIn("should not appear", translations)

    def test_invalid_delimiter_falls_back_to_comma(self) -> None:
        """Многосимвольный разделитель заменяется запятой."""
        self.store.add_history_item(text="fallback delimiter", paste_status="ok")
        result = self.svc.handle_export_history_csv({"delimiter": ";;", "save_to_file": True})
        self.assertTrue(result["ok"])
        # Файл должен существовать и использовать запятую
        content = Path(result["file"]).read_text(encoding="utf-8")
        self.assertIn(",", content)


if __name__ == "__main__":
    unittest.main()
