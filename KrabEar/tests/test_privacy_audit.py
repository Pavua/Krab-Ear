"""Тесты для PrivacyAuditLogger (backend/privacy_audit.py)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.privacy_audit import PrivacyAuditLogger  # noqa: E402


class TestPrivacyAuditLoggerAppend(unittest.TestCase):
    """Базовые тесты append NDJSON."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "privacy_audit.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_append_single_entry(self):
        """log_event создаёт файл и добавляет одну запись."""
        self.logger.log_event("sentry", "blocked")
        self.assertTrue(self.log_path.exists())
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["category"], "sentry")
        self.assertEqual(entry["action"], "blocked")
        self.assertIn("ts", entry)
        self.assertIn("details", entry)

    def test_append_multiple_entries(self):
        """Несколько вызовов log_event добавляют несколько строк."""
        for i in range(5):
            self.logger.log_event("translation", "forced_offline", {"i": i})
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 5)

    def test_details_stored(self):
        """details сохраняются в записи."""
        self.logger.log_event("translation", "forced_offline", {"original_mode": "online", "method": "handle_translate_text"})
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        self.assertEqual(entry["details"]["original_mode"], "online")
        self.assertEqual(entry["details"]["method"], "handle_translate_text")

    def test_entry_without_details(self):
        """Запись без details содержит пустой словарь."""
        self.logger.log_event("sentry", "blocked")
        lines = self.log_path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[0])
        self.assertEqual(entry["details"], {})


class TestPrivacyAuditLoggerParentDir(unittest.TestCase):
    """Тест авто-создания родительской директории."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_auto_create_parent_dir(self):
        """Родительская директория создаётся автоматически."""
        nested = Path(self.tmpdir) / "deeply" / "nested" / "dir" / "audit.log"
        self.assertFalse(nested.parent.exists())
        logger = PrivacyAuditLogger(log_path=nested)
        logger.log_event("test", "action")
        self.assertTrue(nested.exists())


class TestPrivacyAuditLoggerLimit(unittest.TestCase):
    """Тест параметра limit в read_entries."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_limit_truncates(self):
        """read_entries(limit=3) возвращает не более 3 записей."""
        for i in range(10):
            self.logger.log_event("cat", "act", {"n": i})
        entries = self.logger.read_entries(limit=3)
        self.assertEqual(len(entries), 3)
        # Последние 3 по порядку
        self.assertEqual(entries[-1]["details"]["n"], 9)
        self.assertEqual(entries[0]["details"]["n"], 7)

    def test_limit_100_default(self):
        """read_entries без аргумента возвращает до 100 записей."""
        for i in range(50):
            self.logger.log_event("cat", "act")
        entries = self.logger.read_entries()
        self.assertEqual(len(entries), 50)

    def test_total_count(self):
        """total_count() возвращает точное число записей."""
        for _ in range(7):
            self.logger.log_event("cat", "act")
        self.assertEqual(self.logger.total_count(), 7)


class TestPrivacyAuditLoggerMissingFile(unittest.TestCase):
    """Тест поведения при отсутствии лог-файла."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "missing.log"
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_read_entries_missing_file(self):
        """read_entries возвращает [] если файл не существует."""
        self.assertFalse(self.log_path.exists())
        entries = self.logger.read_entries()
        self.assertEqual(entries, [])

    def test_total_count_missing_file(self):
        """total_count() возвращает 0 если файл не существует."""
        self.assertFalse(self.log_path.exists())
        self.assertEqual(self.logger.total_count(), 0)


class TestSentryBlockLog(unittest.TestCase):
    """Тест логирования когда Sentry заблокирован privacy mode."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def test_sentry_blocked_logs_entry(self):
        """init_sentry с privacy_mode_enabled=True и dsn → log category=sentry action=blocked."""
        from backend.privacy_audit import PrivacyAuditLogger, get_privacy_audit_logger

        # Подменяем singleton
        test_logger = PrivacyAuditLogger(log_path=self.log_path)
        PrivacyAuditLogger._instance = test_logger

        from backend.observability import init_sentry
        result = init_sentry(
            dsn="https://fake@sentry.io/1234",
            settings={"privacy_mode_enabled": True},
        )
        self.assertFalse(result)

        entries = test_logger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "sentry")
        self.assertEqual(entries[0]["action"], "blocked")

    def test_sentry_no_log_when_no_dsn(self):
        """init_sentry без DSN при privacy_mode=True НЕ пишет в audit log."""
        from backend.privacy_audit import PrivacyAuditLogger

        test_logger = PrivacyAuditLogger(log_path=self.log_path)
        PrivacyAuditLogger._instance = test_logger

        from backend.observability import init_sentry
        init_sentry(dsn=None, settings={"privacy_mode_enabled": True})

        entries = test_logger.read_entries()
        self.assertEqual(len(entries), 0)


