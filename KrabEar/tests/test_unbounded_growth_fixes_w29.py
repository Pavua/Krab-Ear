"""wave-29 memory-leak fixes — 3 unbounded in-memory accumulators bounded.

1. event_bus.EventBus._listeners — _MAX_LISTENERS cap + remove_listener() API
   (startup-once listeners, but cap guards against reinit/hot-reload accumulation).
2. auto_deduplication.AutoDeduplicator._jobs — _MAX_TRACKED_JOBS cap; oldest terminal
   (done/failed/cancelled) job evicted on new-job creation (running/queued protected).
3. sharing_manager.SharingManager._index — expired (TTL-passed) entries pruned from
   _index (they carried full transcript content → memory + privacy leak; list_shared
   used to only filter them).
"""
import sys
import time
import types
import unittest
import shutil
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus, _MAX_LISTENERS  # noqa: E402
from backend.auto_deduplication import AutoDeduplicator, _MAX_TRACKED_JOBS  # noqa: E402
from backend.sharing_manager import SharingManager  # noqa: E402


class EventBusListenerCapTest(unittest.TestCase):
    def test_cap_enforced_and_remove_listener(self):
        bus = EventBus()
        added = [(lambda t, d: None) for _ in range(_MAX_LISTENERS + 10)]
        for cb in added:
            bus.add_listener(cb)
        self.assertLessEqual(
            len(bus._listeners), _MAX_LISTENERS,
            "listeners must be capped at _MAX_LISTENERS",
        )
        first = bus._listeners[0]
        bus.remove_listener(first)
        self.assertNotIn(first, bus._listeners)
        bus.remove_listener(lambda t, d: None)  # non-listener → no-op, must not raise


class AutoDedupJobEvictionTest(unittest.TestCase):
    def test_oldest_terminal_evicted_running_protected(self):
        dd = AutoDeduplicator()
        for i in range(_MAX_TRACKED_JOBS):
            dd._jobs[f"done{i}"] = {"job_id": f"done{i}", "status": "done"}
        dd._jobs["running0"] = {"job_id": "running0", "status": "running"}
        before = len(dd._jobs)
        dd._evict_oldest_terminal_job_locked()
        self.assertEqual(len(dd._jobs), before - 1)
        self.assertNotIn("done0", dd._jobs, "oldest terminal job must be evicted first")
        self.assertIn("running0", dd._jobs, "running job must never be evicted")

    def test_create_job_respects_cap(self):
        dd = AutoDeduplicator()
        for i in range(_MAX_TRACKED_JOBS):
            dd._jobs[f"done{i}"] = {"job_id": f"done{i}", "status": "done"}
        dd._create_dedup_job()
        self.assertLessEqual(len(dd._jobs), _MAX_TRACKED_JOBS)


class SharingPruneExpiredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="krab_share_test_")
        self.mgr = SharingManager(store=types.SimpleNamespace(data_dir=Path(self.tmp)))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_expired_entry_pruned_valid_kept(self):
        now = time.time()
        self.mgr._index["expired1"] = {
            "share_id": "expired1", "expires_at": now - 100,
            "filename": "krabear_share_expired1.md", "content": "SECRET",
        }
        self.mgr._index["valid1"] = {
            "share_id": "valid1", "expires_at": now + 10000,
            "filename": "krabear_share_valid1.md", "content": "keep",
        }
        listed = self.mgr.list_shared()  # default → prunes expired
        self.assertNotIn("expired1", self.mgr._index,
                         "expired entry (with content) must be pruned from _index")
        self.assertIn("valid1", self.mgr._index, "valid entry must remain")
        ids = {e.get("share_id") for e in listed}
        self.assertIn("valid1", ids)
        self.assertNotIn("expired1", ids)

    def test_revoked_entry_kept_as_tombstone(self):
        # revoked entries are already content-free (sensitive fields popped in revoke);
        # prune leaves them so revoke history / list_shared(include_revoked) still works.
        self.mgr._index["revoked1"] = {
            "share_id": "revoked1", "is_revoked": True,
            "filename": "krabear_share_revoked1.md", "expires_at": time.time() - 100,
        }
        self.mgr.list_shared()
        self.assertIn("revoked1", self.mgr._index, "revoked tombstone must be kept")


if __name__ == "__main__":
    unittest.main()
