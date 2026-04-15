"""Тесты DailyDigestGenerator — генератор ежедневного дайджеста Krab Ear."""

from __future__ import annotations
from backend.state_store import StateStore
from backend.daily_digest import DailyDigestGenerator, DailyDigest

from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest

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


class DailyDigestServiceIntegrationTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер generate_daily_digest через BackendService."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = StateStore(Path(self.tmp.name) / "data")

        from unittest.mock import MagicMock
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
            {"id": "1", "method": "generate_daily_digest", "params": {"date": "2024-01-15"}}
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
