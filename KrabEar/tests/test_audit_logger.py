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


if __name__ == "__main__":
    unittest.main()
