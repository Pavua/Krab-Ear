"""Sentry KRAB-EAR-BACKEND-1V storm follow-up (2026-08-09) — root-cause fix.

The diagnostic added in test_state_store_lock_slow_diagnostics_2026_08_09.py
only *reports* a stuck lock holder; it does not stop the cascade. The actual
storm mechanism ("IPC: лимит 64 коннектов исчерпан") is that
`fcntl.flock(LOCK_EX)` blocks with NO timeout, so a single thread stuck
WHILE HOLDING the lock (e.g. abandoned by the 180s IPC backstop but still
blocked inside a syscall) freezes every other IPC method forever — each new
request piles up a worker thread + connection slot that the 180s backstop
can't reclaim fast enough (see project_sentry_sweep_2026-08-05_ping_lock_contention.md).

This bounds LOCK ACQUISITION only — never the hold itself. A legitimate
long-running writer (e.g. migrate_history_encryption re-encrypting the whole
history under `with self._lock():`) is completely unaffected: it keeps the
lock for as long as it needs. What changes is what happens to OTHER callers
who show up while it's held — they now get a clear, fast
`StateStoreLockTimeout` instead of hanging until the 180s backstop abandons
them and leaks a thread/fd forever.
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

from backend.ipc_errors import IpcOperationalError  # noqa: E402
from backend.state_store import StateStore, StateStoreLockTimeout  # noqa: E402


def _make_store(tmp_dir: str, **kwargs) -> StateStore:
    return StateStore(Path(tmp_dir) / "data", **kwargs)


class TestBoundedAcquireRaisesOnStuckHolder(unittest.TestCase):
    """A waiter must give up (raise) near the configured timeout, not hang forever."""

    def test_bounded_acquire_raises_after_timeout_when_holder_stuck(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=0.3)

            acquired = threading.Event()
            release_holder = threading.Event()

            def stuck_holder():
                with store._lock():
                    acquired.set()
                    # Hold well past the waiter's timeout.
                    release_holder.wait(timeout=5)

            holder_thread = threading.Thread(target=stuck_holder, name="stuck-holder")
            holder_thread.start()
            self.assertTrue(acquired.wait(timeout=5))

            start = time.monotonic()
            with self.assertRaises(StateStoreLockTimeout):
                with store._lock():
                    pass  # pragma: no cover — must never be reached
            elapsed = time.monotonic() - start

            release_holder.set()
            holder_thread.join(timeout=5)

            # Must give up close to the configured 0.3s deadline, not hang
            # for the holder's full ~5s hold.
            self.assertLess(elapsed, 2.0, "waiter blocked far longer than its configured timeout")
            self.assertGreaterEqual(elapsed, 0.25, "waiter gave up suspiciously before its deadline")

    def test_timeout_is_ipc_operational_error_subclass(self):
        """A lock timeout must stay LOUD (internal_error + Sentry) in handle_request,
        not get silently downgraded to invalid_request — see the IPC dispatch
        error contract documented in CLAUDE.md / backend/ipc_errors.py."""
        self.assertTrue(issubclass(StateStoreLockTimeout, IpcOperationalError))
        self.assertTrue(issubclass(StateStoreLockTimeout, RuntimeError))


class TestLockStateRecoversCleanlyAfterTimeout(unittest.TestCase):
    """A timed-out waiter must leave zero trace in the depth/fileobj bookkeeping."""

    def test_lock_state_recovers_cleanly_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=0.3)

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
                with store._lock():
                    pass  # pragma: no cover

            # The timed-out thread must not have leaked depth/fileobj bookkeeping.
            tid = threading.get_ident()
            self.assertNotIn(tid, store._lock_depth)
            self.assertNotIn(tid, store._lock_fileobj)

            release_holder.set()
            holder_thread.join(timeout=5)

            # A fresh acquire after the real holder releases must work normally.
            item = store.add_history_item("after timeout recovery")
            self.assertIsNotNone(item.id)


class TestDefaultTimeoutDoesNotAffectNormalUsage(unittest.TestCase):
    """The default (generous) timeout must never fire during normal fast usage."""

    def test_default_timeout_does_not_affect_normal_fast_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp)  # default lock_acquire_timeout_sec
            for i in range(20):
                store.add_history_item(f"item-{i}")
            active = store._load_active_items_with_lock()
            self.assertEqual(len(active), 20)


class TestReentrantCallIgnoresAcquireTimeout(unittest.TestCase):
    """A thread already holding the lock must never hit the bounded-acquire path
    on a nested (reentrant) call, even with a near-zero configured timeout."""

    def test_reentrant_call_ignores_timeout_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = _make_store(tmp, lock_acquire_timeout_sec=0.001)

            with store._lock():
                # Nested call from the SAME thread — must be instant, no retry loop.
                with store._lock():
                    pass


if __name__ == "__main__":
    unittest.main()
