"""Regression tests — StateStore._lock() per-thread reentrancy deadlock fix.

Корневая причина (диагноз, подтверждён faulthandler-дампом стеков):
``StateStore._lock()`` брал ``fcntl.flock`` на НОВОМ файловом дескрипторе
при КАЖДОМ входе. ``fcntl.flock`` — блокировка на уровне open file
description, не привязанная к треду. Если один и тот же ТРЕД дважды входил
в ``with self._lock():`` вложенно (второй раз — с нового fd), второй
``flock()`` блокировался навечно, ожидая освобождения лока, который держал
… тот же самый тред снаружи — самозаклин без исключения (flock не бросает,
просто блокирует поток).

Реальная цепочка в продакшене:
``migrate_history_encryption()`` держит ``with self._lock():`` на весь цикл
шифрования → синхронно зовёт ``progress_cb(...)`` → (в реальном коде —
``event_bus.emit`` → ``event_replay.record_event`` → ``_is_privacy_mode`` →
``settings_service.cached_settings()``) → ``store.load_settings()`` →
повторный ``with self._lock():`` на том же треде → самозаклин навсегда.
Лок физически держится вечно (finally первого ``with`` никогда не
выполняется — тред застрял внутри yield), а значит блокируется ВЕСЬ
StateStore для абсолютно всех остальных вызовов (главный IPC-тред, другие
сервисы).

Фикс: ``_lock()`` реентерабелен ПО ТРЕДУ (per-thread depth-counter) —
повторный вход с того же треда — no-op поверх уже взятого лока. Реальный
OS-level ``flock`` физически берётся/отпускается РОВНО ОДИН РАЗ — на самом
внешнем входе/выходе — так что кросс-тредовая/кросс-процессная
эксклюзивность (см. ``test_state_store_lock_invariants.py``) не меняется.
Архитектурный прецедент в этом же проекте: ``core/mlx_lock.py::mlx_lock()``
— тот же класс багов, решённый через ``RLock`` (реентерабельный).
"""

from __future__ import annotations

import os
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

from backend.state_store import StateStore  # noqa: E402
from backend.history_crypto import HistoryCrypto  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers (mirrors test_history_encryption_migration.py / test_state_store_lock_invariants.py)
# ---------------------------------------------------------------------------

def _make_store(data_dir: Path) -> StateStore:
    return StateStore(data_dir)


def _inject_crypto(store: StateStore) -> HistoryCrypto:
    """Подменяет крипто-инстанс в StateStore (обход Keychain, CI-safe)."""
    crypto = HistoryCrypto(os.urandom(32))
    store._history_crypto_initialized = True
    store._history_crypto_instance = crypto
    return crypto


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


# ---------------------------------------------------------------------------
# 1. Direct regression — exact production deadlock scenario
# ---------------------------------------------------------------------------

