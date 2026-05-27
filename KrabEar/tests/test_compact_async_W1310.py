"""Tests for StateStore.compact_async / maybe_compact_async (W1302 F2 MED).

Covers:
- compact_async returns immediately (does not block caller)
- compact_async eventually compacts the store in the background
- maybe_compact_async returns immediately and completes
- sync compact still works for IPC callers
- JobTracker integration (job_id returned, status transitions)
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

from backend.state_store import StateStore  # noqa: E402
from backend.job_tracker import JobTracker  # noqa: E402


def _make_store(tmp_dir: Path, threshold: int = 1) -> StateStore:
    """Helper: create a StateStore with a tiny compact threshold."""
    store = StateStore(tmp_dir, compact_threshold_bytes=threshold)
    return store


def _populate(store: StateStore, n: int = 5) -> None:
    """Add n items and delete half to create tombstones."""
    ids = []
    for i in range(n):
        item = store.add_history_item(text=f"item-{i}")
        ids.append(item.id)
    # Delete half to produce tombstone debt.
    for item_id in ids[: n // 2]:
        store.delete_history_item(item_id)


class TestCompactAsyncReturnsImmediately(unittest.TestCase):
    """compact_async / maybe_compact_async must not block the calling thread."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self.tmp.name))
        _populate(self.store, n=10)
        self._bg_thread: "threading.Thread | None" = None

    def tearDown(self) -> None:
        # Wait for any background thread spawned by this test before cleaning up.
        if self._bg_thread is not None:
            self._bg_thread.join(timeout=3.0)
        self.tmp.cleanup()

    def test_compact_async_returns_immediately(self) -> None:
        """compact_async() should return before the background thread finishes."""
        # We inject a blocking version of compact_with_stats to measure timing.
        started = threading.Event()
        released = threading.Event()
        original = self.store.compact_with_stats

        def slow_compact():
            started.set()
            released.wait(timeout=2.0)
            return original()

        self.store.compact_with_stats = slow_compact  # type: ignore[method-assign]

        t0 = time.monotonic()
        self.store.compact_async()
        elapsed = time.monotonic() - t0

        # Should return in well under 100 ms (no file I/O on caller thread).
        self.assertLess(elapsed, 0.1, "compact_async blocked the caller")

        # Let the background thread proceed and finish so tearDown can clean up.
        released.set()
        started.wait(timeout=2.0)
        # Collect the daemon thread by name so tearDown can join it.
        for t in threading.enumerate():
            if t.name == "StateStore-compact-async-full":
                self._bg_thread = t
                break

    def test_maybe_compact_async_returns_immediately(self) -> None:
        """maybe_compact_async() should return immediately when threshold exceeded."""
        started = threading.Event()
        released = threading.Event()
        original_mc = self.store.maybe_compact

        def slow_maybe_compact():
            started.set()
            released.wait(timeout=2.0)
            return original_mc()

        self.store.maybe_compact = slow_maybe_compact  # type: ignore[method-assign]

        t0 = time.monotonic()
        self.store.maybe_compact_async()
        elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 0.1, "maybe_compact_async blocked the caller")

        released.set()
        started.wait(timeout=2.0)
        # Collect thread for clean tearDown.
        for t in threading.enumerate():
            if t.name == "StateStore-compact-async":
                self._bg_thread = t
                break


