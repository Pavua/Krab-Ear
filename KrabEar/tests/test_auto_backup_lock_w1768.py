"""W1768 — регрессия: AutoBackupManager._do_backup делает снимок истории
атомарно относительно append-ов и компактирования StateStore.

Баг (до W1768): _do_backup копировал history.ndjson / tombstones / status тремя
независимыми shutil.copy2() БЕЗ удержания state_store._lock (fcntl.flock,
который сериализует ВСЕ записи истории + compaction). Компактирование, попавшее
МЕЖДУ покопийными copy2, спаривало pre-compact history.ndjson с post-compact
tombstones (или наоборот) → при восстановлении воскресшие/потерянные записи
(integrity/privacy-регрессия).

Фикс: 3 копии файлов + чтение settings.json обёрнуты в `with store._lock()`.
count_active_items() (который сам берёт lock) вызывается ВНЕ блока, иначе
flock — НЕ реентрантный — заблокировал бы процесс навсегда.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Путь для импорта backend.* / core.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.auto_backup import AutoBackupManager
from backend.state_store import StateStore


class _LockSpyStore:
    """Лёгкий fake StateStore со spy-обёрткой вокруг _lock().

    Считает, сколько раз был взят lock, и проверяет, что повторного захвата
    (который на реальном flock привёл бы к deadlock) внутри блока нет.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)
        self.history_path = data_dir / "history.ndjson"
        self.tombstones_path = data_dir / "tombstones.ndjson"
        self.status_path = data_dir / "status.json"
        self.settings_path = data_dir / "settings.json"
        for p in (self.history_path, self.tombstones_path,
                  self.status_path, self.settings_path):
            p.write_text("dummy", encoding="utf-8")
        self.lock_enter_count = 0
        self._lock_depth = 0
        self.reentrant_violation = False

    @contextmanager
    def _lock(self):
        self.lock_enter_count += 1
        if self._lock_depth > 0:
            # Реальный fcntl.flock здесь бы навсегда заблокировался.
            self.reentrant_violation = True
        self._lock_depth += 1
        try:
            yield
        finally:
            self._lock_depth -= 1

    def count_active_items(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> int:
        # Если бы этот вызов оказался ВНУТРИ блока _lock — _lock_depth был бы > 0.
        if self._lock_depth > 0:
            self.reentrant_violation = True
        return 7


class TestDoBackupAcquiresLockOnce(unittest.TestCase):
    """_do_backup берёт store._lock ровно один раз и не реентрит его."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = _LockSpyStore(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_acquired_exactly_once(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        result = mgr._do_backup()
        # Снимок (3 файла + settings) — ровно один захват lock.
        self.assertEqual(
            self.store.lock_enter_count, 1,
            f"_do_backup должен брать store._lock ровно один раз, "
            f"получено {self.store.lock_enter_count}",
        )
        # count_active_items не должен вызываться внутри блока (иначе deadlock на flock).
        self.assertFalse(
            self.store.reentrant_violation,
            "обнаружен повторный/вложенный захват store._lock — flock не реентрантен",
        )
        self.assertIn("backup_path", result)

    def test_backup_still_produces_three_files_plus_meta(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        result = mgr._do_backup()
        backup_dir = Path(result["backup_path"])
        self.assertTrue(backup_dir.exists())
        # 3 verbatim-файла + settings.json + backup_meta.json
        for name in ("history.ndjson", "tombstones.ndjson",
                     "status.json", "settings.json", "backup_meta.json"):
            self.assertTrue(
                (backup_dir / name).exists(),
                f"файл {name} должен присутствовать в бэкапе",
            )
        # entries берётся из count_active_items (вне lock)
        self.assertEqual(result["entries"], 7)


class TestRealStoreNoDeadlock(unittest.TestCase):
    """С реальным StateStore (настоящий fcntl.flock) бэкап не виснет."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name) / "data")

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_and_backup_completes_without_hang(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)
        result_box = {}

        def _run():
            result_box["result"] = mgr.check_and_backup()

        t = threading.Thread(target=_run, name="autobackup-w1768")
        t.start()
        t.join(timeout=20.0)
        self.assertFalse(
            t.is_alive(),
            "check_and_backup завис — вероятен реентрантный deadlock на store._lock",
        )
        self.assertTrue(result_box["result"]["backed_up"])
        backup_dir = Path(result_box["result"]["backup_path"])
        self.assertTrue((backup_dir / "history.ndjson").exists())


class TestCompactionSerializedDuringCopy(unittest.TestCase):
    """Компактирование, запрошенное во время копирования, сериализуется (ждёт)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name) / "data")

    def tearDown(self):
        self._tmp.cleanup()

    def test_compaction_blocks_until_backup_releases_lock(self):
        mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)

        copy_started = threading.Event()
        release_copy = threading.Event()
        order: list[str] = []
        order_lock = threading.Lock()

        real_copy2 = __import__("shutil").copy2

        def _slow_copy2(src, dst, *a, **kw):
            # Сигналим о старте копирования при ПЕРВОМ файле и держим lock,
            # пока конкурирующий поток пытается войти в compaction.
            first = not copy_started.is_set()
            res = real_copy2(src, dst, *a, **kw)
            if first:
                with order_lock:
                    order.append("copy")
                copy_started.set()
                # Удерживаем lock внутри _do_backup до сигнала.
                release_copy.wait(timeout=10.0)
            return res

        def _compactor():
            copy_started.wait(timeout=10.0)
            # В этот момент бэкап держит store._lock внутри copy2 → compaction
            # обязан заблокироваться на flock до release_copy.
            self.store.compact_with_stats()
            with order_lock:
                order.append("compact")

        with patch("backend.auto_backup.shutil.copy2", side_effect=_slow_copy2):
            comp_thread = threading.Thread(target=_compactor, name="compactor-w1768")
            comp_thread.start()

            backup_box = {}

            def _run_backup():
                backup_box["result"] = mgr._do_backup()

            backup_thread = threading.Thread(target=_run_backup, name="backup-w1768")
            backup_thread.start()

            # Ждём, пока копирование реально стартовало.
            self.assertTrue(copy_started.wait(timeout=10.0), "копирование не стартовало")
            # Дать компактору шанс попытаться войти (он должен ЗАБЛОКИРОВАТЬСЯ).
            time.sleep(0.3)
            with order_lock:
                # До освобождения lock compaction НЕ должен был завершиться.
                self.assertNotIn(
                    "compact", order,
                    "compaction завершилась во время копирования — снимок не атомарен",
                )
            # Отпускаем копирование → бэкап освобождает lock → compaction проходит.
            release_copy.set()

            backup_thread.join(timeout=15.0)
            comp_thread.join(timeout=15.0)
            self.assertFalse(backup_thread.is_alive(), "backup-поток завис")
            self.assertFalse(comp_thread.is_alive(), "compaction-поток завис")

        # Порядок: копирование снимка состоялось ДО завершения compaction.
        self.assertEqual(order, ["copy", "compact"])


if __name__ == "__main__":
    unittest.main()