class TestProgressCbCallingLoadSettingsDoesNotDeadlock(unittest.TestCase):
    """migrate_history_encryption(progress_cb=...) where progress_cb itself
    calls store.load_settings() — the exact chain that used to self-deadlock
    (event_bus.emit -> event_replay.record_event -> _is_privacy_mode ->
    settings_service.cached_settings() -> store.load_settings(), all inside
    the outer `with self._lock():` migrate_history_encryption holds for the
    whole run).
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_progress_cb_calling_load_settings_does_not_deadlock(self):
        store = _make_store(self.data_dir)
        _inject_crypto(store)

        # Non-empty history.ndjson with plaintext lines so migration actually runs.
        for i in range(5):
            _write_plaintext_line(store.history_path, f"item-{i}", f"text {i}")

        calls: list[str] = []

        def progress_cb(total, done, encrypted, pct, status):
            calls.append(status)
            # This is the call that used to hang forever (self-deadlock on
            # the same thread re-entering StateStore._lock()).
            store.load_settings()

        result_holder: dict = {}

        def _run():
            result_holder["result"] = store.migrate_history_encryption(
                progress_cb=progress_cb
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=10.0)

        self.assertFalse(
            t.is_alive(),
            "migrate_history_encryption должен завершиться, не зависнуть "
            "(self-deadlock regression)",
        )
        self.assertIn("result", result_holder, "поток завис до записи результата")
        self.assertTrue(result_holder["result"]["ok"], result_holder["result"])
        self.assertGreater(len(calls), 0, "progress_cb ни разу не был вызван")


# ---------------------------------------------------------------------------
# 2. Basic reentrancy
# ---------------------------------------------------------------------------

class TestBasicLockReentrancy(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_nested_lock_same_thread_does_not_block(self):
        store = _make_store(self.data_dir)

        done = threading.Event()
        errors: list[BaseException] = []

        def _run():
            try:
                with store._lock():
                    with store._lock():
                        pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        finished = done.wait(timeout=5.0)

        self.assertTrue(finished, "вложенный with self._lock(): with self._lock(): завис")
        self.assertFalse(t.is_alive())
        self.assertFalse(errors, f"unexpected errors: {errors}")

    def test_triple_nested_lock_same_thread_does_not_block(self):
        store = _make_store(self.data_dir)

        done = threading.Event()
        errors: list[BaseException] = []

        def _run():
            try:
                with store._lock():
                    with store._lock():
                        with store._lock():
                            pass
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        finished = done.wait(timeout=5.0)

        self.assertTrue(finished, "тройной вложенный lock завис")
        self.assertFalse(t.is_alive())
        self.assertFalse(errors, f"unexpected errors: {errors}")


# ---------------------------------------------------------------------------
# 3. Exception inside nested lock correctly unwinds depth-counter
# ---------------------------------------------------------------------------

class TestNestedLockExceptionCleansUpDepth(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_exception_in_nested_lock_releases_depth_and_fileobj(self):
        store = _make_store(self.data_dir)

        with self.assertRaises(RuntimeError):
            with store._lock():
                with store._lock():
                    raise RuntimeError("deliberate error inside nested lock")

        # No leaked per-thread bookkeeping after unwind.
        self.assertEqual(len(store._lock_depth), 0, store._lock_depth)
        self.assertEqual(len(store._lock_fileobj), 0, store._lock_fileobj)

        # Lock must be genuinely free — a fresh acquire must succeed immediately.
        acquired = threading.Event()

        def _run():
            with store._lock():
                acquired.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertTrue(acquired.is_set(), "лок не освободился после исключения")

    def test_exception_in_outer_lock_only_releases_depth_and_fileobj(self):
        store = _make_store(self.data_dir)

        with self.assertRaises(ValueError):
            with store._lock():
                raise ValueError("deliberate error, no nesting")

        self.assertEqual(len(store._lock_depth), 0, store._lock_depth)
        self.assertEqual(len(store._lock_fileobj), 0, store._lock_fileobj)


# ---------------------------------------------------------------------------
# 4. Cross-thread exclusivity is preserved (the real flock semantics)
# ---------------------------------------------------------------------------

class TestCrossThreadExclusivityPreserved(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_thread_b_waits_for_thread_a_to_release(self):
        store = _make_store(self.data_dir)

        a_acquired = threading.Event()
        release_a = threading.Event()
        timestamps: dict[str, float] = {}

        def thread_a():
            with store._lock():
                timestamps["a_enter"] = time.monotonic()
                a_acquired.set()
                release_a.wait(timeout=5.0)
                timestamps["a_exit"] = time.monotonic()

        def thread_b():
            a_acquired.wait(timeout=5.0)
            with store._lock():
                timestamps["b_enter"] = time.monotonic()

        t_a = threading.Thread(target=thread_a, daemon=True)
        t_b = threading.Thread(target=thread_b, daemon=True)

        t_a.start()
        a_acquired.wait(timeout=5.0)
        t_b.start()

        # Give thread B a moment to attempt (and block on) the lock.
        time.sleep(0.15)
        self.assertNotIn(
            "b_enter", timestamps,
            "Thread B вошёл в критическую секцию, пока Thread A ещё держит "
            "лок — кросс-тредовая эксклюзивность сломана регрессией фикса",
        )

        release_a.set()
        t_a.join(timeout=5.0)
        t_b.join(timeout=5.0)

        self.assertFalse(t_a.is_alive())
        self.assertFalse(t_b.is_alive())
        self.assertIn("b_enter", timestamps)
        self.assertIn("a_exit", timestamps)
        self.assertGreaterEqual(
            timestamps["b_enter"], timestamps["a_exit"],
            "Thread B вошёл в критическую секцию ДО того, как Thread A "
            "освободил лок",
        )


# ---------------------------------------------------------------------------
# 5. Acquisition-phase failure rolls back the depth counter (HIGH finding —
#    Sonnet code-review + Fable adversarial-verify independently confirmed).
# ---------------------------------------------------------------------------

class TestAcquisitionFailureRollsBackDepth(unittest.TestCase):
    """If touch()/open()/flock() raises during the OUTER (first) entry into
    ``_lock()`` — e.g. ENOSPC/EMFILE/EACCES, all realistic on a project that
    ships its own ``DiskSpaceMonitor`` — the depth-counter increment that
    already happened at the top of ``_lock()`` must be rolled back.

    Before this fix, that increment was NOT covered by the try/finally (it
    started only at ``try: yield``), so an exception during acquisition left
    ``_lock_depth[tid] == 1`` forever with no matching ``_lock_fileobj[tid]``.
    Every SUBSEQUENT ``_lock()`` call from that same thread then silently
    believed it was a harmless reentrant no-op (``depth != 0`` →
    ``acquired_here = False``) and skipped ``fcntl.flock`` entirely — a
    silent, permanent bypass of mutual exclusion for that thread, worse than
    the loud deadlock the reentrancy fix itself was meant to cure (that one
    was noisy and self-healed via HealthMonitor; this one is silent and
    lives for the rest of the process's life).
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_acquisition_failure_propagates_and_depth_resets(self):
        store = _make_store(self.data_dir)

        with patch(
            "backend.state_store.fcntl.flock",
            side_effect=OSError(28, "simulated ENOSPC"),
        ):
            with self.assertRaises(OSError):
                with store._lock():
                    self.fail(
                        "тело with не должно выполниться — flock() падает "
                        "до yield"
                    )

        # No leaked per-thread bookkeeping after the failed acquisition —
        # this is exactly what the buggy code got wrong (depth stuck at 1).
        self.assertEqual(len(store._lock_depth), 0, store._lock_depth)
        self.assertEqual(len(store._lock_fileobj), 0, store._lock_fileobj)

    def test_lock_actually_acquired_after_prior_acquisition_failure(self):
        """After the failure above, the SAME thread's next ``_lock()`` call
        must take a REAL OS-level flock — proven by contending against a
        SECOND ``StateStore`` instance on the same path from another
        thread, which must block until the first releases (racing through
        instantly would prove the real flock was silently skipped)."""
        store = _make_store(self.data_dir)

        with patch(
            "backend.state_store.fcntl.flock",
            side_effect=OSError(28, "simulated ENOSPC"),
        ):
            with self.assertRaises(OSError):
                with store._lock():
                    pass

        self.assertEqual(len(store._lock_depth), 0, store._lock_depth)
        self.assertEqual(len(store._lock_fileobj), 0, store._lock_fileobj)

        # Real (unpatched) fcntl.flock from here on — same thread as the
        # failed acquisition above.
        store2 = _make_store(self.data_dir)  # second instance, same path

        b_entered = threading.Event()
        b_done = threading.Event()
        timestamps: dict[str, float] = {}

        def _thread_b():
            with store2._lock():
                timestamps["b_enter"] = time.monotonic()
                b_entered.set()
            b_done.set()

        with store._lock():
            tb = threading.Thread(target=_thread_b, daemon=True)
            tb.start()

            # Give thread B a real chance to attempt (and, if the fix is
            # correct, block on) the OS-level lock.
            time.sleep(0.2)
            self.assertFalse(
                b_entered.is_set(),
                "Второй StateStore-инстанс на том же пути вошёл в "
                "критическую секцию, пока первый ещё держит _lock() — "
                "значит реальный fcntl.flock молча пропущен (регрессия из "
                "HIGH-находки)",
            )
            timestamps["a_still_holding"] = time.monotonic()

        tb.join(timeout=5.0)
        self.assertTrue(b_done.is_set(), "Thread B так и не завершился")
        self.assertIn("b_enter", timestamps)
        self.assertGreaterEqual(
            timestamps["b_enter"], timestamps["a_still_holding"],
            "Thread B вошёл в критическую секцию до освобождения лока — "
            "эксклюзивность нарушена",
        )


if __name__ == "__main__":
    unittest.main()
