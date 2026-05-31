"""Tests for W1243 F2 HIGH fix: AutoDeduplicator scan cap + background offload.

Covers:
  - test_run_dedup_caps_scan_at_1000_items
  - test_missing_ts_treated_as_oldest
  - test_run_dedup_returns_job_id_immediately
  - test_dedup_progress_returns_status
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.auto_deduplication import (
    AutoDeduplicator,
    _MAX_DEDUP_SCAN,
    _MISSING_TS_PLACEHOLDER,
    DEFAULT_DEDUP_THRESHOLD,
)


def _make_items(n: int, with_ts: bool = True) -> list[dict]:
    """Генерирует n уникальных записей истории."""
    items = []
    for i in range(n):
        item: dict = {"id": f"item-{i:05d}", "text": f"Unique transcription number {i} with different content"}
        if with_ts:
            item["ts"] = f"2024-01-01T{i // 3600:02d}:{(i % 3600) // 60:02d}:{i % 60:02d}+00:00"
        items.append(item)
    return items


class MockStore:
    """Фиктивный StateStore для тестов scan cap."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    def get_history_page(self, cursor=None, limit: int = 200):
        """Возвращает страницу из внутреннего списка."""
        if cursor is None:
            start = 0
        else:
            start = int(cursor)
        end = start + limit
        page = self._items[start:end]
        next_cursor = str(end) if end < len(self._items) else None
        return page, next_cursor


class TestRunDedupScanCap(unittest.TestCase):
    """Test that run_deduplication respects the _MAX_DEDUP_SCAN limit.

    W1746 performance note: find_duplicates() is O(n²) via SequenceMatcher —
    1000 items takes ~25 s, 1500 items ~78 s.  These scan-cap tests only verify
    *how many items were loaded*, not whether duplicate detection worked, so we
    mock _detector.find_duplicates to return [] immediately.  This keeps the
    tests fast (< 0.1 s) without losing coverage of the scan-cap logic.
    """

    def _make_fast_deduplicator(self) -> "AutoDeduplicator":
        """Return an AutoDeduplicator whose inner detector is a no-op mock."""
        deduplicator = AutoDeduplicator()
        deduplicator._detector = MagicMock()
        deduplicator._detector.find_duplicates.return_value = []
        return deduplicator

    def test_run_dedup_caps_scan_at_1000_items(self) -> None:
        """run_deduplication with >1000 items scans only _MAX_DEDUP_SCAN items."""
        # Create 1500 items — more than the cap of 1000
        items = _make_items(1500)
        store = MockStore(items)
        deduplicator = self._make_fast_deduplicator()

        result = deduplicator.run_deduplication(store=store, threshold=DEFAULT_DEDUP_THRESHOLD)

        # total_scanned must not exceed _MAX_DEDUP_SCAN
        self.assertLessEqual(
            result["total_scanned"],
            _MAX_DEDUP_SCAN,
            f"Expected total_scanned <= {_MAX_DEDUP_SCAN}, got {result['total_scanned']}",
        )
        self.assertEqual(result["total_scanned"], _MAX_DEDUP_SCAN)

    def test_run_dedup_small_store_not_capped(self) -> None:
        """run_deduplication with <=1000 items scans all of them, capped=False."""
        items = _make_items(50)
        store = MockStore(items)
        deduplicator = self._make_fast_deduplicator()

        result = deduplicator.run_deduplication(store=store, threshold=DEFAULT_DEDUP_THRESHOLD)

        self.assertEqual(result["total_scanned"], 50)
        self.assertFalse(result["capped"])

    def test_run_dedup_exactly_at_cap(self) -> None:
        """run_deduplication with exactly _MAX_DEDUP_SCAN items scans all."""
        items = _make_items(_MAX_DEDUP_SCAN)
        store = MockStore(items)
        deduplicator = self._make_fast_deduplicator()

        result = deduplicator.run_deduplication(store=store, threshold=DEFAULT_DEDUP_THRESHOLD)

        self.assertEqual(result["total_scanned"], _MAX_DEDUP_SCAN)

    def test_run_dedup_returns_total_in_store_key(self) -> None:
        """run_deduplication result includes total_in_store and capped keys."""
        items = _make_items(10)
        store = MockStore(items)
        deduplicator = AutoDeduplicator()

        result = deduplicator.run_deduplication(store=store)

        self.assertIn("total_in_store", result)
        self.assertIn("capped", result)
        self.assertIn("total_scanned", result)
        self.assertIn("duplicate_groups", result)
        self.assertIn("duplicates", result)