class TestTranslationForcedOfflineLog(unittest.TestCase):
    """Тест логирования forced_offline для translate."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        PrivacyAuditLogger.reset_instance()

    def _make_translation_service(self):
        """Создаёт TranslationService с заглушками."""
        from backend.translation_service import TranslationService

        # Заглушка translator.translate
        fake_result = MagicMock()
        fake_result.text = "translated"
        fake_result.status = "ok"
        fake_result.source_lang = "ru"
        fake_result.target_lang = "es"
        fake_result.mode = "ru_to_es"
        fake_result.engine = "argos"

        translator = MagicMock()
        translator.translate.return_value = fake_result

        store = MagicMock()
        settings_data = {
            "privacy_mode_enabled": True,
            "network_mode": "online",
            "translation_glossary": {},
            "translation_style": "neutral",
        }
        cached_settings = lambda: settings_data  # noqa: E731
        invalidate = lambda: None  # noqa: E731

        svc = TranslationService(
            translator=translator,
            store=store,
            cached_settings=cached_settings,
            invalidate_settings_cache=invalidate,
        )
        return svc

    def test_translate_text_forced_offline_logged(self):
        """handle_translate_text логирует forced_offline когда privacy on и mode!=offline_only."""
        test_logger = PrivacyAuditLogger(log_path=self.log_path)
        PrivacyAuditLogger._instance = test_logger

        svc = self._make_translation_service()
        svc.handle_translate_text({
            "text": "привет",
            "translation_mode": "ru_to_es",
        })

        entries = test_logger.read_entries()
        self.assertGreater(len(entries), 0)
        audit_entry = entries[0]
        self.assertEqual(audit_entry["category"], "translation")
        self.assertEqual(audit_entry["action"], "forced_offline")
        self.assertEqual(audit_entry["details"]["method"], "handle_translate_text")
        self.assertEqual(audit_entry["details"]["original_mode"], "online")

    def test_translate_selection_forced_offline_logged(self):
        """handle_translate_selection логирует forced_offline."""
        test_logger = PrivacyAuditLogger(log_path=self.log_path)
        PrivacyAuditLogger._instance = test_logger

        svc = self._make_translation_service()
        svc.handle_translate_selection({
            "text": "привет",
        })

        entries = test_logger.read_entries()
        # Должна быть минимум одна запись translation/forced_offline
        trans_entries = [e for e in entries if e.get("category") == "translation" and e.get("action") == "forced_offline"]
        self.assertGreater(len(trans_entries), 0)
        self.assertEqual(trans_entries[0]["details"]["method"], "handle_translate_selection")

    def test_no_log_when_privacy_off(self):
        """Когда privacy_mode_enabled=False, записей в audit log нет."""
        test_logger = PrivacyAuditLogger(log_path=self.log_path)
        PrivacyAuditLogger._instance = test_logger

        from backend.translation_service import TranslationService

        fake_result = MagicMock()
        fake_result.text = "translated"
        fake_result.status = "ok"
        fake_result.source_lang = "ru"
        fake_result.target_lang = "es"
        fake_result.mode = "ru_to_es"
        fake_result.engine = "argos"

        translator = MagicMock()
        translator.translate.return_value = fake_result

        settings_data = {
            "privacy_mode_enabled": False,
            "network_mode": "online",
            "translation_glossary": {},
            "translation_style": "neutral",
        }
        svc = TranslationService(
            translator=translator,
            store=MagicMock(),
            cached_settings=lambda: settings_data,
            invalidate_settings_cache=lambda: None,
        )
        svc.handle_translate_text({"text": "hi", "translation_mode": "ru_to_es"})

        entries = test_logger.read_entries()
        self.assertEqual(len(entries), 0)


if __name__ == "__main__":
    unittest.main()
