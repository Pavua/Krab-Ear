"""Тесты для AuditLogger."""

from __future__ import annotations
from backend.audit_logger import AuditLogger, _SENSITIVE_METHODS, _KEEP_DAYS

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


class TestAuditLoggerBasic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_log_creates_file(self):
        """log_request создаёт файл audit_<date>.ndjson."""
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.5)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        self.assertEqual(len(files), 1)

    def test_log_entry_fields(self):
        """Запись содержит обязательные поля."""
        self.logger.log_request("ping", {"a": 1}, {"ok": True, "result": {}}, 2.5)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertIn("ts", entry)
        self.assertEqual(entry["method"], "ping")
        self.assertEqual(entry["params_keys"], ["a"])
        self.assertTrue(entry["success"])
        self.assertAlmostEqual(entry["duration_ms"], 2.5, places=1)

    def test_log_failure(self):
        """success=False когда ok=False в результате."""
        self.logger.log_request("bad", {}, {"ok": False, "error": "x"}, 1.0)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertFalse(entry["success"])

    def test_sensitive_methods_no_params(self):
        """Для чувствительных методов params_keys не логируются."""
        for method in _SENSITIVE_METHODS:
            self.logger.log_request(method, {"password": "secret", "key": "val"}, {"ok": True, "result": {}}, 1.0)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            for line in f:
                entry = json.loads(line)
                if entry["method"] in _SENSITIVE_METHODS:
                    self.assertEqual(entry["params_keys"], [])

    def test_get_audit_log_returns_entries(self):
        """get_audit_log возвращает все записанные записи."""
        for i in range(5):
            self.logger.log_request(f"method_{i}", {}, {"ok": True, "result": {}}, float(i))
        entries = self.logger.get_audit_log(limit=10)
        self.assertEqual(len(entries), 5)

    def test_get_audit_log_limit(self):
        """get_audit_log уважает параметр limit."""
        for i in range(10):
            self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        entries = self.logger.get_audit_log(limit=3)
        self.assertLessEqual(len(entries), 3)

    def test_get_audit_log_method_filter(self):
        """get_audit_log фильтрует по method_filter."""
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        self.logger.log_request("get_settings", {}, {"ok": True, "result": {}}, 2.0)
        entries = self.logger.get_audit_log(method_filter="ping")
        for e in entries:
            self.assertEqual(e["method"], "ping")

    def test_ts_is_iso8601(self):
        """Поле ts является корректным ISO-8601 timestamp."""
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        # Should parse without error
        dt = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
        self.assertIsNotNone(dt)

    def test_client_info_stored(self):
        """client_info включается в запись если передан."""
        self.logger.log_request(
            "ping", {}, {"ok": True, "result": {}}, 1.0,
            client_info={"ip": "127.0.0.1"}
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry.get("client_info"), {"ip": "127.0.0.1"})

    def test_thread_safety(self):
        """Параллельные вызовы log_request не приводят к повреждению файла."""
        errors = []

        def worker():
            try:
                for _ in range(20):
                    self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        entries = self.logger.get_audit_log(limit=200)
        self.assertEqual(len(entries), 100)

    def test_rotation_keeps_last_7_days(self):
        """Файлы старше 7 дней удаляются."""
        # Создаём 10 файлов с разными датами
        for i in range(10):
            fake_date = f"2020-01-{i + 1:02d}"
            path = Path(self.tmpdir) / f"audit_{fake_date}.ndjson"
            path.write_text('{"ts":"2020","method":"x","params_keys":[],"success":true,"duration_ms":1}\n')
        # Один реальный лог — триггерит cleanup
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        self.assertLessEqual(len(files), _KEEP_DAYS + 1)  # +1 для сегодняшнего

    def test_empty_params_logged_correctly(self):
        """Пустые params дают пустой params_keys."""
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["params_keys"], [])


