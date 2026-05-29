"""W1542 regression tests — restore _ARCHIVE_LOCK_FILE + _PRUNE_CANCEL_EVENT_TTL.

Both constants were clobbered by the W1497 cherry-pick train.

Covered:
  - test_archive_lock_file_constant_present
  - test_prune_cancel_event_ttl_constant_present
  - test_concurrent_archive_serialized_by_lock
  - test_stuck_cancel_event_evicted_after_ttl
"""

from __future__ import annotations

import fcntl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.archive_manager import ArchiveManager, _ARCHIVE_LOCK_FILE  # noqa: E402
from backend.job_tracker import JobTracker, _PRUNE_CANCEL_EVENT_TTL  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fake store for ArchiveManager
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self._items: dict[str, dict[str, Any]] = {}
        self._deleted: set[str] = set()

    def add_item(self, item_id: str, text: str) -> None:
        self._items[item_id] = {"id": item_id, "text": text, "ts": "2026-01-01T00:00:00"}

    def get_history_item_by_id(self, item_id: str):
        if item_id in self._deleted:
            return None
        item = self._items.get(item_id)
        if item is None:
            return None
        # Return a simple object with to_dict
        class _Item:
            def __init__(self, d):
                self._d = d
                self.id = d["id"]
            def to_dict(self):
                return dict(self._d)
        return _Item(item)

    def delete_history_item(self, item_id: str) -> bool:
        if item_id in self._items:
            self._deleted.add(item_id)
            return True
        return False

    def add_history_item(self, text: str = "", **kwargs) -> None:
        pass

    def restore_history_item_raw(self, raw_dict: dict) -> str:
        item_id = raw_dict.get("id", "restored")
        self._items[item_id] = raw_dict
        return item_id


# ---------------------------------------------------------------------------
# 1. test_archive_lock_file_constant_present
# ---------------------------------------------------------------------------

