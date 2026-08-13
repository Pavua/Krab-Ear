"""wave-25 — TOCTOU purge-race fixes для archive_manager + auto_backup.

Контекст: handle_purge_all_data усекает archive.ndjson (ArchiveManager.clear_all)
и удаляет backups/ (rmtree). Но без дополнительной защиты:

  - ArchiveManager.archive_items мог записать в archive.ndjson БЕЗ проверки, что
    privacy-purge произошёл между снимком активной истории и дозаписью → purge,
    за которым сразу следует archive, воскрешал бы PII-записи (TOCTOU).
  - AutoBackupManager.check_and_backup мог ПЕРЕСОЗДАТЬ backups/ сразу после
    rmtree() в purge-теле → PII-снапшоты истории воскресают.

Дополнительно (lock starvation): archive_items держал межпроцессную fcntl.flock
истории на всё время итерации по item_ids — гигантский список блокировал бы
запись/компактирование. Теперь item_ids валидируются и кэпируются (≤100) ДО
захвата lock.

Покрытие:
  B1-a — epoch-проверка: archive после purge → отмена (purge_in_progress).
  B1-b — кэп: 101 id → too_many_ids; мусорные id отсекаются до lock.
  B1-c — bounded load: _read_archive усекает при пределе.
  B2   — backup после set_purged → skipped; set_purged удаляет backups/;
         clear_purged переразрешает; re-check под lock.
  wiring — service._handle_purge_all_data взводит/снимает purge-флаг (в т.ч. при
           исключении в теле purge).
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Путь для импорта backend.* / core.*
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend import archive_manager as archive_mod
from backend.archive_manager import ArchiveManager, ArchiveResult
from backend.auto_backup import AutoBackupManager
from backend.state_store import StateStore


# ===========================================================================
# Фейки для fallback-ветки ArchiveManager (без unlocked-API StateStore)
# ===========================================================================

class _FakeItem:
    def __init__(self, item_id: str, text: str) -> None:
        self.id = item_id
        self.text = text

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "ts": "2026-01-01T00:00:00"}


class _FakeStore:
    """Минимальный store БЕЗ _lock/_load_active_items_unlocked → fallback-путь."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._items: dict[str, _FakeItem] = {}
        self._deleted: set[str] = set()

    def add(self, item_id: str, text: str) -> None:
        self._items[item_id] = _FakeItem(item_id, text)

    def get_history_item_by_id(self, item_id: str) -> _FakeItem | None:
        if item_id in self._deleted:
            return None
        return self._items.get(item_id)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.add(item_id)
            return True
        return False


# ===========================================================================
# B1-b — валидация + кэп item_ids ДО захвата store-flock
# ===========================================================================

class TestArchiveBatchCap(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _FakeStore(Path(self._tmp.name))
        self.mgr = ArchiveManager(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_101_ids_rejected(self) -> None:
        """101 валидный id → ошибка too_many_ids (lock starvation guard)."""
        ids = [f"id{i}" for i in range(101)]
        result = self.mgr.archive_items(ids)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "too_many_ids")
        self.assertEqual(result["max"], 100)
        self.assertEqual(result["got"], 101)

    def test_handle_archive_items_propagates_too_many(self) -> None:
        """IPC-обёртка прокидывает dict-ошибку as-is."""
        ids = [f"id{i}" for i in range(101)]
        out = self.mgr.handle_archive_items({"item_ids": ids})
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "too_many_ids")

    def test_exactly_100_ids_ok(self) -> None:
        """Граница: ровно 100 id допустимо (не отклоняется)."""
        for i in range(100):
            self.store.add(f"id{i}", f"text{i}")
        ids = [f"id{i}" for i in range(100)]
        result = self.mgr.archive_items(ids)
        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 100)

    def test_garbage_ids_dropped_before_lock(self) -> None:
        """Не-строки, пустые и сверхдлинные id отсекаются (не доходят до архива)."""
        self.store.add("good", "real text")
        oversized = "x" * (archive_mod._MAX_ITEM_ID_LEN + 1)
        mixed = ["good", "", "   ", None, 123, oversized, {"x": 1}]
        result = self.mgr.archive_items(mixed)  # type: ignore[arg-type]
        self.assertIsInstance(result, ArchiveResult)
        # Только "good" реально архивируется.
        self.assertEqual(result.archived_count, 1)

    def test_duplicate_ids_deduped_in_validation(self) -> None:
        """Дубликаты схлопываются до уникальных — кэп считается по уникальным."""
        self.store.add("a", "ta")
        # 200 повторов одного id → после dedup это 1, не превышает кэп.
        result = self.mgr.archive_items(["a"] * 200)
        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 1)