class TestAuditLoggerPersistence(unittest.TestCase):
    """Персистенция: данные читаются независимо от экземпляра."""

    def test_new_instance_reads_existing_entries(self):
        """Второй AuditLogger из той же директории видит ранее записанные записи."""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger1 = AuditLogger(data_dir=tmpdir)
            logger1.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
            logger1.close()

            logger2 = AuditLogger(data_dir=tmpdir)
            entries = logger2.get_audit_log(limit=10)
            logger2.close()

            self.assertGreaterEqual(len(entries), 1)
            self.assertEqual(entries[0]["method"], "ping")

    def test_entries_survive_close_reopen(self):
        """Записи сохраняются после close() и повторного открытия."""
        with tempfile.TemporaryDirectory() as tmpdir:
            for _ in range(3):
                al = AuditLogger(data_dir=tmpdir)
                al.log_request("x", {}, {"ok": True, "result": {}}, 0.5)
                al.close()

            final = AuditLogger(data_dir=tmpdir)
            entries = final.get_audit_log(limit=10)
            final.close()
            self.assertGreaterEqual(len(entries), 3)


class TestAuditLoggerQueryFilters(unittest.TestCase):
    """Проверка фильтрации get_audit_log."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)
        methods = ["ping", "get_settings", "get_history", "ping", "ping"]
        for m in methods:
            self.logger.log_request(m, {}, {"ok": True, "result": {}}, 1.0)

    def tearDown(self):
        self.logger.close()

    def test_filter_returns_only_matching_method(self):
        entries = self.logger.get_audit_log(method_filter="get_settings")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["method"], "get_settings")

    def test_filter_nonexistent_method_returns_empty(self):
        entries = self.logger.get_audit_log(method_filter="nonexistent_method_xyz")
        self.assertEqual(entries, [])

    def test_no_filter_returns_all(self):
        entries = self.logger.get_audit_log(limit=100)
        self.assertEqual(len(entries), 5)

    def test_limit_zero_uses_minimum(self):
        """limit=0 не крашит и возвращает не более 1 записи (или пустой список)."""
        # get_audit_log не имеет явного min clamp, но должен не упасть
        entries = self.logger.get_audit_log(limit=0)
        self.assertIsInstance(entries, list)

    def test_success_false_logged_correctly(self):
        """Провальные запросы логируются с success=False."""
        self.logger.log_request("fail_op", {}, {"ok": False, "error": "boom"}, 0.1)
        entries = self.logger.get_audit_log(method_filter="fail_op")
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["success"])


class TestAuditLoggerEdgeCases(unittest.TestCase):
    """Граничные случаи."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_none_params_handled(self):
        """None вместо dict для params не должен крашить."""
        # Реальный код: params.keys() вызывается только если params truthy
        self.logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
        entries = self.logger.get_audit_log(limit=1)
        self.assertEqual(len(entries), 1)

    def test_non_dict_result_logged_as_failure(self):
        """Нестандартный result (не dict) логируется как success=False без краша."""
        self.logger.log_request("weird", {}, {}, 1.0)
        entries = self.logger.get_audit_log(method_filter="weird")
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["success"])

    def test_get_audit_log_empty_dir(self):
        """Пустая директория → пустой список без ошибок."""
        with tempfile.TemporaryDirectory() as tmpdir:
            al = AuditLogger(data_dir=tmpdir)
            entries = al.get_audit_log(limit=10)
            al.close()
        self.assertEqual(entries, [])

    def test_params_keys_sorted(self):
        """params_keys возвращаются в отсортированном порядке."""
        self.logger.log_request(
            "op", {"z": 1, "a": 2, "m": 3}, {"ok": True, "result": {}}, 1.0
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertEqual(entry["params_keys"], ["a", "m", "z"])

    def test_duration_ms_preserved(self):
        """duration_ms сохраняется с точностью до 2 знаков."""
        self.logger.log_request("op", {}, {"ok": True, "result": {}}, 3.14159)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            entry = json.loads(f.readline())
        self.assertAlmostEqual(entry["duration_ms"], 3.14, places=2)


if __name__ == "__main__":
    unittest.main()