class TestArchiveLockFileConstantPresent(unittest.TestCase):
    """_ARCHIVE_LOCK_FILE constant must be importable and have the right value."""

    def test_archive_lock_file_constant_present(self) -> None:
        """_ARCHIVE_LOCK_FILE is defined and is a non-empty string."""
        self.assertIsInstance(_ARCHIVE_LOCK_FILE, str)
        self.assertTrue(_ARCHIVE_LOCK_FILE, "_ARCHIVE_LOCK_FILE must be non-empty")

    def test_archive_lock_file_is_lock_extension(self) -> None:
        """_ARCHIVE_LOCK_FILE ends with .lock to distinguish it from data files."""
        self.assertTrue(
            _ARCHIVE_LOCK_FILE.endswith(".lock"),
            f"Expected .lock suffix, got {_ARCHIVE_LOCK_FILE!r}",
        )

    def test_manager_has_lock_path_attribute(self) -> None:
        """ArchiveManager._lock_path attribute is set in __init__."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)
            self.assertTrue(hasattr(mgr, "_lock_path"), "ArchiveManager must have _lock_path attr")
            self.assertIsInstance(mgr._lock_path, Path)

    def test_lock_path_uses_archive_lock_file_constant(self) -> None:
        """ArchiveManager._lock_path.name matches _ARCHIVE_LOCK_FILE constant."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)
            self.assertEqual(mgr._lock_path.name, _ARCHIVE_LOCK_FILE)

    def test_lock_file_created_on_init(self) -> None:
        """Lock file is created (touch) on ArchiveManager.__init__."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)
            self.assertTrue(mgr._lock_path.exists(), "Lock file must exist after __init__")


# ---------------------------------------------------------------------------
# 2. test_prune_cancel_event_ttl_constant_present
# ---------------------------------------------------------------------------

class TestPruneCancelEventTTLConstantPresent(unittest.TestCase):
    """_PRUNE_CANCEL_EVENT_TTL constant must be importable and positive."""

    def test_prune_cancel_event_ttl_constant_present(self) -> None:
        """_PRUNE_CANCEL_EVENT_TTL is defined and is a positive float."""
        self.assertIsInstance(_PRUNE_CANCEL_EVENT_TTL, (int, float))
        self.assertGreater(_PRUNE_CANCEL_EVENT_TTL, 0)

    def test_prune_cancel_event_ttl_at_least_one_hour(self) -> None:
        """_PRUNE_CANCEL_EVENT_TTL is at least 1 hour (3600 s)."""
        self.assertGreaterEqual(
            _PRUNE_CANCEL_EVENT_TTL,
            3600.0,
            "_PRUNE_CANCEL_EVENT_TTL must be >= 3600 s (1 hour)",
        )


# ---------------------------------------------------------------------------
# 3. test_concurrent_archive_serialized_by_lock
# ---------------------------------------------------------------------------

class TestConcurrentArchiveSerializedByLock(unittest.TestCase):
    """Concurrent archive_items calls are serialized by flock (no data corruption)."""

    def test_concurrent_archive_serialized_by_lock(self) -> None:
        """20 threads archiving different items — all records written, no corruption."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)

            num_items = 20
            for i in range(num_items):
                store.add_item(f"item-{i}", f"Запись {i}")

            errors: list[Exception] = []

            def archive_one(item_id: str) -> None:
                try:
                    mgr.archive_items(item_ids=[item_id])
                except Exception as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=archive_one, args=(f"item-{i}",))
                for i in range(num_items)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

            self.assertEqual(errors, [], f"Concurrent archive raised exceptions: {errors}")

            archived = mgr.list_archived(limit=500)
            self.assertEqual(
                len(archived),
                num_items,
                f"Expected {num_items} archived items, got {len(archived)}",
            )

    def test_append_acquires_lock_ex(self) -> None:
        """_append_ndjson acquires LOCK_EX on the sibling lock file."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)
            flock_ops: list[int] = []
            real_flock = fcntl.flock

            def tracking_flock(fd: int, op: int) -> None:
                flock_ops.append(op)
                return real_flock(fd, op)

            with patch("fcntl.flock", side_effect=tracking_flock):
                mgr._append_ndjson(mgr._archive_path, {"id": "t1", "text": "test"})

            self.assertIn(fcntl.LOCK_EX, flock_ops, "LOCK_EX must be acquired")
            self.assertIn(fcntl.LOCK_UN, flock_ops, "LOCK_UN must be released")

    def test_rewrite_acquires_lock_ex(self) -> None:
        """_rewrite_archive acquires LOCK_EX on the sibling lock file."""
        with tempfile.TemporaryDirectory() as tmp:
            store = _FakeStore(tmp)
            mgr = ArchiveManager(store)
            flock_ops: list[int] = []
            real_flock = fcntl.flock

            def tracking_flock(fd: int, op: int) -> None:
                flock_ops.append(op)
                return real_flock(fd, op)

            with patch("fcntl.flock", side_effect=tracking_flock):
                mgr._rewrite_archive([{"id": "r1", "text": "rewrite"}])

            self.assertIn(fcntl.LOCK_EX, flock_ops, "LOCK_EX must be acquired")
            self.assertIn(fcntl.LOCK_UN, flock_ops, "LOCK_UN must be released")


# ---------------------------------------------------------------------------
# 4. test_stuck_cancel_event_evicted_after_ttl
# ---------------------------------------------------------------------------

class TestStuckCancelEventEvictedAfterTTL(unittest.TestCase):
    """Cancel events older than _PRUNE_CANCEL_EVENT_TTL are evicted by prune()."""

    def test_stuck_cancel_event_evicted_after_ttl(self) -> None:
        """Cancel event with old timestamp is evicted when prune() runs."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.update(jid, status="running")
        tracker.cancel(jid)

        # Verify cancel event is registered
        self.assertIn(jid, tracker._cancel_events_ts)

        # Fake-age the cancel event timestamp beyond TTL
        tracker._cancel_events_ts[jid] = time.monotonic() - (_PRUNE_CANCEL_EVENT_TTL + 1.0)

        # Also fake-age the job to trigger terminal cleanup
        tracker.mark_done(jid, items=[], errors=[])
        tracker.update(jid, finished_at=time.monotonic() - 7200.0)

        tracker.prune(max_age_sec=1.0)

        # Both job and cancel event should be gone
        self.assertIsNone(tracker.get(jid), "Stale done job should be pruned")
        self.assertNotIn(jid, tracker._cancel_events, "Cancel event should be evicted")
        self.assertNotIn(jid, tracker._cancel_events_ts, "Cancel event ts should be evicted")

    def test_cancel_events_ts_populated_on_cancel(self) -> None:
        """cancel() populates _cancel_events_ts with current monotonic time."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        before = time.monotonic()
        tracker.cancel(jid)
        after = time.monotonic()

        self.assertIn(jid, tracker._cancel_events_ts)
        ts = tracker._cancel_events_ts[jid]
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_fresh_cancel_event_not_evicted(self) -> None:
        """A recently set cancel event is NOT evicted during prune()."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.update(jid, status="running")
        tracker.cancel(jid)

        # Do NOT fake-age the event — it should survive prune()
        tracker.prune(max_age_sec=3600.0)

        # Job is still running (not terminal) and cancel event should be there
        self.assertIsNotNone(tracker.get(jid))
        self.assertIn(jid, tracker._cancel_events)

    def test_prune_returns_int_count(self) -> None:
        """prune() returns an int count of removed jobs."""
        tracker = JobTracker()
        result = tracker.prune()
        self.assertIsInstance(result, int)
        self.assertEqual(result, 0)

    def test_prune_returns_count_when_removing(self) -> None:
        """prune() returns the correct count of removed jobs."""
        tracker = JobTracker()
        jid = tracker.create_job(1)
        tracker.mark_done(jid, items=[], errors=[])
        tracker.update(jid, finished_at=time.monotonic() - 7200.0)

        count = tracker.prune(max_age_sec=1.0)
        self.assertEqual(count, 1, "prune() should return 1 for one removed job")
        self.assertIsNone(tracker.get(jid))


if __name__ == "__main__":
    unittest.main()