# ===========================================================================
# B1-a — epoch-проверка закрывает TOCTOU между снимком и записью
# ===========================================================================

class TestArchiveEpochGuardFallback(unittest.TestCase):
    """Fallback-ветка (_FakeStore): purge между снимком epoch и работой → отмена."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _FakeStore(Path(self._tmp.name))
        self.mgr = ArchiveManager(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_archive_after_purge_blocked(self) -> None:
        """clear_all() инкрементит epoch; конкурентный archive отменяется."""
        self.store.add("a", "secret")

        # Симулируем purge между снимком epoch и работой:
        # 1-й вызов _current_epoch = снимок (0), 2-й = re-check в fallback-ветке (99).
        calls = {"n": 0}

        real = self.mgr._current_epoch

        def _fake_epoch() -> int:
            calls["n"] += 1
            return 0 if calls["n"] == 1 else 99

        with patch.object(self.mgr, "_current_epoch", side_effect=_fake_epoch):
            result = self.mgr.archive_items(["a"])

        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "purge_in_progress")
        # Запись НЕ удалена из активной истории (fail-safe).
        self.assertIsNotNone(self.store.get_history_item_by_id("a"))
        # Архив пуст — PII не записан.
        self.assertEqual(self.mgr._read_archive(), [])
        # sanity: real method still callable
        self.assertIsInstance(real(), int)

    def test_clear_all_increments_epoch(self) -> None:
        """clear_all() поднимает purge_epoch (наблюдаемо через _current_epoch)."""
        before = self.mgr._current_epoch()
        self.mgr.clear_all()
        after = self.mgr._current_epoch()
        self.assertEqual(after, before + 1)

    def test_real_purge_then_archive_blocked(self) -> None:
        """End-to-end: реальный clear_all ДО archive той же записи → отмена.

        Снимок epoch берётся внутри archive_items уже ПОСЛЕ clear_all, поэтому
        здесь проверяем именно отсутствие воскрешения: запись отсутствует в
        активной истории после purge — archive не найдёт её и ничего не запишет.
        """
        self.store.add("a", "secret")
        # Полный purge архива (epoch++). Активная история тоже была бы очищена
        # реальным purge; эмулируем удалением записи.
        self.mgr.clear_all()
        self.store.delete_history_item("a")
        result = self.mgr.archive_items(["a"])
        # Запись не найдена → archived_count 0, архив пуст (PII не воскрешён).
        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 0)
        self.assertEqual(self.mgr._read_archive(), [])


class TestArchiveEpochGuardAtomic(unittest.TestCase):
    """Атомарная ветка (реальный StateStore с unlocked-API): epoch re-check под flock."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name) / "data")
        self.mgr = ArchiveManager(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_atomic_path_used(self) -> None:
        """Sanity: реальный StateStore идёт по атомарному пути."""
        self.assertTrue(self.mgr._store_supports_atomic_archive(self.store))

    def test_normal_archive_works(self) -> None:
        """Без гонки archive_items нормально архивирует (нет регрессии)."""
        item = self.store.add_history_item(text="hello world")
        result = self.mgr.archive_items([item.id])
        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 1)
        archived = self.mgr._read_archive()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["id"], item.id)
        # Запись удалена из активной истории (tombstone).
        self.assertIsNone(self.store.get_history_item_by_id(item.id))

    def test_concurrent_purge_under_flock_aborts(self) -> None:
        """Epoch меняется ПОД store-flock (re-check) → archive отменяется.

        Снимок epoch берётся ДО store._lock(); затем эмулируем конкурентный
        clear_all() (epoch++), который произошёл в окне до захвата flock, через
        monkeypatch _current_epoch: первый вызов = snapshot, второй (под flock) =
        изменённое значение.
        """
        item = self.store.add_history_item(text="secret pii")

        calls = {"n": 0}
        base = self.mgr._purge_epoch

        def _fake_epoch() -> int:
            calls["n"] += 1
            # 1-й вызов — снимок ДО flock; со 2-го — «уже был purge».
            return base if calls["n"] == 1 else base + 1

        with patch.object(self.mgr, "_current_epoch", side_effect=_fake_epoch):
            result = self.mgr.archive_items([item.id])

        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "purge_in_progress")
        # Запись НЕ потеряна — осталась в активной истории.
        self.assertIsNotNone(self.store.get_history_item_by_id(item.id))
        # Архив пуст — PII не воскрешён.
        self.assertEqual(self.mgr._read_archive(), [])