class TestMissingTsTreatedAsOldest(unittest.TestCase):
    """Test that items without ts field are treated as epoch 0 (oldest)."""

    def test_missing_ts_treated_as_oldest(self) -> None:
        """Items missing ts field get _MISSING_TS_PLACEHOLDER in the normalized list."""
        # Create items without ts field
        items_no_ts = _make_items(5, with_ts=False)
        store = MockStore(items_no_ts)
        deduplicator = AutoDeduplicator()

        # run_deduplication should not raise and should handle missing ts
        result = deduplicator.run_deduplication(store=store, threshold=DEFAULT_DEDUP_THRESHOLD)

        # Must complete without error
        self.assertIn("total_scanned", result)
        self.assertEqual(result["total_scanned"], 5)

    def test_missing_ts_placeholder_value(self) -> None:
        """_MISSING_TS_PLACEHOLDER is epoch 1970 (treated as oldest)."""
        # The placeholder must be earlier than any real timestamp
        self.assertTrue(
            _MISSING_TS_PLACEHOLDER.startswith("1970-"),
            f"Expected 1970-* placeholder, got: {_MISSING_TS_PLACEHOLDER}",
        )

    def test_check_duplicate_missing_ts_not_matched_as_duplicate(self) -> None:
        """check_duplicate with store item missing ts does not cause a crash."""
        item_no_ts = {"id": "legacy-001", "text": "Транскрипция без временной метки"}
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([item_no_ts], None)

        deduplicator = AutoDeduplicator()
        from datetime import datetime, timezone
        now_ts = datetime.now(tz=timezone.utc).isoformat()

        # Should not raise
        result = deduplicator.check_duplicate(
            text="Транскрипция без временной метки",
            timestamp=now_ts,
            store=mock_store,
        )
        # Because missing-ts item is treated as epoch 0 (far in the past),
        # it falls outside the 60-second window → not a duplicate
        self.assertFalse(
            result.is_duplicate,
            "Legacy item with missing ts should NOT match as duplicate (epoch-0 fallback)",
        )

    def test_check_duplicate_does_not_raise_on_none_ts(self) -> None:
        """check_duplicate handles None ts gracefully."""
        item_none_ts = {"id": "legacy-002", "text": "Тест None ts", "ts": None}
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([item_none_ts], None)

        deduplicator = AutoDeduplicator()
        from datetime import datetime, timezone
        now_ts = datetime.now(tz=timezone.utc).isoformat()

        # Should not raise
        result = deduplicator.check_duplicate(
            text="Тест None ts",
            timestamp=now_ts,
            store=mock_store,
        )
        self.assertIn(result.action_taken, ("kept", "skipped", "merged"))


