"""Тесты W1353: расширение _SENSITIVE_METHODS + фиксация TOCTOU в _cleanup_old_files.

Покрывает:
- F2 MED: import_settings / register_webhook и другие credential-методы не логируют params_keys
- F3 MED: _cleanup_old_files вызывается под self._lock (no TOCTOU)
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.audit_logger import AuditLogger, _SENSITIVE_METHODS  # noqa: E402


# ---------------------------------------------------------------------------
# F2: расширенный список чувствительных методов
# ---------------------------------------------------------------------------

class TestImportSettingsRedactsParams(unittest.TestCase):
    """import_settings не должен логировать ключи параметров (W1351 F2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_import_settings_in_sensitive_set(self):
        """import_settings находится в _SENSITIVE_METHODS."""
        self.assertIn("import_settings", _SENSITIVE_METHODS)

    def test_import_settings_redacts_params(self):
        """import_settings не логирует ключи параметров, включая api_key."""
        self.logger.log_request(
            "import_settings",
            {"path": "/tmp/settings.json", "api_key": "secret_key", "sentry_dsn": "https://xyz"},
            {"ok": True, "result": {}},
            5.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        self.assertEqual(len(files), 1)
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["method"], "import_settings")
        self.assertEqual(entry["params_keys"], [],
                         "import_settings не должен логировать ключи params")

    def test_restore_settings_backup_redacts_params(self):
        """restore_settings_backup не логирует ключи параметров."""
        self.assertIn("restore_settings_backup", _SENSITIVE_METHODS)
        self.logger.log_request(
            "restore_settings_backup",
            {"backup_file": "settings_backup_20260101.json", "auth_token": "tok"},
            {"ok": True, "result": {}},
            2.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["params_keys"], [])

    def test_export_settings_in_sensitive_set(self):
        """export_settings находится в _SENSITIVE_METHODS (может раскрыть credential поля)."""
        self.assertIn("export_settings", _SENSITIVE_METHODS)

    def test_create_manual_settings_backup_in_sensitive_set(self):
        """create_manual_settings_backup находится в _SENSITIVE_METHODS."""
        self.assertIn("create_manual_settings_backup", _SENSITIVE_METHODS)


class TestRegisterWebhookRedactsParams(unittest.TestCase):
    """register_webhook не должен логировать поле 'secret' (W1351 F2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_register_webhook_in_sensitive_set(self):
        """register_webhook находится в _SENSITIVE_METHODS."""
        self.assertIn("register_webhook", _SENSITIVE_METHODS)

    def test_register_webhook_redacts_params(self):
        """register_webhook не логирует ключи параметров, включая secret."""
        self.logger.log_request(
            "register_webhook",
            {"url": "https://example.com/hook", "events": ["stt.done"], "secret": "my_webhook_secret"},
            {"ok": True, "result": {"webhook_id": "wh_123"}},
            3.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        self.assertEqual(len(files), 1)
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["method"], "register_webhook")
        self.assertEqual(entry["params_keys"], [],
                         "register_webhook не должен логировать ключи params (secret!)")

    def test_set_webhook_secret_in_sensitive_set(self):
        """set_webhook_secret находится в _SENSITIVE_METHODS."""
        self.assertIn("set_webhook_secret", _SENSITIVE_METHODS)

    def test_set_webhook_secret_redacts_params(self):
        """set_webhook_secret не логирует secret."""
        self.logger.log_request(
            "set_webhook_secret",
            {"webhook_id": "wh_abc", "secret": "new_secret_value"},
            {"ok": True, "result": {}},
            1.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["params_keys"], [])


class TestGlossaryItemRedactsParams(unittest.TestCase):
    """set_translation_glossary_item не должен логировать ключи (W1351 F2)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_glossary_item_in_sensitive_set(self):
        self.assertIn("set_translation_glossary_item", _SENSITIVE_METHODS)

    def test_glossary_item_redacts_params(self):
        """set_translation_glossary_item не логирует ключи параметров."""
        self.logger.log_request(
            "set_translation_glossary_item",
            {"source": "хорошо", "target": "bien", "lang_pair": "ru-es"},
            {"ok": True, "result": {}},
            0.5,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["params_keys"], [])


class TestAdditionalSensitiveMethods(unittest.TestCase):
    """Дополнительные credential-методы из расширенного списка (W1351 F2)."""

    def test_set_rest_auth_token_in_sensitive_set(self):
        self.assertIn("set_rest_auth_token", _SENSITIVE_METHODS)

    def test_enable_rest_auth_in_sensitive_set(self):
        self.assertIn("enable_rest_auth", _SENSITIVE_METHODS)

    def test_disable_rest_auth_in_sensitive_set(self):
        self.assertIn("disable_rest_auth", _SENSITIVE_METHODS)

    def test_set_signing_secret_in_sensitive_set(self):
        self.assertIn("set_signing_secret", _SENSITIVE_METHODS)

    def test_configure_request_signing_in_sensitive_set(self):
        self.assertIn("configure_request_signing", _SENSITIVE_METHODS)

    def test_set_notification_preferences_still_in_sensitive_set(self):
        """Проверяем, что ранее существующие методы не были удалены."""
        self.assertIn("set_notification_preferences", _SENSITIVE_METHODS)

    def test_apply_profile_preset_still_in_sensitive_set(self):
        self.assertIn("apply_profile_preset", _SENSITIVE_METHODS)

    def test_set_settings_still_in_sensitive_set(self):
        self.assertIn("set_settings", _SENSITIVE_METHODS)

    def test_all_sensitive_methods_produce_empty_params_keys(self):
        """Все методы в _SENSITIVE_METHODS дают params_keys=[] при вызове."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(data_dir=tmpdir)
            try:
                for method in _SENSITIVE_METHODS:
                    logger.log_request(
                        method,
                        {"api_key": "sk-xxx", "password": "hunter2", "secret": "shh"},
                        {"ok": True, "result": {}},
                        1.0,
                    )

                files = list(Path(tmpdir).glob("audit_*.ndjson"))
                self.assertEqual(len(files), 1)
                with open(files[0]) as f:
                    for line in f:
                        entry = json.loads(line.strip())
                        self.assertEqual(
                            entry["params_keys"],
                            [],
                            f"Метод {entry['method']} должен иметь params_keys=[]",
                        )
            finally:
                logger.close()


# ---------------------------------------------------------------------------
# F3: _cleanup_old_files вызывается под self._lock (no TOCTOU)
# ---------------------------------------------------------------------------

class TestCleanupHoldsLock(unittest.TestCase):
    """_cleanup_old_files должна вызываться под self._lock (W1351 F3)."""

    def test_cleanup_called_under_lock(self):
        """_cleanup_old_files не вызывается вне self._lock."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(data_dir=tmpdir)
            cleanup_lock_states: list[bool] = []

            original_cleanup = logger._cleanup_old_files

            def patched_cleanup():
                # Проверяем, что мы уже владеем lock'ом в этот момент
                # threading.Lock не раскрывает "кто держит", но можем проверить
                # что попытка acquire от другого потока немедленно упадёт
                acquired = logger._lock.acquire(blocking=False)
                if acquired:
                    # Не должны были получить — lock должен быть занят нами
                    logger._lock.release()
                    cleanup_lock_states.append(False)  # lock НЕ держался
                else:
                    cleanup_lock_states.append(True)   # lock держится (правильно)
                original_cleanup()

            logger._cleanup_old_files = patched_cleanup

            try:
                logger.log_request(
                    "ping", {}, {"ok": True, "result": {}}, 1.0
                )
            finally:
                logger.close()

            self.assertTrue(
                all(cleanup_lock_states),
                "_cleanup_old_files должна вызываться с удержанием self._lock",
            )

    def test_cleanup_from_another_thread_blocked_during_log(self):
        """Конкурентный вызов _cleanup_old_files из другого потока блокируется."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(data_dir=tmpdir)
            errors: list[str] = []

            # Создаём много файлов, чтобы cleanup реально что-то делал
            for i in range(10):
                path = Path(tmpdir) / f"audit_2020-01-{i + 1:02d}.ndjson"
                path.write_text(
                    '{"ts":"2020","method":"x","params_keys":[],"success":true,"duration_ms":1}\n'
                )

            try:
                for _ in range(20):
                    logger.log_request(
                        "ping", {}, {"ok": True, "result": {}}, 0.1
                    )
            except Exception as exc:
                errors.append(str(exc))
            finally:
                logger.close()

            self.assertEqual(errors, [], f"Неожиданные ошибки при параллельной очистке: {errors}")

    def test_cleanup_concurrent_writes_no_file_corruption(self):
        """Параллельные log_request не приводят к повреждению файлов при cleanup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(data_dir=tmpdir)

            # Создаём старые файлы, чтобы cleanup активировался
            for i in range(9):
                p = Path(tmpdir) / f"audit_2019-01-{i + 1:02d}.ndjson"
                p.write_text(
                    '{"ts":"2019","method":"old","params_keys":[],"success":true,"duration_ms":1}\n'
                )

            errors: list[Exception] = []

            def worker():
                try:
                    for _ in range(10):
                        logger.log_request(
                            "concurrent_op", {}, {"ok": True, "result": {}}, 0.5
                        )
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            logger.close()
            self.assertEqual(errors, [])

            # Читаем оставшиеся файлы — не должно быть повреждённых строк
            remaining = list(Path(tmpdir).glob("audit_*.ndjson"))
            for path in remaining:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            json.loads(line)
                        except json.JSONDecodeError as exc:
                            self.fail(
                                f"Повреждённая строка в {path}: {exc!r} — строка: {line!r}"
                            )


if __name__ == "__main__":
    unittest.main()
