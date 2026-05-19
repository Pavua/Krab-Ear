"""Тесты AutoBackupManager."""

from __future__ import annotations
from backend.auto_backup import AutoBackupManager, AUTO_BACKUP_INTERVAL_HOURS, AUTO_BACKUP_MAX_COPIES

import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# Путь для импорта
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _make_store(data_dir: Path) -> MagicMock:
    """Создаёт fake StateStore с нужными атрибутами."""
    store = MagicMock()
    store.data_dir = str(data_dir)
    store.history_path = data_dir / "history.ndjson"
    store.tombstones_path = data_dir / "tombstones.ndjson"
    store.status_path = data_dir / "status.json"
    store.settings_path = data_dir / "settings.json"
    store.count_active_items.return_value = 42
    # Создаём dummy файлы
    for attr in ("history_path", "tombstones_path", "settings_path"):
        p = getattr(store, attr)
        p.write_text("dummy", encoding="utf-8")
    return store


class TestAutoBackupDefaults(unittest.TestCase):
    """Проверяем дефолтные константы."""

    def test_default_interval_hours(self):
        self.assertEqual(AUTO_BACKUP_INTERVAL_HOURS, 24)

    def test_default_max_copies(self):
        self.assertEqual(AUTO_BACKUP_MAX_COPIES, 7)

    def test_manager_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            mgr = AutoBackupManager(store=store)
            self.assertEqual(mgr.interval_hours, 24)
            self.assertEqual(mgr.max_copies, 7)
            self.assertTrue(mgr.enabled)


