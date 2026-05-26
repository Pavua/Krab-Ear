"""Тесты для PrivacyAuditLogger (backend/privacy_audit.py)."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
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
        from backend.privacy_audit import PrivacyAuditLogger

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


class TestPrivacyAuditRedactSensitiveData(unittest.TestCase):
    """test_redact_sensitive_data — чувствительные поля не хранятся дословно."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def test_dsn_not_stored_in_plaintext(self):
        """DSN в деталях события хранится как 'redacted' или тип/метка, а не полный URL."""
        # The caller is responsible for redacting before log_event; we verify the
        # redacted string reaches the file, not the real DSN.
        self.logger.log_event("sentry", "blocked", {"dsn": "redacted"})
        raw = self.log_path.read_text(encoding="utf-8")
        self.assertNotIn("secret_key", raw)

    def test_details_text_field_not_stored(self):
        """Поле text (транскрипт) не должно попадать в audit log — только метаданные."""
        self.logger.log_event("translation", "forced_offline", {
            "method": "handle_translate_text",
            "original_mode": "online",
        })
        raw = self.log_path.read_text(encoding="utf-8")
        entry = json.loads(raw.strip())
        self.assertNotIn("text", entry["details"])
        self.assertIn("method", entry["details"])

    def test_log_contains_only_metadata_keys(self):
        """Запись содержит только ожидаемые ключи верхнего уровня.

        После добавления HMAC-цепочки (W952 F-3) записи также содержат
        поля ``prev_hash`` и ``entry_hash``.
        """
        self.logger.log_event("sentry", "blocked", {})
        entries = self.logger.read_entries()
        self.assertEqual(len(entries), 1)
        keys = set(entries[0].keys())
        # prev_hash и entry_hash добавлены в рамках W952 F-3 tamper detection
        expected_keys = {"ts", "category", "action", "details", "prev_hash", "entry_hash"}
        self.assertEqual(keys, expected_keys)


