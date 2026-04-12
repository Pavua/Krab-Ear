"""Тесты handle_batch_export — пакетный экспорт в нескольких форматах."""

from __future__ import annotations

import sys
from pathlib import Path
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore
from backend.history_service import HistoryService


class BatchExportEmptyHistoryTestCase(unittest.TestCase):
    """Пустая история — базовые контракты."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_empty_history_returns_structure(self) -> None:
        """Пустая история: возвращает корректную структуру с total_entries=0."""
        result = self.svc.handle_batch_export({"formats": ["csv", "markdown", "srt"]})
        self.assertIn("dir", result)
        self.assertIn("files", result)
        self.assertIn("errors", result)
        self.assertIn("total_entries", result)
        self.assertEqual(result["total_entries"], 0)

    def test_empty_history_bundle_dir_created(self) -> None:
        """Директория бандла создаётся даже при пустой истории."""
        result = self.svc.handle_batch_export({"formats": ["csv"]})
        self.assertTrue(Path(result["dir"]).exists())

    def test_default_formats_all_four(self) -> None:
        """Без явного formats запрашиваются все четыре формата."""
        self.store.add_history_item(text="hello world", paste_status="ok")
        result = self.svc.handle_batch_export({})
        # Obsidian может упасть если нет записей (но здесь есть), остальные три точно должны быть
        all_attempted = set(result["files"].keys()) | set(result["errors"].keys())
        self.assertGreaterEqual(len(all_attempted), 3)


class BatchExportWithDataTestCase(unittest.TestCase):
    """Тесты с реальными записями."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)
        self.store.add_history_item(text="Первая запись", paste_status="ok", source_lang="ru")
        self.store.add_history_item(text="Segunda entrada", paste_status="ok", source_lang="es")

    def test_csv_file_exists_and_has_content(self) -> None:
        """Формат csv: файл создаётся и содержит данные."""
        result = self.svc.handle_batch_export({"formats": ["csv"]})
        self.assertIn("csv", result["files"])
        csv_path = Path(result["files"]["csv"])
        self.assertTrue(csv_path.exists())
        content = csv_path.read_text(encoding="utf-8")
        self.assertIn("Первая запись", content)

    def test_markdown_file_exists_and_has_content(self) -> None:
        """Формат markdown: файл создаётся и содержит заголовок."""
        result = self.svc.handle_batch_export({"formats": ["markdown"]})
        self.assertIn("markdown", result["files"])
        md_path = Path(result["files"]["markdown"])
        self.assertTrue(md_path.exists())
        content = md_path.read_text(encoding="utf-8")
        self.assertIn("# Krab Ear", content)

    def test_srt_file_exists_and_has_content(self) -> None:
        """Формат srt: файл создаётся и содержит текст записей."""
        result = self.svc.handle_batch_export({"formats": ["srt"]})
        self.assertIn("srt", result["files"])
        srt_path = Path(result["files"]["srt"])
        self.assertTrue(srt_path.exists())
        content = srt_path.read_text(encoding="utf-8")
        # SRT должен содержать хотя бы один таймкод
        self.assertIn("-->", content)

    def test_total_entries_matches_store(self) -> None:
        """total_entries соответствует количеству записей в хранилище."""
        result = self.svc.handle_batch_export({"formats": ["csv", "markdown"]})
        self.assertEqual(result["total_entries"], 2)

    def test_multiple_formats_all_in_same_bundle_dir(self) -> None:
        """Все файлы находятся в одной директории бандла."""
        result = self.svc.handle_batch_export({"formats": ["csv", "markdown", "srt"]})
        bundle_dir = Path(result["dir"])
        for fmt, path in result["files"].items():
            self.assertEqual(Path(path).parent, bundle_dir, f"{fmt} находится не в bundle_dir")

    def test_custom_output_dir(self) -> None:
        """Параметр output_dir направляет бандл в нужную директорию."""
        custom_dir = Path(self.tmp.name) / "my_exports"
        result = self.svc.handle_batch_export({
            "formats": ["csv"],
            "output_dir": str(custom_dir),
        })
        bundle_dir = Path(result["dir"])
        # bundle_dir должна находиться внутри custom_dir
        self.assertEqual(bundle_dir.parent, custom_dir.resolve())

    def test_unknown_format_raises(self) -> None:
        """Неизвестный формат вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_batch_export({"formats": ["json", "xml"]})

    def test_empty_formats_list_raises(self) -> None:
        """Пустой список formats вызывает RuntimeError."""
        with self.assertRaises(RuntimeError):
            self.svc.handle_batch_export({"formats": []})

    def test_formats_case_insensitive(self) -> None:
        """Форматы принимаются в верхнем регистре."""
        result = self.svc.handle_batch_export({"formats": ["CSV", "Markdown"]})
        self.assertIn("csv", result["files"])
        self.assertIn("markdown", result["files"])


class BatchExportBuildSrtTestCase(unittest.TestCase):
    """Тесты вспомогательного метода _build_bulk_srt."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_empty_items_dicts_returns_empty_string(self) -> None:
        """Пустой список записей → пустой SRT."""
        result = self.svc._build_bulk_srt([])
        self.assertEqual(result.strip(), "")

    def test_single_item_produces_timestamp_line(self) -> None:
        """Одна запись → SRT с одним сегментом и таймкодом."""
        self.store.add_history_item(text="Test segment", paste_status="ok")
        items, _ = self.store.get_history_page_filtered(
            cursor=None, limit=10, paste_status=None, translation_mode=None
        )
        srt = self.svc._build_bulk_srt(items)
        self.assertIn("-->", srt)
        self.assertIn("Test segment", srt)


class BatchExportBuildMarkdownTestCase(unittest.TestCase):
    """Тесты вспомогательного метода _build_markdown_content."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_empty_items_returns_empty_placeholder(self) -> None:
        """Пустой список → заглушка с заголовком."""
        result = self.svc._build_markdown_content([])
        self.assertIn("# Krab Ear", result)
        self.assertIn("пуста", result)

    def test_items_appear_in_output(self) -> None:
        """Текст записей присутствует в Markdown-выводе."""
        self.store.add_history_item(text="Unique text 42", paste_status="ok")
        items, _ = self.store.get_history_page_filtered(
            cursor=None, limit=10, paste_status=None, translation_mode=None
        )
        md = self.svc._build_markdown_content(items)
        self.assertIn("Unique text 42", md)


if __name__ == "__main__":
    unittest.main()
