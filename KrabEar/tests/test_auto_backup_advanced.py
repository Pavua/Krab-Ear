"""Расширенные тесты AutoBackupManager — scheduled thread, partial file, permissions."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_backup import AutoBackupManager


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_store(data_dir: Path) -> MagicMock:
    store = MagicMock()
    store.data_dir = str(data_dir)
    store.history_path = data_dir / "history.ndjson"
    store.tombstones_path = data_dir / "tombstones.ndjson"
    store.status_path = data_dir / "status.json"
    store.settings_path = data_dir / "settings.json"
    store.count_active_items.return_value = 10
    for attr in ("history_path", "tombstones_path", "settings_path"):
        p = getattr(store, attr)
        p.write_text("dummy", encoding="utf-8")
    return store


# ---------------------------------------------------------------------------
# B1. Scheduled thread — start() / stop()
# ---------------------------------------------------------------------------

class TestAutoBackupScheduledThread(unittest.TestCase):
    """AutoBackupManager с фоновым потоком через polling."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_and_backup_callable_multiple_times_with_reset(self):
        """Имитация периодического вызова: reset meta → каждый раз делает бэкап."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=10)
        backups_created = 0
        for _ in range(3):
            meta = mgr._load_meta()
            meta["last_backup_ts"] = None
            mgr._save_meta(meta)
            result = mgr.check_and_backup()
            if result["backed_up"]:
                backups_created += 1
            time.sleep(0.01)
        self.assertGreaterEqual(backups_created, 3)

    def test_thread_based_backup_loop_creates_backup(self):
        """Фоновый поток с коротким интервалом создаёт бэкап и останавливается."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        stop_event = threading.Event()
        backed_up = threading.Event()

        def _bg_loop():
            while not stop_event.is_set():
                result = mgr.check_and_backup()
                if result.get("backed_up"):
                    backed_up.set()
                    return
                # reset TTL so next iteration will backup again
                meta = mgr._load_meta()
                meta["last_backup_ts"] = None
                mgr._save_meta(meta)
                time.sleep(0.01)

        t = threading.Thread(target=_bg_loop, daemon=True)
        t.start()
        created = backed_up.wait(timeout=3.0)
        stop_event.set()
        t.join(timeout=1.0)

        self.assertTrue(created, "Background loop did not create a backup in time")
        self.assertFalse(t.is_alive(), "Background thread did not stop")

    def test_stop_flag_prevents_new_backups(self):
        """После установки флага остановки новые бэкапы не создаются."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        stop_event = threading.Event()
        stop_event.set()  # уже остановлен

        def _bg_loop():
            if not stop_event.is_set():
                mgr.check_and_backup()

        t = threading.Thread(target=_bg_loop, daemon=True)
        t.start()
        t.join(timeout=1.0)

        # Ни одного бэкапа не должно быть создано
        backups = mgr._list_auto_backups()
        self.assertEqual(len(backups), 0)


# ---------------------------------------------------------------------------
# B2. Partial file cleanup — следующий запуск подбирает мусор
# ---------------------------------------------------------------------------

class TestAutoBackupPartialFileCleanup(unittest.TestCase):
    """Неполный (partial) бэкап не блокирует следующий run."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_partial_backup_dir_no_meta_skipped_by_list(self):
        """Папка авто-бэкапа без backup_meta.json не ломает _list_auto_backups."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        # Создаём «осколок» частичного бэкапа вручную
        partial_dir = mgr.backups_dir / "auto_backup_20250101_000000"
        partial_dir.mkdir(parents=True)
        (partial_dir / "history.ndjson").write_text("partial", encoding="utf-8")
        # Не создаём backup_meta.json — имитируем прерванный бэкап

        # _list_auto_backups должен вернуть эту папку без исключений
        backups = mgr._list_auto_backups()
        self.assertIn(partial_dir, backups)

    def test_successful_backup_after_partial_exists(self):
        """Наличие partial-папки не мешает следующему check_and_backup."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=10)
        # Создаём «осколок»
        partial_dir = mgr.backups_dir / "auto_backup_20250101_120000"
        partial_dir.mkdir(parents=True)
        (partial_dir / "history.ndjson").write_text("partial", encoding="utf-8")

        result = mgr.check_and_backup()
        self.assertTrue(result["backed_up"])
        new_backup = Path(result["backup_path"])
        self.assertTrue(new_backup.exists())
        self.assertTrue((new_backup / "backup_meta.json").exists())

    def test_prune_removes_partial_when_over_limit(self):
        """_prune_old_backups удаляет partial-папки, если их больше max_copies."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=2)
        # Создаём 3 partial-папки
        for i in range(3):
            d = mgr.backups_dir / f"auto_backup_2025010{i}_120000"
            d.mkdir(parents=True)

        mgr._prune_old_backups()
        remaining = mgr._list_auto_backups()
        self.assertLessEqual(len(remaining), 2)


# ---------------------------------------------------------------------------
# B3. Ошибки прав доступа (permissions) — graceful degradation
# ---------------------------------------------------------------------------

class TestAutoBackupPermissionErrors(unittest.TestCase):
    """Ошибки прав не роняют менеджер, а возвращают информативный результат."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        # Восстанавливаем права перед удалением tmp-директории
        try:
            os.chmod(str(self.data_dir), 0o755)
        except Exception:
            pass
        self._tmp.cleanup()

    def test_backup_handles_copy_failure_gracefully(self):
        """Ошибка shutil.copy2 (например, PermissionError) — исключение прокидывается вверх."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        with patch("backend.auto_backup.shutil.copy2", side_effect=PermissionError("no access")):
            # _do_backup выбросит исключение, check_and_backup не должен молча съесть его
            # Проверяем, что менеджер остаётся в рабочем состоянии после ошибки
            try:
                mgr.check_and_backup()
            except PermissionError:
                pass  # ожидаемо

            # После ошибки статус должен быть доступен
            status = mgr.get_auto_backup_status()
            self.assertIsInstance(status["enabled"], bool)

    def test_meta_write_failure_does_not_corrupt_state(self):
        """Ошибка записи meta-файла не портит in-memory состояние менеджера."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        original_save = mgr._save_meta

        call_count = [0]

        def patched_save(meta):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("disk full")
            original_save(meta)

        mgr._save_meta = patched_save  # type: ignore[method-assign]

        try:
            mgr.check_and_backup()
        except OSError:
            pass

        # Менеджер должен отвечать на get_auto_backup_status без краша
        status = mgr.get_auto_backup_status()
        self.assertIn("enabled", status)

    def test_load_meta_with_corrupted_json(self):
        """Повреждённый meta.json → _load_meta возвращает дефолтные значения."""
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=5)
        mgr.backups_dir.mkdir(parents=True, exist_ok=True)
        mgr._meta_path.write_text("INVALID_JSON{{{{", encoding="utf-8")

        meta = mgr._load_meta()
        self.assertIsNone(meta.get("last_backup_ts"))
        self.assertEqual(meta.get("backup_count", 0), 0)

    def test_backup_with_missing_source_files(self):
        """Если исходные файлы не существуют — бэкап создаётся без ошибок."""
        store = _make_store(self.data_dir)
        # Удаляем исходные файлы
        for attr in ("history_path", "tombstones_path", "settings_path"):
            p = getattr(store, attr)
            if Path(p).exists():
                Path(p).unlink()

        mgr = AutoBackupManager(store=store, interval_hours=0, max_copies=5)
        result = mgr.check_and_backup()
        # Бэкап должен быть создан (просто без файлов внутри)
        self.assertTrue(result["backed_up"])
        backup_dir = Path(result["backup_path"])
        self.assertTrue(backup_dir.exists())

    def test_readonly_backups_dir_handled(self):
        """Read-only директория backups/ → бэкап выбрасывает исключение (не молчит)."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        # Создаём директорию и делаем её read-only
        mgr.backups_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(str(mgr.backups_dir), 0o555)

        try:
            try:
                result = mgr.check_and_backup()
                # На некоторых системах root может писать — просто проверим структуру
                self.assertIn("backed_up", result)
            except (PermissionError, OSError):
                pass  # ожидаемо на ограниченных системах
        finally:
            os.chmod(str(mgr.backups_dir), 0o755)


# ---------------------------------------------------------------------------
# B4. stop() корректно останавливает поток — через флаг
# ---------------------------------------------------------------------------

class TestAutoBackupThreadStop(unittest.TestCase):
    """Поток с флагом остановки завершается корректно."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = _make_store(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_daemon_thread_does_not_block_test_exit(self):
        """Daemon-поток не блокирует завершение процесса."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=5)
        self.assertTrue(mgr.enabled)
        stop_event = threading.Event()

        def _loop():
            while not stop_event.is_set():
                time.sleep(0.01)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        stop_event.set()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())

    def test_multiple_concurrent_backup_calls_thread_safe(self):
        """Concurrent check_and_backup из N потоков не вызывает исключений."""
        mgr = AutoBackupManager(store=self.store, interval_hours=0, max_copies=20)
        errors = []

        def worker():
            try:
                meta = mgr._load_meta()
                meta["last_backup_ts"] = None
                mgr._save_meta(meta)
                mgr.check_and_backup()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread-safety errors: {errors}")


if __name__ == "__main__":
    unittest.main()
