"""Тесты DailyDigestGenerator — генератор ежедневного дайджеста Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.daily_digest import DailyDigestGenerator, DailyDigest

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, Mock
import sys
import tempfile
import unittest
from contextlib import contextmanager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class DailyDigestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.gen = DailyDigestGenerator()

    # ------------------------------------------------------------------
    # generate_digest без store
    # ------------------------------------------------------------------

    def test_generate_digest_no_store_returns_empty(self) -> None:
        """generate_digest без store возвращает пустой дайджест."""
        digest = self.gen.generate_digest(date_str="2024-01-15", store=None)
        self.assertIsInstance(digest, DailyDigest)
        self.assertEqual(digest.date, "2024-01-15")
        self.assertEqual(digest.total_recordings, 0)
        self.assertIsInstance(digest.formatted_markdown, str)

    def test_generate_digest_defaults_to_today(self) -> None:
        """generate_digest без date_str использует сегодняшнюю дату."""
        digest = self.gen.generate_digest(store=None)
        self.assertEqual(digest.date, date.today().isoformat())

    def test_invalid_date_raises_value_error(self) -> None:
        """Неверный формат даты вызывает ValueError."""
        with self.assertRaises(ValueError):
            self.gen.generate_digest(date_str="not-a-date", store=None)

    # ------------------------------------------------------------------
    # generate_digest с реальным store
    # ------------------------------------------------------------------

    def test_empty_store_returns_empty_digest(self) -> None:
        """Пустое хранилище возвращает дайджест с нулевыми счётчиками."""
        digest = self.gen.generate_digest(date_str="2024-01-15", store=self.store)
        self.assertEqual(digest.total_recordings, 0)

    def test_digest_aggregates_today_items(self) -> None:
        """Дайджест включает записи только за указанный день."""
        today = date.today().isoformat()
        self.store.add_history_item(text="первая запись сегодня", paste_status="ok")
        self.store.add_history_item(text="вторая запись сегодня", paste_status="ok")

        digest = self.gen.generate_digest(date_str=today, store=self.store)
        self.assertEqual(digest.total_recordings, 2)
        self.assertGreater(digest.total_words, 0)

    def test_digest_formatted_markdown_is_string(self) -> None:
        """formatted_markdown-поле дайджеста — непустая строка."""
        digest = self.gen.generate_digest(date_str="2024-01-15", store=self.store)
        self.assertIsInstance(digest.formatted_markdown, str)
        self.assertGreater(len(digest.formatted_markdown), 0)

    def test_digest_top_topics_is_list(self) -> None:
        """top_topics содержит список ключевых слов дня."""
        today = date.today().isoformat()
        self.store.add_history_item(
            text="привет привет привет мир", paste_status="ok"
        )
        digest = self.gen.generate_digest(date_str=today, store=self.store)
        self.assertIsInstance(digest.top_topics, list)

    # ------------------------------------------------------------------
    # Расширенные тесты (дополнение coverage)
    # ------------------------------------------------------------------

    def test_digest_multiple_items_calculates_duration(self) -> None:
        """Дайджест корректно агрегирует duration из нескольких items."""
        today = date.today().isoformat()

        self.store.add_history_item(text="первая запись", audio_duration_sec=60.0)
        self.store.add_history_item(text="вторая запись", audio_duration_sec=120.0)

        digest = self.gen.generate_digest(date_str=today, store=self.store)
        self.assertEqual(digest.total_recordings, 2)
        self.assertEqual(digest.total_duration_min, 3.0)  # 180 сек / 60 = 3 мин

    def test_digest_filters_by_date(self) -> None:
        """Дайджест включает только записи за указанный день."""
        target_date = "2024-01-15"

        # Создаём mock store с items за разные дни
        mock_store = Mock()

        item1 = Mock()
        item1.text = "запись 15 января"
        item1.ts = f"{target_date}T10:00:00"
        item1.audio_duration_sec = 60.0
        item1.source_lang = "ru"
        item1.confidence = 0.95

        item2 = Mock()
        item2.text = "запись 16 января"
        item2.ts = "2024-01-16T10:00:00"
        item2.audio_duration_sec = 120.0
        item2.source_lang = "en"
        item2.confidence = 0.90

        @contextmanager
        def mock_lock():
            yield

        mock_store._lock = mock_lock
        mock_store._load_active_items_unlocked.return_value = [item1, item2]

        digest = self.gen.generate_digest(date_str=target_date, store=mock_store)
        self.assertEqual(digest.total_recordings, 1)
        self.assertIn("ru", digest.languages_used)
        self.assertNotIn("en", digest.languages_used)

    def test_digest_languages_aggregation(self) -> None:
        """Дайджест агрегирует языки из всех записей за день."""
        today = date.today().isoformat()

        mock_store = Mock()

        item1 = Mock()
        item1.text = "русский текст"
        item1.ts = f"{today}T10:00:00"
        item1.audio_duration_sec = 60.0
        item1.source_lang = "ru"
        item1.confidence = 0.95

        item2 = Mock()
        item2.text = "spanish text"
        item2.ts = f"{today}T11:00:00"
        item2.audio_duration_sec = 60.0
        item2.source_lang = "es"
        item2.confidence = 0.90

        item3 = Mock()
        item3.text = "another russian"
        item3.ts = f"{today}T12:00:00"
        item3.audio_duration_sec = 60.0
        item3.source_lang = "ru"
        item3.confidence = 0.92

        @contextmanager
        def mock_lock():
            yield

        mock_store._lock = mock_lock
        mock_store._load_active_items_unlocked.return_value = [item1, item2, item3]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertEqual(digest.languages_used.get("ru"), 2)
        self.assertEqual(digest.languages_used.get("es"), 1)

    def test_digest_highlights_sorted_by_confidence(self) -> None:
        """Highlights сортируются по confidence (убывание), затем по длине."""
        today = date.today().isoformat()

        mock_store = Mock()

        item1 = Mock()
        item1.text = "низкая уверенность"
        item1.ts = f"{today}T10:00:00"
        item1.audio_duration_sec = 60.0
        item1.source_lang = "ru"
        item1.confidence = 0.70

        item2 = Mock()
        item2.text = "очень высокая уверенность текст с большой длиной"
        item2.ts = f"{today}T11:00:00"
        item2.audio_duration_sec = 120.0
        item2.source_lang = "ru"
        item2.confidence = 0.98

        item3 = Mock()
        item3.text = "средняя уверенность"
        item3.ts = f"{today}T12:00:00"
        item3.audio_duration_sec = 90.0
        item3.source_lang = "ru"
        item3.confidence = 0.85

        @contextmanager
        def mock_lock():
            yield

        mock_store._lock = mock_lock
        mock_store._load_active_items_unlocked.return_value = [item1, item2, item3]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertGreater(len(digest.highlights), 0)
        # Первый highlight должен быть от item2 (confidence 0.98)
        self.assertIn("очень высокая уверенность", digest.highlights[0])

    def test_digest_highlights_truncate_long_text(self) -> None:
        """Highlights обрезаются до 200 символов с многоточием."""
        today = date.today().isoformat()

        long_text = "очень длинный текст " * 30  # > 200 chars

        mock_store = Mock()

        item = Mock()
        item.text = long_text
        item.ts = f"{today}T10:00:00"
        item.audio_duration_sec = 60.0
        item.source_lang = "ru"
        item.confidence = 0.95

        @contextmanager
        def mock_lock():
            yield

        mock_store._lock = mock_lock
        mock_store._load_active_items_unlocked.return_value = [item]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertEqual(len(digest.highlights), 1)
        self.assertLessEqual(len(digest.highlights[0]), 203)  # 200 + "…"
        self.assertTrue(digest.highlights[0].endswith("…"))

    def test_digest_markdown_contains_statistics(self) -> None:
        """Markdown-отчёт содержит сводку, темы и фрагменты."""
        today = date.today().isoformat()

        mock_store = Mock()

        item = Mock()
        item.text = "важное ключевое слово важное"
        item.ts = f"{today}T10:00:00"
        item.audio_duration_sec = 120.0
        item.source_lang = "ru"
        item.confidence = 0.95

        @contextmanager
        def mock_lock():
            yield

        mock_store._lock = mock_lock
        mock_store._load_active_items_unlocked.return_value = [item]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        md = digest.formatted_markdown

        self.assertIn("Дайджест транскрипций", md)
        self.assertIn("Сводка", md)
        self.assertIn("1", md)  # 1 запись
        self.assertIn("2", md)  # 2 минуты
        if digest.top_topics:
            self.assertIn("Темы дня", md)
        if digest.highlights:
            self.assertIn("Избранные фрагменты", md)


class DailyDigestServiceIntegrationTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер generate_daily_digest через BackendService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        recorder = MagicMock()
        recorder.is_recording = False
        transcriber = MagicMock()
        translator = MagicMock()

        from backend.service import BackendService
        self.svc = BackendService(
            store=store, recorder=recorder,
            transcriber=transcriber, translator=translator
        )

    def test_generate_daily_digest_handler(self) -> None:
        """IPC-хэндлер generate_daily_digest возвращает dict с обязательными полями."""
        resp = self.svc.handle_request(
            {"id": "1", "method": "generate_daily_digest",
             "params": {"date": "2024-01-15"}}
        )
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("date", result)
        self.assertIn("total_recordings", result)
        self.assertIn("markdown", result)

    def test_generate_daily_digest_no_date(self) -> None:
        """generate_daily_digest без даты использует сегодня."""
        resp = self.svc.handle_request(
            {"id": "2", "method": "generate_daily_digest", "params": {}}
        )
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["date"], date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