class TestRunDedupReturnsJobIdImmediately(unittest.TestCase):
    """Test that handle_run_deduplication returns a synchronous result (W1540 reverted async)."""

    def test_run_dedup_returns_job_id_immediately(self) -> None:
        """handle_run_deduplication returns synchronous dedup result dict (W1540 contract)."""
        deduplicator = AutoDeduplicator()

        slow_store = MagicMock()
        slow_store.get_history_page.return_value = ([], None)

        start = time.monotonic()
        result = deduplicator.handle_run_deduplication({"_store": slow_store, "threshold": 0.9})
        elapsed = time.monotonic() - start

        # Must complete quickly for empty store
        self.assertLess(elapsed, 5.0, "handle_run_deduplication must complete quickly")

        # W1540: synchronous result contains dedup stats fields (not async job_id)
        self.assertIn("total_scanned", result, f"Expected dedup result fields, got: {result}")
        self.assertIn("duplicate_groups", result)
        self.assertIn("duplicates", result)

    def test_run_dedup_async_job_created_in_registry(self) -> None:
        """After calling run_deduplication_async, the job appears in the registry."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        job_id = deduplicator.run_deduplication_async(store=mock_store)

        state = deduplicator.get_dedup_job(job_id)
        self.assertIsNotNone(state, "Job should be in the registry immediately")
        self.assertIn(state["status"], ("queued", "running", "done"))

    def test_run_dedup_async_completes(self) -> None:
        """Background job eventually reaches 'done' status."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        job_id = deduplicator.run_deduplication_async(store=mock_store)

        # Wait for completion (up to 5 seconds)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = deduplicator.get_dedup_job(job_id)
            if state and state["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

        state = deduplicator.get_dedup_job(job_id)
        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "done", f"Job should be done, got: {state}")
        self.assertIsNotNone(state["result"])

    def test_run_dedup_returns_job_id_via_handle_method(self) -> None:
        """handle_run_deduplication returns sync dedup result (W1540 reverted async)."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        result = deduplicator.handle_run_deduplication(
            {"_store": mock_store, "threshold": 0.9}
        )

        # W1540: synchronous result has dedup stats fields
        self.assertIn("total_scanned", result, f"Expected dedup fields, got: {result}")
        self.assertIn("duplicates", result)
        self.assertIsInstance(result["duplicates"], list)


class TestDedupProgressReturnsStatus(unittest.TestCase):
    """Test the dedup_progress IPC handler."""

    def test_dedup_progress_returns_status(self) -> None:
        """dedup_progress returns status for a valid job_id."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        # Start an async job
        job_id = deduplicator.run_deduplication_async(store=mock_store)

        # Poll for progress
        result = deduplicator.handle_dedup_progress({"job_id": job_id})

        self.assertTrue(result.get("found"), f"Expected found=True, got: {result}")
        self.assertEqual(result["job_id"], job_id)
        self.assertIn(result["status"], ("queued", "running", "done", "failed"))
        self.assertIn("elapsed_sec", result)
        self.assertIsInstance(result["elapsed_sec"], float)

    def test_dedup_progress_unknown_job_id(self) -> None:
        """dedup_progress with unknown job_id returns found=False."""
        deduplicator = AutoDeduplicator()
        result = deduplicator.handle_dedup_progress({"job_id": "nonexistent-job-xyz"})

        self.assertFalse(result.get("found"))
        self.assertEqual(result["job_id"], "nonexistent-job-xyz")

    def test_dedup_progress_missing_job_id_raises(self) -> None:
        """dedup_progress without job_id raises ValueError."""
        deduplicator = AutoDeduplicator()
        with self.assertRaises(ValueError):
            deduplicator.handle_dedup_progress({})

    def test_dedup_progress_via_handle_method(self) -> None:
        """run_deduplication_async + handle_dedup_progress round-trip (W1540: use async directly)."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        # W1540: handle_run_deduplication is now synchronous; use run_deduplication_async for async
        job_id = deduplicator.run_deduplication_async(store=mock_store)

        # Poll progress
        progress = deduplicator.handle_dedup_progress({"job_id": job_id})
        self.assertTrue(progress.get("found"))
        self.assertEqual(progress["job_id"], job_id)
        self.assertIn(progress["status"], ("queued", "running", "done", "failed"))

    def test_dedup_progress_result_populated_after_completion(self) -> None:
        """dedup_progress result field is populated after job completes."""
        deduplicator = AutoDeduplicator()
        mock_store = MagicMock()
        mock_store.get_history_page.return_value = ([], None)

        job_id = deduplicator.run_deduplication_async(store=mock_store)

        # Wait for completion
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            state = deduplicator.get_dedup_job(job_id)
            if state and state["status"] == "done":
                break
            time.sleep(0.05)

        result = deduplicator.handle_dedup_progress({"job_id": job_id})
        self.assertEqual(result["status"], "done")
        self.assertIsNotNone(result["result"])
        self.assertIn("total_scanned", result["result"])
        self.assertIn("duplicate_groups", result["result"])
        self.assertIsNone(result["error"])


class TestMaxDedupScanConstant(unittest.TestCase):
    """Sanity checks for _MAX_DEDUP_SCAN constant."""

    def test_max_dedup_scan_is_1000(self) -> None:
        """_MAX_DEDUP_SCAN must be exactly 1000."""
        self.assertEqual(_MAX_DEDUP_SCAN, 1000)

    def test_max_dedup_scan_is_positive_int(self) -> None:
        """_MAX_DEDUP_SCAN must be a positive int."""
        self.assertIsInstance(_MAX_DEDUP_SCAN, int)
        self.assertGreater(_MAX_DEDUP_SCAN, 0)


if __name__ == "__main__":
    unittest.main()