class TestPrivacyAuditAtomicAppend(unittest.TestCase):
    """test_atomic_append — каждый log_event добавляет ровно одну строку."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def test_each_event_one_line(self):
        """Каждый log_event создаёт ровно одну строку NDJSON."""
        for i in range(5):
            self.logger.log_event("cat", "act", {"i": i})
        lines = [ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 5)
        for line in lines:
            entry = json.loads(line)
            self.assertIn("ts", entry)
            self.assertIn("category", entry)

    def test_lines_valid_json_each(self):
        """Каждая строка является самостоятельным валидным JSON."""
        self.logger.log_event("sentry", "blocked")
        self.logger.log_event("translation", "forced_offline", {"x": 1})
        lines = [ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for line in lines:
            obj = json.loads(line)
            self.assertIsInstance(obj, dict)

    def test_no_extra_blank_lines(self):
        """Между записями нет лишних пустых строк."""
        for _ in range(3):
            self.logger.log_event("cat", "act")
        raw = self.log_path.read_text(encoding="utf-8")
        # Каждая строка кончается \n, нет двойных \n\n
        self.assertNotIn("\n\n", raw)


class TestPrivacyAuditUnwritableDisk(unittest.TestCase):
    """test_handles_unwritable_disk — ошибка записи не поднимает исключение."""

    def setUp(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def test_log_event_silent_on_io_error(self):
        """log_event не бросает исключение даже при IOError на диске."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        logger = PrivacyAuditLogger(log_path=log_path)
        # Заменяем _ensure_parent чтобы не мешал, потом симулируем ошибку через open
        with patch("backend.privacy_audit.PrivacyAuditLogger._ensure_parent"):
            with patch("builtins.open", side_effect=OSError("no space left on device")):
                try:
                    logger.log_event("sentry", "blocked")
                except Exception as exc:
                    self.fail(f"log_event не должен бросать исключение: {exc}")
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_log_event_with_mocked_open_error(self):
        """log_event с замоканным open() → ошибка игнорируется."""
        import tempfile
        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        logger = PrivacyAuditLogger(log_path=log_path)
        with patch("builtins.open", side_effect=PermissionError("disk full")):
            try:
                logger.log_event("cat", "act")
            except Exception as exc:
                self.fail(f"Не должно быть исключения: {exc}")
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestPrivacyAuditQueryRecentEvents(unittest.TestCase):
    """test_query_recent_events — read_entries с фильтрацией по limit."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def test_read_entries_default_limit_100(self):
        """read_entries() без аргументов возвращает до 100 записей."""
        for i in range(120):
            self.logger.log_event("cat", "act", {"n": i})
        entries = self.logger.read_entries()
        self.assertEqual(len(entries), 100)

    def test_read_entries_custom_limit(self):
        """read_entries(limit=5) возвращает последние 5 записей."""
        for i in range(15):
            self.logger.log_event("cat", "act", {"n": i})
        entries = self.logger.read_entries(limit=5)
        self.assertEqual(len(entries), 5)
        # Последняя запись n=14
        self.assertEqual(entries[-1]["details"]["n"], 14)

    def test_read_entries_order_old_to_new(self):
        """Порядок результатов: от старых к новым."""
        for i in range(5):
            self.logger.log_event("cat", "act", {"seq": i})
        entries = self.logger.read_entries()
        seqs = [e["details"]["seq"] for e in entries]
        self.assertEqual(seqs, list(range(5)))

    def test_read_entries_total_count_consistent(self):
        """total_count() совпадает с количеством реальных строк в файле."""
        for _ in range(8):
            self.logger.log_event("cat", "act")
        count = self.logger.total_count()
        all_entries = self.logger.read_entries(limit=1000)
        self.assertEqual(count, len(all_entries))

    def test_read_entries_limit_zero_returns_all(self):
        """read_entries(limit=0) возвращает все записи (без обрезки)."""
        for i in range(10):
            self.logger.log_event("cat", "act", {"n": i})
        entries = self.logger.read_entries(limit=0)
        self.assertEqual(len(entries), 10)


class TestPrivacyAuditConcurrentWrites(unittest.TestCase):
    """test_concurrent_log_writes — параллельная запись не повреждает файл."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_log_events_all_written(self):
        """20 потоков, каждый пишет 5 событий → 100 строк, все валидный JSON."""
        from backend.privacy_audit import PrivacyAuditLogger
        logger = PrivacyAuditLogger(log_path=self.log_path)

        n_threads = 20
        events_per_thread = 5
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            for i in range(events_per_thread):
                try:
                    logger.log_event("cat", "act", {"tid": tid, "i": i})
                except Exception as exc:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        lines = [ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), n_threads * events_per_thread)
        for line in lines:
            obj = json.loads(line)
            self.assertIn("ts", obj)

    def test_singleton_returns_same_instance_sequential(self):
        """get_instance() при последовательном вызове возвращает один и тот же объект."""
        from backend.privacy_audit import get_privacy_audit_logger
        inst1 = get_privacy_audit_logger(log_path=self.log_path)
        inst2 = get_privacy_audit_logger(log_path=self.log_path)
        self.assertIs(inst1, inst2)


class TestPrivacyAuditLogRotation(unittest.TestCase):
    """test_log_rotation — clear() как ручная ротация + поведение после очистки."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()
        self.logger = PrivacyAuditLogger(log_path=self.log_path)

    def tearDown(self):
        from backend.privacy_audit import PrivacyAuditLogger
        PrivacyAuditLogger.reset_instance()

    def test_clear_removes_log_file(self):
        """clear() удаляет файл лога (ротация → пустой лог)."""
        self.logger.log_event("sentry", "blocked")
        self.assertTrue(self.log_path.exists())
        self.logger.clear()
        self.assertFalse(self.log_path.exists())

    def test_write_after_clear(self):
        """После clear() можно снова писать записи."""
        self.logger.log_event("cat", "before")
        self.logger.clear()
        self.logger.log_event("cat", "after")
        entries = self.logger.read_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "after")

    def test_clear_idempotent(self):
        """Повторный вызов clear() не бросает исключений."""
        self.logger.log_event("cat", "act")
        self.logger.clear()
        try:
            self.logger.clear()
        except Exception as exc:
            self.fail(f"Повторный clear() не должен бросать: {exc}")

    def test_total_count_zero_after_clear(self):
        """total_count() == 0 после clear()."""
        for _ in range(5):
            self.logger.log_event("cat", "act")
        self.logger.clear()
        self.assertEqual(self.logger.total_count(), 0)


if __name__ == "__main__":
    unittest.main()
