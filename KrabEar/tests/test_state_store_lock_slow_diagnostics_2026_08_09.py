"""Sentry KRAB-EAR-BACKEND-1V follow-up (2026-08-09).

`StateStore._lock()` is a single process-wide `fcntl.flock(LOCK_EX)` with NO
timeout, shared by ~50+ call sites. Investigation of the 06:26 CEST recidive
(see project_sentry_sweep_2026-08-05_ping_lock_contention.md) confirmed that
`get_memory_stats` itself does zero StateStore work — it hung purely because
EVERY `handle_request` reads the privacy gate via `cached_settings()` after
the handler returns, which blocks on this same lock whenever the 5s TTL cache
is stale. So any thread that gets stuck WHILE HOLDING `_lock()` (e.g. an
abandoned backstop-timeout worker still blocked inside the syscall) freezes
every unrelated IPC method, not just the one that originally stalled.

`flock(LOCK_EX)` blocks synchronously in C with no chance to log mid-wait, so
a waiter can only usefully report evidence at ONE point: right before it
calls the blocking syscall, by checking how long the CURRENT holder (if any)
has already held the lock. In a real storm, dozens of later-arriving
requests pile up over minutes — each one's pre-block check fires immediately
and names the still-stuck holder, which is exactly the missing "who's
holding it" signal previous sweeps could only get from a live py-spy dump
(unavailable to an autonomous session without the owner's password).

This is diagnostic-only: no timeout is added to flock() itself (that would
be the actual architectural fix, deferred to the owner per the memory note).
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.state_store import StateStore, _LOCK_SLOW_WARN_SEC  # noqa: E402

_STORE_LOGGER = "KrabEar.Backend.Store"


def _make_store(tmp_dir: str) -> StateStore:
    return StateStore(Path(tmp_dir) / "data")


class TestNoWarningDuringNormalFastUsage(unittest.TestCase):
    """Fast, uncontended lock use must never emit the slow-lock diagnostic."""

    def test_no_warning_during_normal_fast_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            root_logger = logging.getLogger(_STORE_LOGGER)
            records: list[logging.LogRecord] = []

            class _Collector(logging.Handler):
                def emit(self, record):
                    records.append(record)

            handler = _Collector()
            root_logger.addHandler(handler)
            try:
                for i in range(20):
                    store.add_history_item(f"fast-{i}")
                store._load_active_items_with_lock()
            finally:
                root_logger.removeHandler(handler)

            slow_lock_records = [
                r for r in records
                if r.levelno >= logging.WARNING and "flock" in r.getMessage()
            ]
            self.assertEqual(
                slow_lock_records, [],
                f"Unexpected slow-lock warnings during fast usage: {slow_lock_records}",
            )


class TestWarnsNewWaiterAboutAlreadyStuckHolder(unittest.TestCase):
    """A later caller must log the stuck holder's identity BEFORE blocking.

    This is the capability that would have named the culprit during the
    2026-08-09 06:26 CEST storm instead of requiring a live py-spy dump.
    """

    def test_warns_new_waiter_about_already_stuck_holder(self):
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
            self.assertTrue(acquired.wait(timeout=5), "Holder never acquired the lock")

            # Let the holder sit on the lock past the slow-warn threshold BEFORE
            # a new waiter shows up — mirrors dozens of later IPC requests
            # arriving while one thread is already wedged.
            time.sleep(_LOCK_SLOW_WARN_SEC + 0.5)

            with self.assertLogs(_STORE_LOGGER, level="WARNING") as cm:
                waiter_started = threading.Event()

                def new_waiter():
                    waiter_started.set()
                    with store._lock():
                        pass

                waiter_thread = threading.Thread(target=new_waiter, name="new-waiter")
                waiter_thread.start()
                self.assertTrue(waiter_started.wait(timeout=2))
                # Give the waiter a moment to run its pre-block check-and-log
                # (must fire immediately, well before the holder ever releases).
                time.sleep(0.3)
                release_holder.set()
                holder_thread.join(timeout=5)
                waiter_thread.join(timeout=5)

            joined_output = "\n".join(cm.output)
            self.assertIn("ждёт эксклюзивный flock", joined_output)
            self.assertIn("stuck_holder", joined_output)


class TestWarnsOnOwnSlowWaitAndOnHolderRelease(unittest.TestCase):
    """Both the acquiring waiter and the releasing holder log their own duration."""

    def test_warns_own_wait_on_acquire_and_holder_duration_on_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)

            acquired = threading.Event()
            release_holder = threading.Event()

            def slow_holder():
                with store._lock():
                    acquired.set()
                    release_holder.wait(timeout=10)

            with self.assertLogs(_STORE_LOGGER, level="WARNING") as cm:
                holder_thread = threading.Thread(target=slow_holder, name="slow_holder")
                holder_thread.start()
                self.assertTrue(acquired.wait(timeout=5))

                waiter_done = threading.Event()
                waiter_error: list[BaseException] = []

                def blocked_waiter():
                    try:
                        with store._lock():
                            pass
                    except BaseException as exc:  # noqa: BLE001
                        waiter_error.append(exc)
                    finally:
                        waiter_done.set()

                waiter_thread = threading.Thread(target=blocked_waiter, name="blocked_waiter")
                waiter_thread.start()
                # Give the waiter time to actually enter the blocking flock() call.
                time.sleep(0.2)

                # Hold well past the threshold, then release — this exercises
                # BOTH the holder's own "held for Ns" log on release AND the
                # waiter's "waited Ns" log once it finally acquires.
                time.sleep(_LOCK_SLOW_WARN_SEC + 0.5)
                release_holder.set()
                holder_thread.join(timeout=5)
                self.assertTrue(waiter_done.wait(timeout=5))
                waiter_thread.join(timeout=5)

            self.assertEqual(waiter_error, [])
            joined_output = "\n".join(cm.output)
            self.assertIn("держал эксклюзивный flock", joined_output)
            self.assertIn("ждал эксклюзивный flock", joined_output)


if __name__ == "__main__":
    unittest.main()
