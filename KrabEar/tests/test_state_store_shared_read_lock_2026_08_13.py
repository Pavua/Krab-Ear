"""Регрессионные тесты — StateStore._lock(shared=True) (спека 2026-08-13).

Живой отказ 12:07: ``_load_active_items_with_lock`` держал ЭКСКЛЮЗИВНЫЙ flock
10.2с на 12 600 записях владельца — за это время каждый читатель настроек
(privacy-гейт вызывает ``load_settings`` почти из каждого IPC-хендлера) стоял
в очереди, хотя обе стороны — ЧИСТЫЕ ЧТЕНИЯ. Решение: точечный
``shared=True`` (``fcntl.LOCK_SH``) для ``_load_active_items_with_lock`` и
``load_settings`` — несколько читателей держат лок одновременно, писатели
(``save_settings`` и ~50 прочих call sites) остаются ``LOCK_EX`` без изменений.

Главная опасность (см. §3 спеки): реентерабельность ``_lock()`` по треду
(depth-counter, фикс #1872) обязана помнить РЕЖИМ. Вложенный EXCLUSIVE-запрос
поверх удерживаемого SHARED — атомарный SH→EX апгрейд, которого POSIX flock
не даёт; должен ГРОМКО падать ``StateStoreLockUpgradeError``, а не молча
продолжать работу под разделяемым локом (что было бы тише и хуже дедлока).

Покрытие:
- default (``shared=False``) поведение побайтово прежнее — ``LOCK_EX``.
- ``shared=True`` реально берёт ``LOCK_SH``.
- Два shared-читателя (разные instance/fd, тот же процесс) держат лок
  одновременно — сериализация ушла.
- Два shared-читателя из РАЗНЫХ ОС-процессов держат лок одновременно
  (реальный ``fcntl``, НЕ мок) — прямое доказательство межпроцессной
  совместимости SH-SH.
- Писатель (``LOCK_EX``) ждёт shared-читателей и наоборот.
- Вложенный exclusive поверх held shared — ГРОМКОЕ исключение,
  depth/mode-счётчики корректно откатываются, лок реально освобождается.
- Вложенный shared поверх held shared/exclusive — безопасный no-op.
- ``migrate_history_encryption`` (эксклюзив снаружи) → ``progress_cb`` →
  ``load_settings()`` (теперь shared изнутри) — НЕ ломается (ровно тот путь,
  ради которого делали реентерабельность, см. §3 спеки).
"""

from __future__ import annotations

import fcntl
import multiprocessing
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.state_store import StateStore, StateStoreLockUpgradeError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(data_dir: Path) -> StateStore:
    return StateStore(data_dir)


def _write_plaintext_line(history_path: Path, item_id: str, text: str) -> None:
    import json
    payload = {
        "id": item_id,
        "ts": "2026-01-01T00:00:00Z",
        "text": text,
        "confidence": 0.9,
        "duration": 5.0,
    }
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _shared_reader_process_worker(data_dir_str: str, acquired_event, release_event) -> None:
    """Module-level target для multiprocessing (нужен top-level для
    picklability под 'spawn'; тест явно использует fork-контекст, где это
    не обязательно, но top-level держим ради переносимости/документируемости).
    """
    store = StateStore(Path(data_dir_str))
    with store._lock(shared=True):
        acquired_event.set()
        release_event.wait(timeout=10.0)


# ---------------------------------------------------------------------------
# 1. Default behavior byte-identical; shared=True actually takes LOCK_SH
# ---------------------------------------------------------------------------

