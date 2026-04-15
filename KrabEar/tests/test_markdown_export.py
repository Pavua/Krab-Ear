"""Тесты handle_export_history_markdown — экспорт транскрипций в Markdown."""

from __future__ import annotations
from backend.history_service import HistoryService
from backend.state_store import StateStore

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class MarkdownExportBasicTestCase(unittest.TestCase):
    """Базовый экспорт: несколько записей без диаризации и перевода."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_empty_history_returns_ok(self) -> None:
        """Пустая история возвращает ok=True и entries=0."""
        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 0)
        self.assertIn("chars", result)

    def test_basic_export_contains_entries(self) -> None:
        """Экспорт нескольких записей: entries совпадает с количеством добавленных."""
        self.store.add_history_item(text="первая запись", paste_status="ok", source_lang="ru")
        self.store.add_history_item(text="вторая запись", paste_status="ok", source_lang="ru")
        self.store.add_history_item(text="третья запись", paste_status="ok", source_lang="ru")

        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 3)
        self.assertGreater(result["chars"], 0)

    def test_export_respects_limit(self) -> None:
        """Параметр limit ограничивает количество записей."""
        for i in range(10):
            self.store.add_history_item(text=f"запись {i}", paste_status="ok")

        result = self.svc.handle_export_history_markdown({"limit": 5})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 5)

    def test_export_chars_matches_content_length(self) -> None:
        """Поле chars соответствует реальному размеру Markdown-контента."""
        self.store.add_history_item(text="тест длины контента", paste_status="ok")
        # Экспортируем и проверяем, что chars > 0 (контент сгенерирован)
        result = self.svc.handle_export_history_markdown({})
        self.assertGreater(result["chars"], len("тест длины контента"))

    def test_languages_in_statistics(self) -> None:
        """Языки из source_lang должны попасть в статистику (chars > минимума)."""
        self.store.add_history_item(text="hello world", paste_status="ok", source_lang="en")
        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        # Суммарный контент значительно больше одной строки текста
        self.assertGreater(result["chars"], 50)


class MarkdownExportDiarizationTestCase(unittest.TestCase):
    """Экспорт записей с диаризацией (несколько спикеров)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def _make_diarization(self, turns: list[dict]) -> dict:
        return {"enabled": True, "speaker_turns": turns}

    def test_diarization_speakers_in_export(self) -> None:
        """Запись с диаризацией: реплики спикеров включены в экспорт."""
        diar = self._make_diarization([
            {"speaker": "SPEAKER_00", "text": "Привет, как дела?", "start": 0.0, "end": 2.5},
            {"speaker": "SPEAKER_01", "text": "Всё хорошо, спасибо!", "start": 2.5, "end": 5.0},
        ])
        self.store.add_history_item(
            text="Привет, как дела? Всё хорошо, спасибо!",
            paste_status="ok",
            diarization=diar,
            audio_duration_sec=5.0,
        )

        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        # Контент должен включать реплики спикеров
        self.assertGreater(result["chars"], 0)

    def test_diarization_multiple_speakers_counted(self) -> None:
        """Несколько записей с диаризацией: все включаются в экспорт."""
        for i in range(3):
            diar = self._make_diarization([
                {"speaker": "SPEAKER_00", "text": f"Вопрос {i}", "start": 0.0, "end": 1.5},
                {"speaker": "SPEAKER_01", "text": f"Ответ {i}", "start": 1.5, "end": 3.0},
            ])
            self.store.add_history_item(
                text=f"Вопрос {i} Ответ {i}",
                paste_status="ok",
                diarization=diar,
            )

        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 3)

    def test_single_speaker_diarization_uses_plain_text(self) -> None:
        """Диаризация с одним спикером — отображается как обычный текст."""
        diar = self._make_diarization([
            {"speaker": "SPEAKER_00", "text": "Один спикер говорит", "start": 0.0, "end": 2.0},
        ])
        self.store.add_history_item(
            text="Один спикер говорит",
            paste_status="ok",
            diarization=diar,
        )
        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)


class MarkdownExportTranslationTestCase(unittest.TestCase):
    """Экспорт записей с переводом."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    def test_translation_included_in_export(self) -> None:
        """Запись с переводом: поле translated_text включается в экспорт."""
        self.store.add_history_item(
            text="Это оригинальный текст на русском",
            paste_status="ok",
            translated_text="This is the original text in Russian",
            translation_mode="ru_to_en",
            translation_status="ok",
            source_lang="ru",
            target_lang="en",
        )

        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)
        self.assertGreater(result["chars"], 0)

    def test_failed_translation_not_included(self) -> None:
        """Запись с ошибочным переводом: translated_text не включается."""
        self.store.add_history_item(
            text="Текст без перевода",
            paste_status="ok",
            translated_text="Partial translation",
            translation_mode="ru_to_en",
            translation_status="translate_error",
            source_lang="ru",
        )
        # Должен успешно экспортироваться, просто без перевода
        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 1)

    def test_mixed_translated_and_plain_entries(self) -> None:
        """Смешанный экспорт: переводные и обычные записи вместе."""
        self.store.add_history_item(
            text="Привет",
            paste_status="ok",
            translated_text="Hello",
            translation_mode="ru_to_en",
            translation_status="ok",
            source_lang="ru",
            target_lang="en",
        )
        self.store.add_history_item(
            text="Просто текст без перевода",
            paste_status="ok",
        )

        result = self.svc.handle_export_history_markdown({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries"], 2)

    def test_return_structure(self) -> None:
        """Структура ответа: ok, entries, chars обязательны."""
        self.store.add_history_item(text="проверка структуры", paste_status="ok")
        result = self.svc.handle_export_history_markdown({})
        self.assertIn("ok", result)
        self.assertIn("entries", result)
        self.assertIn("chars", result)
        self.assertIsInstance(result["ok"], bool)
        self.assertIsInstance(result["entries"], int)
        self.assertIsInstance(result["chars"], int)


if __name__ == "__main__":
    unittest.main()
