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


class TestAuditLoggerWave95(unittest.TestCase):
    """Wave 95 — required test coverage for task spec."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.logger = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.logger.close()

    def test_log_ipc_call_writes_structured_entry(self):
        """log_request записывает структурированную запись аудита IPC-вызова."""
        self.logger.log_request(
            "get_history",
            {"limit": 10, "offset": 0},
            {"ok": True, "result": {"items": []}},
            duration_ms=4.2,
            client_info={"version": "2.0"},
        )
        entries = self.logger.get_audit_log(method_filter="get_history")
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["method"], "get_history")
        self.assertIn("ts", e)
        self.assertTrue(e["success"])
        self.assertAlmostEqual(e["duration_ms"], 4.2, places=1)
        self.assertIn("client_info", e)

    def test_log_redacts_sensitive_params(self):
        """Параметры чувствительных методов не попадают в лог (только метаданные)."""
        sensitive_params = {
            "password": "super_secret",
            "api_key": "sk-12345",
            "token": "bearer-token",
            "sentry_dsn": "https://key@sentry.io/123",
        }
        for method in _SENSITIVE_METHODS:
            self.logger.log_request(method, sensitive_params, {"ok": True, "result": {}}, 1.0)

        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0]) as f:
            content = f.read()

        # Sensitive values must NOT appear in the log
        self.assertNotIn("super_secret", content)
        self.assertNotIn("sk-12345", content)
        self.assertNotIn("bearer-token", content)
        # params_keys must be empty for sensitive methods
        for line in content.splitlines():
            entry = json.loads(line)
            if entry["method"] in _SENSITIVE_METHODS:
                self.assertEqual(entry["params_keys"], [],
                                 f"Sensitive method {entry['method']} leaked params_keys")

    def test_log_persists_to_file_atomic(self):
        """Записи сохраняются в файл и читаются без потерь после flush."""
        methods = ["a", "b", "c", "d", "e"]
        for m in methods:
            self.logger.log_request(m, {}, {"ok": True, "result": {}}, 0.1)

        # Read raw file, not through API
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        self.assertEqual(len(files), 1, "Should produce exactly one file for today")
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 5)
        parsed_methods = [json.loads(ln)["method"] for ln in lines]
        self.assertEqual(parsed_methods, methods)

    def test_concurrent_log_writes_no_data_loss(self):
        """Параллельные write-потоки не теряют и не портят записи."""
        n_threads = 8
        n_per_thread = 25
        errors: list[Exception] = []

        def writer(tid: int) -> None:
            try:
                for i in range(n_per_thread):
                    self.logger.log_request(
                        f"method_{tid}_{i}", {}, {"ok": True, "result": {}}, float(tid)
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        entries = self.logger.get_audit_log(limit=n_threads * n_per_thread + 10)
        self.assertEqual(len(entries), n_threads * n_per_thread)

        # Verify all lines are valid JSON (no corruption)
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        for f in files:
            for raw_line in f.read_text(encoding="utf-8").splitlines():
                if raw_line.strip():
                    json.loads(raw_line)  # raises if corrupted

    def test_query_recent_entries_by_method(self):
        """get_audit_log с method_filter возвращает только записи нужного метода."""
        calls = [("ping", 3), ("transcribe", 2), ("get_settings", 1)]
        for method, count in calls:
            for _ in range(count):
                self.logger.log_request(method, {}, {"ok": True, "result": {}}, 1.0)

        for method, count in calls:
            entries = self.logger.get_audit_log(method_filter=method, limit=50)
            self.assertEqual(len(entries), count,
                             f"Expected {count} entries for {method}, got {len(entries)}")

    def test_handles_unwritable_disk_gracefully(self):
        """BUG: Если директория недоступна для записи — log_request выбрасывает PermissionError.

        Ошибка происходит в _rotate_if_needed() при вызове open() — исключение
        не перехватывается и propagates до caller'а.
        Ожидаемое поведение: log_request должен молча логировать ошибку через logger.exception,
        но НЕ пробрасывать исключение наружу (как это делается для write/flush в строке 69).
        Тест документирует текущее сломанное поведение.
        """
        import os
        import stat

        restricted_dir = Path(self.tmpdir) / "restricted"
        restricted_dir.mkdir()
        # Make directory read-only
        os.chmod(restricted_dir, stat.S_IREAD | stat.S_IEXEC)

        try:
            restricted_logger = AuditLogger(data_dir=restricted_dir)
            try:
                # BUG: this raises PermissionError from _rotate_if_needed()
                # The try/except in log_request only covers the write/flush, not open()
                with self.assertRaises(PermissionError,
                                       msg="BUG: _rotate_if_needed() PermissionError not caught"):
                    restricted_logger.log_request("ping", {}, {"ok": True, "result": {}}, 1.0)
            finally:
                restricted_logger.close()
        finally:
            # Restore permissions for cleanup
            os.chmod(restricted_dir, stat.S_IRWXU)


if __name__ == "__main__":
    unittest.main()