class TestDefaultLockFlagsUnchanged(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_default_shared_false_uses_lock_ex(self):
        store = _make_store(self.data_dir)
        captured_flags: list[int] = []
        real_flock = fcntl.flock

        def spy_flock(fd, flags):
            captured_flags.append(flags)
            return real_flock(fd, flags)

        with patch("backend.state_store.fcntl.flock", side_effect=spy_flock):
            with store._lock():
                pass

        # captured_flags[0] — попытка захвата (LOCK_EX|LOCK_NB); последующий
        # элемент (если есть) — LOCK_UN при освобождении, не относится к
        # проверке "какой режим захвата запрошен".
        self.assertGreaterEqual(len(captured_flags), 1)
        self.assertEqual(captured_flags[0], fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_shared_true_uses_lock_sh(self):
        store = _make_store(self.data_dir)
        captured_flags: list[int] = []
        real_flock = fcntl.flock

        def spy_flock(fd, flags):
            captured_flags.append(flags)
            return real_flock(fd, flags)

        with patch("backend.state_store.fcntl.flock", side_effect=spy_flock):
            with store._lock(shared=True):
                pass

        self.assertGreaterEqual(len(captured_flags), 1)
        self.assertEqual(captured_flags[0], fcntl.LOCK_SH | fcntl.LOCK_NB)


# ---------------------------------------------------------------------------
# 2. Two shared readers overlap within the same process (different fds)
# ---------------------------------------------------------------------------

class TestTwoSharedReadersOverlapSameProcess(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_two_shared_readers_hold_lock_concurrently(self):
        store_a = _make_store(self.data_dir)
        store_b = _make_store(self.data_dir)

        a_acquired = threading.Event()
        b_acquired = threading.Event()
        release_both = threading.Event()

        def reader(store: StateStore, acquired_evt: threading.Event) -> None:
            with store._lock(shared=True):
                acquired_evt.set()
                release_both.wait(timeout=5.0)

        t_a = threading.Thread(target=reader, args=(store_a, a_acquired), daemon=True)
        t_b = threading.Thread(target=reader, args=(store_b, b_acquired), daemon=True)
        t_a.start()
        t_b.start()

        self.assertTrue(a_acquired.wait(timeout=5.0), "читатель A не взял shared-лок")
        self.assertTrue(
            b_acquired.wait(timeout=5.0),
            "читатель B не смог войти, пока A ещё держит shared-лок — "
            "сериализация чистых читателей не ушла",
        )

        release_both.set()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)
        self.assertFalse(t_a.is_alive())
        self.assertFalse(t_b.is_alive())


# ---------------------------------------------------------------------------
# 3. Two REAL OS PROCESSES hold the shared lock concurrently — real fcntl,
#    not a mock (DoD explicit requirement).
# ---------------------------------------------------------------------------

class TestTwoRealProcessesHoldSharedLockConcurrently(unittest.TestCase):
    """Тред-версия выше (п.2) уже доказывает межэкземплярную совместимость
    SH-SH дешевле, но DoD дословно требует именно РАЗНЫХ ПРОЦЕССОВ. fcntl.flock
    физически привязан к open file description процесса, поэтому только
    настоящий межпроцессный тест доказывает то, что нужно доказать: два
    независимых процесса ОС одновременно держат один и тот же файловый лок."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)
        # StateStore.__init__ создаёт data_dir + touch()ает все файлы — делаем
        # это один раз в родителе, чтобы оба child-процесса не гонялись за
        # mkdir/touch одного и того же пути.
        _make_store(self.data_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_two_processes_shared_lock_overlap(self):
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("fork start method недоступен на этой платформе")

        ctx = multiprocessing.get_context("fork")
        acquired_a = ctx.Event()
        acquired_b = ctx.Event()
        release_all = ctx.Event()

        p_a = ctx.Process(
            target=_shared_reader_process_worker,
            args=(str(self.data_dir), acquired_a, release_all),
        )
        p_b = ctx.Process(
            target=_shared_reader_process_worker,
            args=(str(self.data_dir), acquired_b, release_all),
        )

        p_a.start()
        p_b.start()
        try:
            got_a = acquired_a.wait(timeout=10.0)
            # Пока процесс A ещё держит shared-лок (заблокирован на
            # release_all.wait() ВНУТРИ with self._lock(shared=True)),
            # процесс B обязан суметь взять СВОЙ shared-лок на том же файле
            # без ожидания — иначе LOCK_SH-LOCK_SH совместимость не работает
            # межпроцессно.
            got_b = acquired_b.wait(timeout=10.0)
        finally:
            release_all.set()
            p_a.join(timeout=10.0)
            p_b.join(timeout=10.0)

        self.assertTrue(got_a, "процесс A не взял shared-лок вовремя")
        self.assertTrue(
            got_b,
            "процесс B не смог взять shared-лок, пока процесс A ещё держит "
            "свой — межпроцессная SH-SH совместимость сломана",
        )
        self.assertEqual(p_a.exitcode, 0, "процесс A завершился с ошибкой")
        self.assertEqual(p_b.exitcode, 0, "процесс B завершился с ошибкой")


# ---------------------------------------------------------------------------
# 4. Writer waits for shared readers, and shared readers wait for a writer.
# ---------------------------------------------------------------------------

class TestWriterWaitsForSharedReaders(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_exclusive_waits_for_shared_holder(self):
        store_a = _make_store(self.data_dir)
        store_b = _make_store(self.data_dir)

        reader_acquired = threading.Event()
        release_reader = threading.Event()
        timestamps: dict[str, float] = {}

        def hold_shared():
            with store_a._lock(shared=True):
                timestamps["reader_enter"] = time.monotonic()
                reader_acquired.set()
                release_reader.wait(timeout=5.0)
                timestamps["reader_exit"] = time.monotonic()

        def do_write():
            reader_acquired.wait(timeout=5.0)
            with store_b._lock():  # эксклюзив — как save_settings
                timestamps["writer_enter"] = time.monotonic()

        t_r = threading.Thread(target=hold_shared, daemon=True)
        t_w = threading.Thread(target=do_write, daemon=True)

        t_r.start()
        reader_acquired.wait(timeout=5.0)
        t_w.start()

        time.sleep(0.15)
        self.assertNotIn(
            "writer_enter", timestamps,
            "писатель вошёл в критическую секцию, пока читатель ещё держит "
            "shared-лок — писатель обязан ждать всех читателей",
        )

        release_reader.set()
        t_r.join(timeout=5.0)
        t_w.join(timeout=5.0)

        self.assertFalse(t_r.is_alive())
        self.assertFalse(t_w.is_alive())
        self.assertIn("writer_enter", timestamps)
        self.assertIn("reader_exit", timestamps)
        self.assertGreaterEqual(
            timestamps["writer_enter"], timestamps["reader_exit"],
            "писатель вошёл ДО того, как читатель освободил shared-лок",
        )


class TestSharedReaderWaitsForWriter(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_shared_reader_waits_for_exclusive_holder(self):
        store_a = _make_store(self.data_dir)
        store_b = _make_store(self.data_dir)

        writer_acquired = threading.Event()
        release_writer = threading.Event()
        timestamps: dict[str, float] = {}

        def hold_exclusive():
            with store_a._lock():
                timestamps["writer_enter"] = time.monotonic()
                writer_acquired.set()
                release_writer.wait(timeout=5.0)
                timestamps["writer_exit"] = time.monotonic()

        def do_shared_read():
            writer_acquired.wait(timeout=5.0)
            with store_b._lock(shared=True):
                timestamps["reader_enter"] = time.monotonic()

        t_w = threading.Thread(target=hold_exclusive, daemon=True)
        t_r = threading.Thread(target=do_shared_read, daemon=True)

        t_w.start()
        writer_acquired.wait(timeout=5.0)
        t_r.start()

        time.sleep(0.15)
        self.assertNotIn(
            "reader_enter", timestamps,
            "читатель вошёл в критическую секцию, пока писатель ещё держит "
            "эксклюзивный лок — читатель обязан ждать писателя",
        )

        release_writer.set()
        t_w.join(timeout=5.0)
        t_r.join(timeout=5.0)

        self.assertFalse(t_w.is_alive())
        self.assertFalse(t_r.is_alive())
        self.assertIn("reader_enter", timestamps)
        self.assertIn("writer_exit", timestamps)
        self.assertGreaterEqual(
            timestamps["reader_enter"], timestamps["writer_exit"],
            "читатель вошёл ДО того, как писатель освободил эксклюзивный лок",
        )


# ---------------------------------------------------------------------------
# 5. Nested exclusive under held shared — MUST raise loudly, never silently
#    continue under the weaker lock.
# ---------------------------------------------------------------------------

class TestNestedExclusiveUnderSharedRaises(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_nested_exclusive_under_shared_raises_before_body_runs(self):
        store = _make_store(self.data_dir)
        body_ran = False

        with self.assertRaises(StateStoreLockUpgradeError):
            with store._lock(shared=True):
                with store._lock():  # default shared=False — незаконный апгрейд
                    body_ran = True  # pragma: no cover — не должно выполниться

        self.assertFalse(body_ran, "тело вложенного exclusive не должно было выполниться")

    def test_state_fully_cleaned_up_after_upgrade_error(self):
        store = _make_store(self.data_dir)

        with self.assertRaises(StateStoreLockUpgradeError):
            with store._lock(shared=True):
                with store._lock():
                    pass

        # Внешний shared-with корректно откатился через свой finally —
        # никакого утёкшего per-thread state.
        self.assertEqual(len(store._lock_depth), 0, store._lock_depth)
        self.assertEqual(len(store._lock_fileobj), 0, store._lock_fileobj)
        self.assertEqual(len(store._lock_mode), 0, store._lock_mode)

    def test_lock_genuinely_free_after_upgrade_error(self):
        """После исключения лок обязан быть РЕАЛЬНО свободен — другой тред
        должен взять его немедленно (не самозаклин, не утечка fd)."""
        store = _make_store(self.data_dir)

        with self.assertRaises(StateStoreLockUpgradeError):
            with store._lock(shared=True):
                with store._lock():
                    pass

        acquired = threading.Event()

        def _run():
            with store._lock():
                acquired.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertTrue(acquired.is_set(), "лок не освободился после апгрейд-исключения")

    def test_outer_shared_lock_still_visible_to_other_readers_before_the_raise(self):
        """Пока внешний shared удерживается (до вложенного незаконного
        апгрейда), другой ПОТОК всё ещё должен суметь взять shared-лок —
        подтверждает, что апгрейд-проверка срабатывает ДО инкремента
        depth-счётчика и не портит состояние, которое видят другие треды."""
        store_a = _make_store(self.data_dir)
        store_b = _make_store(self.data_dir)

        outer_acquired = threading.Event()
        proceed_to_upgrade = threading.Event()
        other_reader_acquired = threading.Event()
        upgrade_error: list[BaseException] = []

        def outer_holder():
            with store_a._lock(shared=True):
                outer_acquired.set()
                proceed_to_upgrade.wait(timeout=5.0)
                try:
                    with store_a._lock():
                        pass
                except StateStoreLockUpgradeError as exc:
                    upgrade_error.append(exc)

        def other_reader():
            outer_acquired.wait(timeout=5.0)
            with store_b._lock(shared=True):
                other_reader_acquired.set()

        t_outer = threading.Thread(target=outer_holder, daemon=True)
        t_outer.start()
        outer_acquired.wait(timeout=5.0)

        t_other = threading.Thread(target=other_reader, daemon=True)
        t_other.start()
        self.assertTrue(
            other_reader_acquired.wait(timeout=5.0),
            "другой поток не смог взять shared-лок, пока первый держит свой",
        )

        proceed_to_upgrade.set()
        t_outer.join(timeout=5.0)
        t_other.join(timeout=5.0)

        self.assertEqual(len(upgrade_error), 1)
        self.assertIsInstance(upgrade_error[0], StateStoreLockUpgradeError)


# ---------------------------------------------------------------------------
# 6. Safe nested combinations — no exception, no upgrade.
# ---------------------------------------------------------------------------

class TestSafeNestedCombinationsDoNotRaise(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_nested_shared_under_shared_is_noop(self):
        store = _make_store(self.data_dir)
        with store._lock(shared=True):
            with store._lock(shared=True):
                pass  # не должно упасть

    def test_nested_shared_under_exclusive_is_noop(self):
        """Зеркало реального продакшен-пути: migrate_history_encryption
        держит эксклюзив снаружи, load_settings() теперь просит shared
        изнутри — это НЕ апгрейд (эксклюзив уже строже shared), безопасный
        no-op, реальный flock не трогается."""
        store = _make_store(self.data_dir)
        with store._lock():  # эксклюзив (default)
            with store._lock(shared=True):
                pass  # не должно упасть


# ---------------------------------------------------------------------------
# 7. migrate_history_encryption (exclusive) -> progress_cb -> load_settings
#    (now shared) must still work — the exact chain reentrancy was built for.
# ---------------------------------------------------------------------------

class TestMigrateHistoryEncryptionWithSharedLoadSettings(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_progress_cb_calling_shared_load_settings_does_not_deadlock_or_raise(self):
        import os
        from backend.history_crypto import HistoryCrypto

        store = _make_store(self.data_dir)
        store._history_crypto_initialized = True
        store._history_crypto_instance = HistoryCrypto(os.urandom(32))

        for i in range(5):
            _write_plaintext_line(store.history_path, f"item-{i}", f"text {i}")

        calls: list[str] = []
        errors: list[BaseException] = []

        def progress_cb(total, done, encrypted, pct, status):
            calls.append(status)
            try:
                settings = store.load_settings()  # теперь shared=True изнутри
                self.assertIsInstance(settings, dict)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        result_holder: dict = {}

        def _run():
            result_holder["result"] = store.migrate_history_encryption(progress_cb=progress_cb)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10.0)

        self.assertFalse(t.is_alive(), "migrate_history_encryption завис")
        self.assertIn("result", result_holder)
        self.assertTrue(result_holder["result"]["ok"], result_holder["result"])
        self.assertGreater(len(calls), 0, "progress_cb ни разу не был вызван")
        self.assertFalse(
            errors,
            f"load_settings() внутри progress_cb упал с ошибкой: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
