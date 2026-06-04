"""Тесты DailyDigestGenerator — генератор ежедневного дайджеста Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.daily_digest import DailyDigestGenerator, DailyDigest

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock
import sys
import tempfile
import unittest

from tests.test_helpers import make_test_item

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
        self.assertEqual(digest.date, datetime.now(timezone.utc).date().isoformat())

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
        today = datetime.now(timezone.utc).date().isoformat()
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
        today = datetime.now(timezone.utc).date().isoformat()
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
        today = datetime.now(timezone.utc).date().isoformat()

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

        item1 = make_test_item(
            text="запись 15 января",
            ts=f"{target_date}T10:00:00",
            audio_duration_sec=60.0,
            source_lang="ru",
            confidence=0.95,
        )

        item2 = make_test_item(
            text="запись 16 января",
            ts="2024-01-16T10:00:00",
            audio_duration_sec=120.0,
            source_lang="en",
            confidence=0.90,
        )

        mock_store._load_active_items_with_lock.return_value = [item1, item2]

        digest = self.gen.generate_digest(date_str=target_date, store=mock_store)
        self.assertEqual(digest.total_recordings, 1)
        self.assertIn("ru", digest.languages_used)
        self.assertNotIn("en", digest.languages_used)

    def test_digest_languages_aggregation(self) -> None:
        """Дайджест агрегирует языки из всех записей за день."""
        today = datetime.now(timezone.utc).date().isoformat()

        mock_store = Mock()

        item1 = make_test_item(
            text="русский текст",
            ts=f"{today}T10:00:00",
            audio_duration_sec=60.0,
            source_lang="ru",
            confidence=0.95,
        )

        item2 = make_test_item(
            text="spanish text",
            ts=f"{today}T11:00:00",
            audio_duration_sec=60.0,
            source_lang="es",
            confidence=0.90,
        )

        item3 = make_test_item(
            text="another russian",
            ts=f"{today}T12:00:00",
            audio_duration_sec=60.0,
            source_lang="ru",
            confidence=0.92,
        )

        mock_store._load_active_items_with_lock.return_value = [item1, item2, item3]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertEqual(digest.languages_used.get("ru"), 2)
        self.assertEqual(digest.languages_used.get("es"), 1)

    def test_digest_highlights_sorted_by_confidence(self) -> None:
        """Highlights сортируются по confidence (убывание), затем по длине."""
        today = datetime.now(timezone.utc).date().isoformat()

        mock_store = Mock()

        item1 = make_test_item(
            text="низкая уверенность",
            ts=f"{today}T10:00:00",
            audio_duration_sec=60.0,
            source_lang="ru",
            confidence=0.70,
        )

        item2 = make_test_item(
            text="очень высокая уверенность текст с большой длиной",
            ts=f"{today}T11:00:00",
            audio_duration_sec=120.0,
            source_lang="ru",
            confidence=0.98,
        )

        item3 = make_test_item(
            text="средняя уверенность",
            ts=f"{today}T12:00:00",
            audio_duration_sec=90.0,
            source_lang="ru",
            confidence=0.85,
        )

        mock_store._load_active_items_with_lock.return_value = [item1, item2, item3]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertGreater(len(digest.highlights), 0)
        # Первый highlight должен быть от item2 (confidence 0.98)
        self.assertIn("очень высокая уверенность", digest.highlights[0])

    def test_digest_highlights_truncate_long_text(self) -> None:
        """Highlights обрезаются до 200 символов с многоточием."""
        today = datetime.now(timezone.utc).date().isoformat()

        long_text = "очень длинный текст " * 30  # > 200 chars

        mock_store = Mock()

        item = make_test_item(
            text=long_text,
            ts=f"{today}T10:00:00",
            audio_duration_sec=60.0,
            source_lang="ru",
            confidence=0.95,
        )

        mock_store._load_active_items_with_lock.return_value = [item]

        digest = self.gen.generate_digest(date_str=today, store=mock_store)
        self.assertEqual(len(digest.highlights), 1)
        self.assertLessEqual(len(digest.highlights[0]), 203)  # 200 + "…"
        self.assertTrue(digest.highlights[0].endswith("…"))

    def test_digest_markdown_contains_statistics(self) -> None:
        """Markdown-отчёт содержит сводку, темы и фрагменты."""
        today = datetime.now(timezone.utc).date().isoformat()

        mock_store = Mock()

        item = make_test_item(
            text="важное ключевое слово важное",
            ts=f"{today}T10:00:00",
            audio_duration_sec=120.0,
            source_lang="ru",
            confidence=0.95,
        )

        mock_store._load_active_items_with_lock.return_value = [item]

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
        self.assertEqual(resp["result"]["date"], datetime.now(timezone.utc).date().isoformat())


def _make_mock_store(items, today=None):
    """Вспомогательная функция: mock-store, возвращающий items."""
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()

    for item in items:
        if not hasattr(item, 'ts') or not item.ts:
            item.ts = f"{today}T10:00:00"
        if not hasattr(item, 'confidence') or item.confidence is None:
            item.confidence = 0.90
        if not hasattr(item, 'audio_duration_sec') or item.audio_duration_sec is None:
            item.audio_duration_sec = 60.0
        if not hasattr(item, 'source_lang'):
            item.source_lang = "ru"

    store = Mock()
    store._load_active_items_with_lock.return_value = items
    return store


def _make_item(text, ts=None, lang="ru", confidence=0.90, duration=60.0):
    """Фабрика тестовых items."""
    return make_test_item(
        text=text,
        ts=ts or f"{datetime.now(timezone.utc).date().isoformat()}T10:00:00",
        source_lang=lang,
        confidence=confidence,
        audio_duration_sec=duration,
    )


class DailyDigestDirectItemsTestCase(unittest.TestCase):
    """Тесты с прямой передачей items через mock store."""

    def setUp(self):
        self.gen = DailyDigestGenerator()
        self.today = datetime.now(timezone.utc).date().isoformat()

    def test_empty_items_returns_graceful_empty_digest(self):
        """Пустой список items возвращает пустой дайджест без исключений."""
        store = _make_mock_store([])
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertIsInstance(digest, DailyDigest)
        self.assertEqual(digest.total_recordings, 0)
        self.assertEqual(digest.total_words, 0)
        self.assertEqual(digest.total_duration_min, 0.0)
        self.assertEqual(digest.languages_used, {})
        self.assertEqual(digest.top_topics, [])
        self.assertEqual(digest.highlights, [])

    def test_empty_items_markdown_has_no_recordings_message(self):
        """Markdown пустого дайджеста содержит сообщение об отсутствии записей."""
        store = _make_mock_store([])
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertIn("Записей за этот день не найдено", digest.formatted_markdown)

    def test_single_item_word_count(self):
        """Дайджест корректно считает слова одного item."""
        item = _make_item("один два три четыре пять")
        store = _make_mock_store([item])
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.total_recordings, 1)
        self.assertEqual(digest.total_words, 5)

    def test_multiple_items_word_count_summed(self):
        """Слова суммируются по всем items за день."""
        items = [
            _make_item("раз два три"),          # 3
            _make_item("четыре пять шесть семь"),  # 4
        ]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.total_words, 7)

    def test_duration_summed_correctly(self):
        """Длительность (мин) суммируется и конвертируется из секунд."""
        items = [
            _make_item("текст один", duration=90.0),
            _make_item("текст два", duration=90.0),
        ]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.total_duration_min, 3.0)  # 180 сек / 60

    def test_multilanguage_grouping(self):
        """Дайджест корректно группирует языки из разных items."""
        items = [
            _make_item("текст первый", lang="ru"),
            _make_item("texto en español", lang="es"),
            _make_item("english text here", lang="en"),
            _make_item("ещё один русский", lang="ru"),
        ]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.languages_used.get("ru"), 2)
        self.assertEqual(digest.languages_used.get("es"), 1)
        self.assertEqual(digest.languages_used.get("en"), 1)

    def test_multilanguage_most_frequent_in_markdown(self):
        """Markdown содержит языки, отсортированные по частоте."""
        items = [
            _make_item("rus1", lang="ru"),
            _make_item("rus2", lang="ru"),
            _make_item("es1", lang="es"),
        ]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        md = digest.formatted_markdown
        self.assertIn("ru (2)", md)
        self.assertIn("es (1)", md)
        # ru должен идти перед es (по убыванию частоты)
        self.assertLess(md.index("ru (2)"), md.index("es (1)"))

    def test_top_topics_extracted_from_items(self):
        """top_topics содержит значимые слова из транскрипций."""
        items = [
            _make_item("проект разработка разработка код"),
            _make_item("проект тестирование разработка"),
        ]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertIn("разработка", digest.top_topics)
        self.assertIn("проект", digest.top_topics)

    def test_top_topics_excludes_stop_words(self):
        """top_topics не содержит стоп-слова."""
        items = [_make_item("и в на для с это тот нужный термин термин термин")]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        stop_words_in_topics = {"и", "в", "на", "для", "с", "это", "тот"}
        for w in digest.top_topics:
            self.assertNotIn(w, stop_words_in_topics, f"Стоп-слово {w!r} в top_topics")

    def test_top_topics_max_10(self):
        """top_topics содержит не более 10 слов."""
        long_text = " ".join(f"слово{i}" * 2 for i in range(30))
        items = [_make_item(long_text)]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertLessEqual(len(digest.top_topics), 10)

    def test_items_with_no_text_handled_gracefully(self):
        """Items с пустым text не вызывают исключений."""
        item1 = _make_item("")
        item2 = _make_item("нормальный текст")
        store = _make_mock_store([item1, item2])
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.total_recordings, 2)
        self.assertEqual(digest.total_words, 2)

    def test_items_with_none_duration_handled_gracefully(self):
        """Items с audio_duration_sec=None не вызывают исключений."""
        item = make_test_item(
            text="текст",
            ts=f"{self.today}T10:00:00",
            source_lang="ru",
            confidence=0.9,
            audio_duration_sec=None,
        )

        store = Mock()
        store._load_active_items_with_lock.return_value = [item]

        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.total_duration_min, 0.0)

    def test_items_with_no_source_lang_skipped_in_languages(self):
        """Items без source_lang не попадают в languages_used."""
        item = make_test_item(
            text="текст без языка",
            ts=f"{self.today}T10:00:00",
            source_lang="",
            confidence=0.9,
            audio_duration_sec=60.0,
        )
        store = _make_mock_store([item])
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertEqual(digest.languages_used, {})

    def test_highlights_count_at_most_3(self):
        """highlights содержит не более 3 элементов."""
        items = [_make_item(f"текст записи {i}", confidence=0.9 - i * 0.01) for i in range(10)]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertLessEqual(len(digest.highlights), 3)

    def test_digest_date_matches_input(self):
        """date поле дайджеста совпадает с переданной датой."""
        store = _make_mock_store([_make_item("текст", ts="2025-06-15T09:00:00")], today="2025-06-15")
        digest = self.gen.generate_digest(date_str="2025-06-15", store=store)
        self.assertEqual(digest.date, "2025-06-15")

    def test_digest_result_is_DailyDigest_instance(self):
        """generate_digest всегда возвращает экземпляр DailyDigest."""
        items = [_make_item("проверка типа")]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertIsInstance(digest, DailyDigest)

    def test_markdown_contains_word_count(self):
        """Markdown содержит количество слов."""
        items = [_make_item("один два три")]
        store = _make_mock_store(items)
        digest = self.gen.generate_digest(date_str=self.today, store=store)
        self.assertIn("Слов", digest.formatted_markdown)
        self.assertIn("3", digest.formatted_markdown)

    def test_markdown_contains_date_header(self):
        """Markdown содержит заголовок с датой."""
        items = [_make_item("текст")]
        store = _make_mock_store(items, today="2025-03-10")
        digest = self.gen.generate_digest(date_str="2025-03-10", store=store)
        self.assertIn("2025-03-10", digest.formatted_markdown)

    def test_tokenize_function_lowercases_words(self):
        """_tokenize возвращает слова в нижнем регистре."""
        from backend.daily_digest import _tokenize
        tokens = _tokenize("Привет МИР Hello WORLD")
        for t in tokens:
            self.assertEqual(t, t.lower(), f"Токен {t!r} не в нижнем регистре")

    def test_tokenize_skips_digits_and_punctuation(self):
        """_tokenize возвращает только буквенные токены."""
        from backend.daily_digest import _tokenize
        tokens = _tokenize("hello 123 world! foo, bar.")
        for t in tokens:
            self.assertTrue(t.isalpha(), f"Токен {t!r} содержит не-буквы")


if __name__ == "__main__":
    unittest.main()