# ===========================================================================
# B1 — bounded lock-hold: store._lock берётся ровно один раз
# ===========================================================================

class TestArchiveBoundedLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = StateStore(data_dir=Path(self._tmp.name) / "data")
        self.mgr = ArchiveManager(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_store_lock_acquired_once_for_batch(self) -> None:
        """Для целого батча store._lock берётся один раз (bounded hold)."""
        ids = []
        for i in range(20):
            ids.append(self.store.add_history_item(text=f"t{i}").id)

        real_lock = self.store._lock
        counter = {"n": 0}

        def _counting_lock(*a: Any, **kw: Any) -> Any:
            counter["n"] += 1
            return real_lock(*a, **kw)

        with patch.object(self.store, "_lock", side_effect=_counting_lock):
            result = self.mgr.archive_items(ids)

        self.assertIsInstance(result, ArchiveResult)
        self.assertEqual(result.archived_count, 20)
        # Один захват flock на весь батч — не по разу на id.
        self.assertEqual(counter["n"], 1)


# ===========================================================================
# B1-c — bounded load (_read_archive усекает при пределе)
# ===========================================================================

class TestArchiveBoundedLoad(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _FakeStore(Path(self._tmp.name))
        self.mgr = ArchiveManager(store=self.store)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_archive_truncates_at_limit(self) -> None:
        """При превышении _MAX_ARCHIVE_LOAD загрузка обрезается."""
        # Пишем 30 записей напрямую в архив.
        for i in range(30):
            self.mgr._append_ndjson(self.mgr._archive_path, {"id": f"x{i}", "text": "t"})
        # Опускаем лимит до 10 → должно вернуться ровно 10.
        with patch.object(archive_mod, "_MAX_ARCHIVE_LOAD", 10):
            items = self.mgr._read_archive()
        self.assertEqual(len(items), 10)

    def test_read_archive_no_truncation_under_limit(self) -> None:
        """Ниже лимита — все записи возвращаются."""
        for i in range(5):
            self.mgr._append_ndjson(self.mgr._archive_path, {"id": f"y{i}", "text": "t"})
        items = self.mgr._read_archive()
        self.assertEqual(len(items), 5)


# ===========================================================================
# B2 — AutoBackupManager purge-guard
# ===========================================================================

class _BackupStore:
    """Минимальный store для AutoBackupManager (файлы + count_active_items)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = str(data_dir)
        d = data_dir
        d.mkdir(parents=True, exist_ok=True)
        self.history_path = d / "history.ndjson"
        self.tombstones_path = d / "tombstones.ndjson"
        self.status_path = d / "status.json"
        self.settings_path = d / "settings.json"
        for p in (self.history_path, self.tombstones_path,
                  self.status_path, self.settings_path):
            p.write_text("{}", encoding="utf-8")

    def count_active_items(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> int:
        return 1


class TestAutoBackupPurgeGuard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _BackupStore(Path(self._tmp.name) / "data")
        self.mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_backup_after_set_purged_skipped(self) -> None:
        """После set_purged() check_and_backup пропускается (reason=purged)."""
        self.mgr.set_purged()
        self.assertTrue(self.mgr.is_purged())
        out = self.mgr.check_and_backup()
        self.assertFalse(out["backed_up"])
        self.assertEqual(out["skipped_reason"], "purged")
        # backups/ не создан.
        self.assertFalse(self.mgr.backups_dir.exists())

    def test_set_purged_removes_existing_backups(self) -> None:
        """set_purged() сразу удаляет уже существующий backups/ с PII."""
        # Сначала делаем реальный бэкап.
        first = self.mgr.check_and_backup()
        self.assertTrue(first["backed_up"])
        self.assertTrue(self.mgr.backups_dir.exists())
        # set_purged() должен снести его.
        self.mgr.set_purged()
        self.assertFalse(self.mgr.backups_dir.exists())

    def test_clear_purged_reenables_backups(self) -> None:
        """clear_purged() переразрешает бэкапы со следующего цикла."""
        self.mgr.set_purged()
        self.assertTrue(self.mgr.is_purged())
        self.mgr.clear_purged()
        self.assertFalse(self.mgr.is_purged())
        out = self.mgr.check_and_backup()
        self.assertTrue(out["backed_up"])
        self.assertTrue(self.mgr.backups_dir.exists())

    def test_recheck_under_lock_blocks_recreate(self) -> None:
        """Purge, стартовавший пока бэкап держит lock, ловится re-check'ом.

        Эмулируем гонку: внутри _do_backup (под self._lock) взводим _purged через
        обёртку _do_backup, имитируя set()-в-окне. Re-check ПЕРЕД _do_backup
        обязан увидеть флаг и не вызвать запись.
        """
        # Взводим флаг прямо перед re-check'ом, но ПОСЛЕ начального is_set() (который
        # на старте False). Делаем это, патча _load_meta так, чтобы он установил
        # purge внутри удержания lock.
        orig_load = self.mgr._load_meta

        def _load_then_purge() -> dict:
            meta = orig_load()
            self.mgr._purged.set()  # purge «приходит», пока держим lock
            return meta

        with patch.object(self.mgr, "_load_meta", side_effect=_load_then_purge):
            out = self.mgr.check_and_backup()

        self.assertFalse(out["backed_up"])
        self.assertEqual(out["skipped_reason"], "purged")
        # backups/ не создан re-check'ом.
        self.assertFalse(any(self.mgr.backups_dir.glob("auto_backup_*"))
                         if self.mgr.backups_dir.exists() else False)

    def test_normal_backup_unaffected(self) -> None:
        """Без purge обычный бэкап работает (нет регрессии)."""
        out = self.mgr.check_and_backup()
        self.assertTrue(out["backed_up"])
        self.assertFalse(self.mgr.is_purged())


class TestAutoBackupPurgeGuardConcurrent(unittest.TestCase):
    """Реальная гонка потоков: set_purged во время удержания backup-lock не виснет."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _BackupStore(Path(self._tmp.name) / "data")
        self.mgr = AutoBackupManager(store=self.store, interval_hours=24, max_copies=7)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_set_purged_during_backup_no_deadlock(self) -> None:
        in_backup = threading.Event()
        release = threading.Event()

        real_do = self.mgr._do_backup

        def _slow_do() -> dict:
            in_backup.set()
            release.wait(timeout=10.0)
            return real_do()

        backup_box: dict[str, Any] = {}

        def _run_backup() -> None:
            with patch.object(self.mgr, "_do_backup", side_effect=_slow_do):
                backup_box["res"] = self.mgr.check_and_backup()

        t = threading.Thread(target=_run_backup, name="backup-w25")
        t.start()
        self.assertTrue(in_backup.wait(timeout=10.0))

        # set_purged() взводит Event ДО захвата _lock → не блокируется на backup-lock
        # навсегда; rmtree подождёт освобождения lock.
        purge_box: dict[str, Any] = {}

        def _run_purge() -> None:
            self.mgr.set_purged()
            purge_box["done"] = True

        pt = threading.Thread(target=_run_purge, name="purge-w25")
        pt.start()
        # Дать set_purged взвести Event (set() до lock).
        time.sleep(0.2)
        self.assertTrue(self.mgr.is_purged())

        # Отпускаем backup.
        release.set()
        t.join(timeout=15.0)
        pt.join(timeout=15.0)
        self.assertFalse(t.is_alive(), "backup-поток завис (deadlock)")
        self.assertFalse(pt.is_alive(), "purge-поток завис (deadlock)")
        self.assertTrue(purge_box.get("done"))


# ===========================================================================
# wiring — service._handle_purge_all_data взводит/снимает purge-флаг
# ===========================================================================

class _StubAutoBackup:
    def __init__(self) -> None:
        self.set_calls = 0
        self.clear_calls = 0
        self.order: list[str] = []

    def set_purged(self) -> None:
        self.set_calls += 1
        self.order.append("set")

    def clear_purged(self) -> None:
        self.clear_calls += 1
        self.order.append("clear")


class _StubHistory:
    def __init__(self, raise_exc: bool = False) -> None:
        self.called = 0
        self.raise_exc = raise_exc
        self.order_ref: list[str] | None = None

    def handle_purge_all_data(self, params: dict[str, Any]) -> dict[str, Any]:
        self.called += 1
        if self.order_ref is not None:
            self.order_ref.append("purge")
        if self.raise_exc:
            raise RuntimeError("boom")
        return {"ok": True, "purged": True}


class _ServicePurgeShim:
    """Минимальный shim, переиспользующий несвязанный метод BackendService.

    Импортируем сам метод _handle_purge_all_data из service.py и привязываем к
    shim'у с подменёнными _auto_backup/_history — без полной инициализации
    BackendService (которая тянет mlx и т.п.).
    """

    def __init__(self, history: _StubHistory, auto_backup: _StubAutoBackup) -> None:
        self._history = history
        self._auto_backup = auto_backup


class TestServiceWiring(unittest.TestCase):
    def _bound_handler(self, shim: _ServicePurgeShim):
        # backend.service guard'ит import mlx_whisper (engine.py: try/except → None),
        # поэтому import резистентен к ubuntu-no-mlx. На всякий случай — skip, если
        # импорт всё же не удался в нестандартном окружении (а не падаем с error).
        try:
            from backend.service import BackendService
        except Exception as exc:  # pragma: no cover - defensive (no-mlx edge)
            self.skipTest(f"backend.service import недоступен: {exc!r}")
        func = BackendService._handle_purge_all_data
        return lambda params: func(shim, params)

    def test_set_before_purge_clear_after(self) -> None:
        ab = _StubAutoBackup()
        hist = _StubHistory()
        hist.order_ref = ab.order
        shim = _ServicePurgeShim(hist, ab)
        handler = self._bound_handler(shim)

        out = handler({})
        self.assertEqual(out, {"ok": True, "purged": True})
        self.assertEqual(hist.called, 1)
        self.assertEqual(ab.set_calls, 1)
        self.assertEqual(ab.clear_calls, 1)
        # Порядок: set → purge → clear.
        self.assertEqual(ab.order, ["set", "purge", "clear"])

    def test_clear_purged_called_even_on_exception(self) -> None:
        """Если purge-тело бросает — clear_purged всё равно вызывается (finally)."""
        ab = _StubAutoBackup()
        hist = _StubHistory(raise_exc=True)
        shim = _ServicePurgeShim(hist, ab)
        handler = self._bound_handler(shim)

        with self.assertRaises(RuntimeError):
            handler({})
        self.assertEqual(ab.set_calls, 1)
        self.assertEqual(ab.clear_calls, 1)  # снят несмотря на исключение


if __name__ == "__main__":
    unittest.main()
