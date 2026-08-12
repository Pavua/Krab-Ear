"""Спека 2026-08-12 ping-zero-wait — StateStore side.

Предыдущая волна (2026-08-12 ping-nonblocking, `0c10efbf`) дала `handle_ping`
короткий БЮДЖЕТ ожидания flock (`ping_count_lock_timeout_sec`, дефолт 0.3с).
Живой замер на проде под реальной контенцией (захват `history.lock` из
отдельного процесса, store владельца ~12 600 записей) показал: с дефолтными
бюджетами суммарное ожидание внутри `handle_ping` доходило до 2.04с — Swift
`main+HealthMonitor.swift:217` шлёт `ping` с таймаутом РОВНО 2с, два подряд
промаха → `forceRestartBackend` (`launchctl kickstart -k`). Бюджетный подход
(ждать ДО N секунд) в принципе не может дать жёсткую гарантию: сумма
НЕСКОЛЬКИХ попыток захвата на пути ping растёт вместе с числом попыток,
даже если каждая по отдельности мала.

Этот файл покрывает НОВЫЙ примитив на уровне `StateStore`: `nowait=True` —
РОВНО ОДНА неблокирующая попытка `flock(LOCK_EX | LOCK_NB)`, без единого
цикла опроса (`_LOCK_POLL_INTERVAL_SEC`) и без ожидания вообще. Лок занят →
немедленный `StateStoreLockTimeout`; лок свободен → обычный успех.

Не переиспользует `timeout_sec=0.0` напрямую как sentinel: на уровне
пользовательских НАСТРОЕК (`settings_read_lock_timeout_sec`,
`ping_count_lock_timeout_sec`) число `0` уже ИСТОРИЧЕСКИ означает
«read-path бюджет отключён, ждать сколько нужно инстансу» (см.
`SettingsService._read_lock_timeout_budget` /
`test_settings_service_read_lock_timeout_2026_08_12.py`) — эти два смысла
несовместимы, поэтому у `nowait` отдельный явный булев параметр вместо
перегрузки числа 0 ещё одним значением.

Покрывает:
- `_lock(nowait=True)` — мгновенный (не через один цикл `_LOCK_POLL_INTERVAL_SEC`)
  `StateStoreLockTimeout`, когда лок занят другим держателем;
- `nowait=True` НЕ зависит от `timeout_sec`/инстанс-таймаута (даже огромного);
- свободный лок — `nowait=True` не меняет результат обычного пути;
- реентерабельность (вложенный вызов той же нитью) не спотыкается о nowait;
- чистый откат bookkeeping (`_lock_depth`/`_lock_fileobj`) после nowait-таймаута;
- `count_active_items(nowait=True)` / `load_settings(nowait=True)` пробрасывают
  флаг в `_lock()` так же, как существующий `lock_timeout_sec=...`.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import (  # noqa: E402
    _LOCK_POLL_INTERVAL_SEC,
    StateStore,
    StateStoreLockTimeout,
)


def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


class TestLockNowaitRaisesInstantly(unittest.TestCase):
    """nowait=True must give up on the FIRST failed attempt, never sleep/poll."""

    def test_nowait_raises_without_a_single_poll_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Инстанс-таймаут намеренно ОГРОМНЫЙ — если бы nowait не работал
            # (деградировал до обычного ожидания), тест завис бы на 30с.
            store = _make_store(tmp, lock_acquire_timeout_sec=30.0)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                with store._lock(nowait=True):
                    pass  # pragma: no cover — must never be reached
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            # Меньше ОДНОГО цикла опроса — доказывает, что не было даже
            # единственного time.sleep(_LOCK_POLL_INTERVAL_SEC).
            self.assertLess(
                elapsed, _LOCK_POLL_INTERVAL_SEC,
                "nowait=True must give up on the very first attempt, "
                "without a single poll-sleep iteration",
            )

    def test_nowait_ignores_explicit_timeout_sec_and_still_fails_fast(self):
        """nowait=True побеждает даже явный большой timeout_sec (по контракту
        параметра — nowait форсирует effective_timeout=0.0 безусловно)."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                with store._lock(timeout_sec=30.0, nowait=True):
                    pass  # pragma: no cover
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            self.assertLess(elapsed, _LOCK_POLL_INTERVAL_SEC)


class TestLockNowaitFreeLockUnaffected(unittest.TestCase):
    """Свободный лок: nowait=True не меняет результат обычного пути."""

    def test_nowait_succeeds_normally_when_lock_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            with store._lock(nowait=True):
                pass  # no exception

    def test_nowait_count_active_items_matches_plain_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.add_history_item("one")
            store.add_history_item("two")

            plain = store.count_active_items()
            via_nowait = store.count_active_items(nowait=True)

            self.assertEqual(plain, 2)
            self.assertEqual(via_nowait, 2)

    def test_nowait_load_settings_matches_plain_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            store.save_settings({"quality_profile": "max"})

            plain = store.load_settings()
            via_nowait = store.load_settings(nowait=True)

            self.assertEqual(plain, via_nowait)
            self.assertEqual(via_nowait["quality_profile"], "max")


class TestLockNowaitReentrancy(unittest.TestCase):
    """A thread already holding the lock must never hit the nowait acquire
    path on a nested (reentrant) call — same depth-counter no-op as the
    budgeted timeout_sec override (see test_state_store_lock_acquire_timeout)."""

    def test_reentrant_nowait_call_is_instant_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)
            with store._lock():
                with store._lock(nowait=True):
                    pass  # must not raise, must not attempt a real flock


class TestLockNowaitStateRecoversCleanly(unittest.TestCase):
    """A nowait timeout must leave zero trace in the depth/fileobj bookkeeping
    (same invariant as the budgeted-timeout path, see
    test_state_store_lock_acquire_timeout_2026_08_09.py)."""

    def test_lock_state_recovers_cleanly_after_nowait_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=5)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            with self.assertRaises(StateStoreLockTimeout):
                with store._lock(nowait=True):
                    pass  # pragma: no cover

            tid = threading.get_ident()
            self.assertNotIn(tid, store._lock_depth)
            self.assertNotIn(tid, store._lock_fileobj)

            release_holder.set()
            holder_thread.join(timeout=5)

            # A fresh acquire after the real holder releases must work normally.
            item = store.add_history_item("after nowait timeout recovery")
            self.assertIsNotNone(item.id)


class TestCountActiveItemsAndLoadSettingsNowaitPropagation(unittest.TestCase):
    """count_active_items(nowait=True) / load_settings(nowait=True) must raise
    StateStoreLockTimeout INSTANTLY under contention — the flag must actually
    reach _lock(), not be silently swallowed."""

    def test_count_active_items_nowait_raises_instantly_under_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=30.0)
            store.add_history_item("one")

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                store.count_active_items(nowait=True)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            self.assertLess(elapsed, _LOCK_POLL_INTERVAL_SEC)

    def test_load_settings_nowait_raises_instantly_under_contention(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=30.0)
            store.save_settings({"quality_profile": "balanced"})

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                store.load_settings(nowait=True)
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            self.assertLess(elapsed, _LOCK_POLL_INTERVAL_SEC)


if __name__ == "__main__":
    unittest.main()