class TestCompactAsyncEventuallyCompletes(unittest.TestCase):
    """compact_async / maybe_compact_async must actually compact the data."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self.tmp.name))
        _populate(self.store, n=10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _wait_tombstones_empty(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            stats = self.store.get_history_stats()
            if stats["tombstones_lines"] == 0:
                return True
            time.sleep(0.05)
        return False

    def test_compact_async_eventually_completes(self) -> None:
        """After compact_async, tombstones file should eventually be empty."""
        before = self.store.get_history_stats()
        self.assertGreater(before["tombstones_lines"], 0, "Need tombstones for this test")

        self.store.compact_async()

        compacted = self._wait_tombstones_empty(timeout=3.0)
        self.assertTrue(compacted, "compact_async did not complete within 3 s")

        after = self.store.get_history_stats()
        self.assertEqual(after["tombstones_lines"], 0)

    def test_maybe_compact_async_eventually_compacts(self) -> None:
        """maybe_compact_async should compact when threshold exceeded."""
        before = self.store.get_history_stats()
        self.assertGreater(before["tombstones_lines"], 0, "Need tombstones for this test")

        self.store.maybe_compact_async()

        compacted = self._wait_tombstones_empty(timeout=3.0)
        self.assertTrue(compacted, "maybe_compact_async did not complete within 3 s")

    def test_maybe_compact_async_skips_below_threshold(self) -> None:
        """maybe_compact_async returns None and does NOT start thread when below threshold."""
        big_store = StateStore(
            Path(self.tmp.name) / "big",
            compact_threshold_bytes=100 * 1024 * 1024,
        )
        _populate(big_store, n=3)

        result = big_store.maybe_compact_async()
        self.assertIsNone(result, "Should return None when below threshold")


class TestSyncCompactStillWorksForIPC(unittest.TestCase):
    """IPC callers use synchronous compact / compact_with_stats — must still work."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self.tmp.name))
        _populate(self.store, n=10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sync_compact_still_works_for_ipc(self) -> None:
        """compact() / compact_with_stats() should still work synchronously."""
        before = self.store.get_history_stats()
        self.assertGreater(before["tombstones_lines"], 0)

        stats = self.store.compact_with_stats()

        self.assertIn("reclaimed_bytes", stats)
        after = self.store.get_history_stats()
        self.assertEqual(after["tombstones_lines"], 0, "Sync compact did not clear tombstones")

    def test_compact_returns_true(self) -> None:
        result = self.store.compact()
        self.assertTrue(result)

    def test_maybe_compact_synchronous_still_works(self) -> None:
        """maybe_compact() should still work synchronously."""
        triggered = self.store.maybe_compact()
        self.assertTrue(triggered)
        after = self.store.get_history_stats()
        self.assertEqual(after["tombstones_lines"], 0)


class TestCompactAsyncJobTracker(unittest.TestCase):
    """compact_async / maybe_compact_async integrate with JobTracker."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _make_store(Path(self.tmp.name))
        _populate(self.store, n=6)
        self.tracker = JobTracker()

    def tearDown(self) -> None:
        # Wait for any daemon compact threads to finish before cleaning up temp dir.
        for t in list(threading.enumerate()):
            if t.name in ("StateStore-compact-async", "StateStore-compact-async-full"):
                t.join(timeout=3.0)
        self.tmp.cleanup()

    def _wait_job_done(self, job_id: str, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.tracker.get(job_id)
            if job and job["status"] in ("done", "failed"):
                return True
            time.sleep(0.05)
        return False

    def test_compact_async_returns_job_id(self) -> None:
        """compact_async with a tracker should return a non-None job_id."""
        job_id = self.store.compact_async(job_tracker=self.tracker)
        self.assertIsNotNone(job_id)
        self.assertIsInstance(job_id, str)

    def test_compact_async_job_reaches_done(self) -> None:
        """compact_async job should eventually reach 'done' status."""
        job_id = self.store.compact_async(job_tracker=self.tracker)
        assert job_id is not None

        finished = self._wait_job_done(job_id, timeout=3.0)
        self.assertTrue(finished, "Job did not reach done/failed within 3 s")

        job = self.tracker.get(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "done")

    def test_maybe_compact_async_returns_job_id_when_threshold_exceeded(self) -> None:
        """maybe_compact_async with tracker returns job_id when threshold exceeded."""
        job_id = self.store.maybe_compact_async(job_tracker=self.tracker)
        self.assertIsNotNone(job_id)

        finished = self._wait_job_done(job_id, timeout=3.0)
        self.assertTrue(finished, "Job did not finish within 3 s")

    def test_maybe_compact_async_no_job_when_below_threshold(self) -> None:
        """maybe_compact_async returns None (no job) when file is below threshold."""
        big_store = StateStore(
            Path(self.tmp.name) / "big",
            compact_threshold_bytes=100 * 1024 * 1024,
        )
        _populate(big_store, n=2)

        job_id = big_store.maybe_compact_async(job_tracker=self.tracker)
        self.assertIsNone(job_id)

    def test_compact_async_without_tracker_returns_none(self) -> None:
        """compact_async without tracker returns None."""
        job_id = self.store.compact_async(job_tracker=None)
        self.assertIsNone(job_id)


if __name__ == "__main__":
    unittest.main()