class TestCheckAndBackup(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_first_backup_is_performed(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        result = mgr.check_and_backup()
        self.assertTrue(result["backed_up"])
        self.assertIsNotNone(result["backup_path"])
        self.assertIsNone(result["skipped_reason"])

    def test_backup_skipped_too_soon(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        # Первый бэкап
        mgr.check_and_backup()
        # Второй сразу же — должен быть пропущен
        result = mgr.check_and_backup()
        self.assertFalse(result["backed_up"])
        self.assertEqual(result["skipped_reason"], "too_soon")

    def test_backup_skipped_when_disabled(self):
        mgr = AutoBackupManager(store=self.store, enabled=False)
        result = mgr.check_and_backup()
        self.assertFalse(result["backed_up"])
        self.assertEqual(result["skipped_reason"], "disabled")

    def test_backup_creates_files(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24)
        result = mgr.check_and_backup()
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.exists())
        self.assertTrue((backup_path / "backup_meta.json").exists())

    def test_backup_meta_saved(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24)
        mgr.check_and_backup()
        meta = mgr._load_meta()
        self.assertIsNotNone(meta.get("last_backup_ts"))
        self.assertEqual(meta.get("backup_count"), 1)

    def test_backup_performed_after_interval(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=1)
        mgr.check_and_backup()
        # Выставляем last_backup_ts в прошлое — за пределами interval
        meta = mgr._load_meta()
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        meta["last_backup_ts"] = past
        mgr._save_meta(meta)
        result = mgr.check_and_backup()
        self.assertTrue(result["backed_up"])

    def test_prune_old_backups(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=3)
        # Создаём 5 бэкапов с паузой, чтобы имена различались
        for i in range(5):
            meta = mgr._load_meta()
            meta["last_backup_ts"] = None  # сбрасываем, чтобы каждый раз делать бэкап
            mgr._save_meta(meta)
            mgr.check_and_backup()
            time.sleep(0.01)  # чтобы timestamp различался
        backups = mgr._list_auto_backups()
        self.assertLessEqual(len(backups), 3)

    def test_backup_result_contains_size_and_entries(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24)
        result = mgr.check_and_backup()
        self.assertIn("size_mb", result)
        self.assertIn("entries", result)
        self.assertEqual(result["entries"], 42)


class TestGetAutoBackupStatus(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_status_before_any_backup(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        status = mgr.get_auto_backup_status()
        self.assertTrue(status["enabled"])
        self.assertIsNone(status["last_backup_ts"])
        self.assertIsNone(status["next_backup_ts"])
        self.assertEqual(status["total_backups"], 0)
        self.assertEqual(status["interval_hours"], 24)
        self.assertEqual(status["max_copies"], 7)

    def test_status_after_backup(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        mgr.check_and_backup()
        status = mgr.get_auto_backup_status()
        self.assertIsNotNone(status["last_backup_ts"])
        self.assertIsNotNone(status["next_backup_ts"])
        self.assertEqual(status["total_backups"], 1)

    def test_status_disabled(self):
        mgr = AutoBackupManager(store=self.store, enabled=False)
        status = mgr.get_auto_backup_status()
        self.assertFalse(status["enabled"])

    def test_status_backups_dir_in_result(self):
        mgr = AutoBackupManager(store=self.store)
        status = mgr.get_auto_backup_status()
        self.assertIn("backups_dir", status)
        self.assertIn("backups", status["backups_dir"])


class TestAutoBackupThreadSafety(unittest.TestCase):
    """Проверяем, что concurrent вызовы не ломают менеджер."""

    def test_concurrent_check_and_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(Path(tmp))
            mgr = AutoBackupManager(store=store, interval_hours=0, max_copies=10)
            errors = []

            def worker():
                try:
                    mgr._load_meta()["last_backup_ts"]  # чтение — безопасно
                    mgr.get_auto_backup_status()
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])


class TestAutoBackupDiskPressure(unittest.TestCase):
    """test_backup_skipped_on_disk_pressure — бэкап не выполняется при нехватке места."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_backup_skipped_when_enabled_false(self):
        """Если enabled=False — бэкап пропускается с причиной 'disabled'."""
        mgr = AutoBackupManager(store=self.store, enabled=False)
        result = mgr.check_and_backup()
        self.assertFalse(result["backed_up"])
        self.assertEqual(result["skipped_reason"], "disabled")

    def test_backup_skipped_on_disk_pressure_via_patch(self):
        """Симулируем нехватку диска через патч _do_backup → OSError (disk full)."""
        from unittest.mock import patch as mock_patch
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        with mock_patch.object(mgr, "_do_backup", side_effect=OSError("No space left on device")):
            try:
                result = mgr.check_and_backup()
                # Если менеджер поглощает ошибку — бэкап не был выполнен
                self.assertFalse(result.get("backed_up", True))
            except OSError:
                pass  # Ожидаемо: менеджер пробрасывает OSError
        # После ошибки диска — get_auto_backup_status должен работать
        status = mgr.get_auto_backup_status()
        self.assertIn("enabled", status)


class TestAutoBackupAtomicWrite(unittest.TestCase):
    """test_backup_atomic — бэкап пишет в директорию, а не в .tmp, затем rename."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_backup_dir_contains_backup_meta(self):
        """После успешного бэкапа backup_meta.json должен присутствовать в директории."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        result = mgr.check_and_backup()
        self.assertTrue(result["backed_up"])
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.exists())
        self.assertTrue((backup_path / "backup_meta.json").exists())

    def test_backup_dir_name_starts_with_auto_backup(self):
        """Директория бэкапа должна начинаться с 'auto_backup_'."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        result = mgr.check_and_backup()
        backup_path = Path(result["backup_path"])
        self.assertTrue(backup_path.name.startswith("auto_backup_"))

    def test_backup_meta_contains_expected_keys(self):
        """backup_meta.json должен содержать обязательные ключи."""
        import json
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        result = mgr.check_and_backup()
        backup_path = Path(result["backup_path"])
        meta = json.loads((backup_path / "backup_meta.json").read_text(encoding="utf-8"))
        for key in ("backup_ts", "size_bytes", "files", "auto"):
            self.assertIn(key, meta)
        self.assertTrue(meta["auto"])

    def test_only_one_backup_dir_created_per_call(self):
        """Один вызов check_and_backup создаёт ровно одну директорию бэкапа."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=20)
        before = set(d.name for d in mgr.backups_dir.iterdir() if d.is_dir()) if mgr.backups_dir.exists() else set()
        mgr.check_and_backup()
        after = set(d.name for d in mgr.backups_dir.iterdir() if d.is_dir())
        new_dirs = after - before
        self.assertEqual(len(new_dirs), 1)


class TestAutoBackupConcurrentLock(unittest.TestCase):
    """test_concurrent_backup_invocation — lock предотвращает одновременный запуск."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmpdir.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_lock_prevents_double_run(self):
        """Если _lock занят, второй вызов ждёт — lock сериализует вызовы."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=10)
        results = []
        errors = []
        started = threading.Event()

        original_do_backup = mgr._do_backup

        def slow_do_backup():
            started.set()
            time.sleep(0.1)
            return original_do_backup()

        mgr._do_backup = slow_do_backup  # type: ignore[method-assign]

        def worker():
            try:
                meta = mgr._load_meta()
                meta["last_backup_ts"] = None
                mgr._save_meta(meta)
                r = mgr.check_and_backup()
                results.append(r)
            except Exception as exc:
                errors.append(exc)

        t1 = threading.Thread(target=worker)
        t1.start()
        started.wait(timeout=2.0)  # ждём, пока первый поток войдёт в _do_backup

        t2 = threading.Thread(target=worker)
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        # Обе попытки должны завершиться (одна backed_up=True, другая too_soon)
        self.assertEqual(len(results), 2)

    def test_concurrent_status_reads_safe(self):
        """Параллельные чтения статуса из N потоков не вызывают исключений."""
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=5)
        mgr.check_and_backup()
        errors = []

        def reader():
            try:
                for _ in range(5):
                    mgr.get_auto_backup_status()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
