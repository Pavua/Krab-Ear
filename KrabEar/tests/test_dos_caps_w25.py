"""Wave-25 MED DoS caps + input-validation tests.

Covers:
  - CollectionManager: MAX_COLLECTIONS cap, MAX_ITEMS_PER_COLLECTION cap,
    MAX_COLLECTION_NAME_LEN, item_id validation (empty / non-string / too-long).
  - RecordingScheduler: MAX_SCHEDULED_RECORDINGS cap, eviction of terminal entries.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Path bootstrap — mirrors the pattern used throughout this test suite.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT / "KrabEar") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))

from backend.collection_manager import (  # noqa: E402
    CollectionManager,
    MAX_COLLECTIONS,
    MAX_ITEMS_PER_COLLECTION,
    MAX_COLLECTION_NAME_LEN,
    MAX_ITEM_ID_LEN,
)
from backend.recording_scheduler import (  # noqa: E402
    RecordingScheduler,
    MAX_SCHEDULED_RECORDINGS,
    MAX_PENDING_SCHEDULES,  # wave-25: separate pending cap (50) added alongside total cap (1000)
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    _EVICT_AFTER_SECONDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: Path) -> MagicMock:
    """Return a minimal fake store that CollectionManager accepts."""
    store = MagicMock()
    store.data_dir = str(tmp_dir)
    return store


# ---------------------------------------------------------------------------
# CollectionManager tests
# ---------------------------------------------------------------------------

class TestCollectionManagerCaps(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._store = _make_store(Path(self._tmp))
        self._mgr = CollectionManager(self._store)

    def _make_collections(self, n: int) -> None:
        """Create exactly n collections named col_0 … col_{n-1}."""
        for i in range(n):
            result = self._mgr.create_collection(f"col_{i}")
            # All creates up to MAX_COLLECTIONS should succeed
            self.assertNotIn("reason", result, f"Unexpected limit at i={i}")

    # --- Collection-count cap ---

    def test_create_501st_collection_returns_limit_exceeded(self) -> None:
        """Creating the (MAX_COLLECTIONS+1)th collection must return limit_exceeded."""
        self._make_collections(MAX_COLLECTIONS)

        result = self._mgr.create_collection("overflow_collection")

        self.assertFalse(result.get("ok"), "Expected ok=False")
        self.assertEqual(result.get("reason"), "limit_exceeded")
        self.assertIn("detail", result)

    def test_exactly_500_collections_allowed(self) -> None:
        """Exactly MAX_COLLECTIONS collections must be accepted."""
        self._make_collections(MAX_COLLECTIONS)
        self.assertEqual(len(self._mgr.list_collections()), MAX_COLLECTIONS)

    # --- Collection name length ---

    def test_name_too_long_raises_value_error(self) -> None:
        long_name = "x" * (MAX_COLLECTION_NAME_LEN + 1)
        with self.assertRaises(ValueError):
            self._mgr.create_collection(long_name)

    def test_name_exactly_max_len_is_accepted(self) -> None:
        name = "x" * MAX_COLLECTION_NAME_LEN
        result = self._mgr.create_collection(name)
        self.assertNotIn("reason", result)

    # --- Items-per-collection cap ---

    def test_add_10001st_item_returns_limit_exceeded(self) -> None:
        """Adding the (MAX_ITEMS_PER_COLLECTION+1)th item must return limit_exceeded."""
        self._mgr.create_collection("big_col")
        for i in range(MAX_ITEMS_PER_COLLECTION):
            r = self._mgr.add_to_collection("big_col", f"item_{i}")
            self.assertNotIn("reason", r, f"Unexpected limit at item {i}")

        overflow = self._mgr.add_to_collection("big_col", "overflow_item")

        self.assertFalse(overflow.get("ok"), "Expected ok=False")
        self.assertEqual(overflow.get("reason"), "limit_exceeded")
        self.assertIn("detail", overflow)

    def test_exactly_10000_items_allowed(self) -> None:
        self._mgr.create_collection("cap_col")
        for i in range(MAX_ITEMS_PER_COLLECTION):
            self._mgr.add_to_collection("cap_col", f"item_{i}")
        items = [c for c in self._mgr.list_collections() if c["name"] == "cap_col"]
        self.assertEqual(items[0]["item_count"], MAX_ITEMS_PER_COLLECTION)

    # --- item_id input validation ---

    def test_empty_item_id_raises_value_error(self) -> None:
        self._mgr.create_collection("my_col")
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("my_col", "")

    def test_whitespace_only_item_id_raises_value_error(self) -> None:
        self._mgr.create_collection("my_col")
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("my_col", "   ")

    def test_non_string_item_id_raises_value_error(self) -> None:
        self._mgr.create_collection("my_col")
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("my_col", 12345)  # type: ignore[arg-type]

    def test_none_item_id_raises_value_error(self) -> None:
        self._mgr.create_collection("my_col")
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("my_col", None)  # type: ignore[arg-type]

    def test_item_id_too_long_raises_value_error(self) -> None:
        self._mgr.create_collection("my_col")
        long_id = "a" * (MAX_ITEM_ID_LEN + 1)
        with self.assertRaises(ValueError):
            self._mgr.add_to_collection("my_col", long_id)

    def test_item_id_exactly_max_len_is_accepted(self) -> None:
        self._mgr.create_collection("my_col")
        max_id = "b" * MAX_ITEM_ID_LEN
        result = self._mgr.add_to_collection("my_col", max_id)
        self.assertNotIn("reason", result)


# ---------------------------------------------------------------------------
# RecordingScheduler tests
# ---------------------------------------------------------------------------

class TestRecordingSchedulerCaps(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self._sched = RecordingScheduler(self._tmp)

    def _future_iso(self, offset_hours: int = 1) -> str:
        dt = datetime.now(tz=timezone.utc) + timedelta(hours=offset_hours)
        return dt.isoformat()

    def _fill_to_cap(self) -> None:
        """Schedule exactly MAX_PENDING_SCHEDULES pending entries.

        Wave-25 added MAX_PENDING_SCHEDULES=50 (pending cap, checked first)
        alongside MAX_SCHEDULED_RECORDINGS=1000 (total cap). _fill_to_cap
        creates pending (future) items so it hits the pending cap, not the
        total cap. Tests are repointed to test the pending cap.
        """
        for i in range(MAX_PENDING_SCHEDULES):
            self._sched.schedule_recording(
                start_time=self._future_iso(offset_hours=i + 1),
                duration_sec=60,
                label=f"entry_{i}",
            )

    def test_1001st_recording_raises_value_error(self) -> None:
        """Scheduling the (MAX_PENDING_SCHEDULES+1)th PENDING entry must raise ValueError.

        Wave-25: pending cap (50) fires before total cap (1000) for future items.
        """
        self._fill_to_cap()
        with self.assertRaises(ValueError, msg="Expected ValueError at pending cap+1"):
            self._sched.schedule_recording(
                start_time=self._future_iso(offset_hours=2000),
                duration_sec=30,
                label="overflow",
            )

    def test_exactly_1000_recordings_allowed(self) -> None:
        """Exactly MAX_PENDING_SCHEDULES pending items are accepted (cap is inclusive)."""
        self._fill_to_cap()
        items = self._sched.list_scheduled()
        self.assertEqual(len(items), MAX_PENDING_SCHEDULES)

    def test_eviction_of_old_cancelled_entries_frees_cap(self) -> None:
        """Old (>24h) cancelled entries are evicted, allowing new schedules."""
        # Manually inject old cancelled entries directly to bypass the cap check
        old_dt = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_EVICT_AFTER_SECONDS + 10)
        ).isoformat()
        import uuid
        for i in range(MAX_SCHEDULED_RECORDINGS):
            sid = str(uuid.uuid4())
            self._sched._schedules[sid] = {
                "id": sid,
                "start_time": old_dt,
                "duration_sec": 60,
                "label": f"old_{i}",
                "status": STATUS_CANCELLED,
                "created_at": old_dt,
            }

        # Should NOT raise — eviction frees space before cap check
        result = self._sched.schedule_recording(
            start_time=self._future_iso(1),
            duration_sec=60,
            label="new_entry",
        )
        self.assertEqual(result["label"], "new_entry")

    def test_eviction_of_old_completed_entries_frees_cap(self) -> None:
        """Old (>24h) completed entries are also evicted."""
        old_dt = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_EVICT_AFTER_SECONDS + 10)
        ).isoformat()
        import uuid
        for i in range(MAX_SCHEDULED_RECORDINGS):
            sid = str(uuid.uuid4())
            self._sched._schedules[sid] = {
                "id": sid,
                "start_time": old_dt,
                "duration_sec": 60,
                "label": f"done_{i}",
                "status": STATUS_COMPLETED,
                "created_at": old_dt,
            }

        result = self._sched.schedule_recording(
            start_time=self._future_iso(1),
            duration_sec=60,
            label="after_eviction",
        )
        self.assertEqual(result["label"], "after_eviction")

    def test_recent_terminal_entries_not_evicted(self) -> None:
        """Terminal entries younger than 24h are NOT evicted."""
        import uuid
        recent_dt = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_EVICT_AFTER_SECONDS - 60)
        ).isoformat()
        for i in range(MAX_SCHEDULED_RECORDINGS):
            sid = str(uuid.uuid4())
            self._sched._schedules[sid] = {
                "id": sid,
                "start_time": recent_dt,
                "duration_sec": 60,
                "label": f"recent_{i}",
                "status": STATUS_CANCELLED,
                "created_at": recent_dt,
            }

        # Should raise — recent terminal entries are NOT evicted
        with self.assertRaises(ValueError):
            self._sched.schedule_recording(
                start_time=self._future_iso(1),
                duration_sec=60,
                label="overflow",
            )

    def test_list_scheduled_triggers_eviction(self) -> None:
        """list_scheduled() also evicts old terminal entries."""
        old_dt = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=_EVICT_AFTER_SECONDS + 10)
        ).isoformat()
        import uuid
        old_ids = set()
        for i in range(5):
            sid = str(uuid.uuid4())
            old_ids.add(sid)
            self._sched._schedules[sid] = {
                "id": sid,
                "start_time": old_dt,
                "duration_sec": 60,
                "label": f"old_{i}",
                "status": STATUS_COMPLETED,
                "created_at": old_dt,
            }
        # Add one fresh pending entry
        self._sched.schedule_recording(
            start_time=self._future_iso(1),
            duration_sec=30,
            label="fresh",
        )

        items = self._sched.list_scheduled()

        returned_ids = {it["id"] for it in items}
        # Old completed entries should have been evicted
        self.assertTrue(old_ids.isdisjoint(returned_ids))
        # Fresh pending entry must still be present
        fresh = [it for it in items if it["label"] == "fresh"]
        self.assertEqual(len(fresh), 1)

    def test_invalid_duration_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._sched.schedule_recording(
                start_time=self._future_iso(1),
                duration_sec=0,
            )


if __name__ == "__main__":
    unittest.main()
