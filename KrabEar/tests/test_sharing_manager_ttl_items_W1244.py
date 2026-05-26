"""Tests for W1242 F2+F3+F5 fixes in SharingManager (W1244).

F2 MED: ttl_hours validation — negative clamped to 0, inf/nan rejected, >max clamped.
F3 MED: item_ids unbounded — capped at _MAX_SHARE_ITEMS = 100.
F5 LOW: empty content (all item_ids missing) returns warning: "no_items_found".
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

# Ensure backend package is importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.sharing_manager import SharingManager, _MAX_TTL_HOURS, _MAX_SHARE_ITEMS


# ---------------------------------------------------------------------------
# Minimal fake store — no items found (all lookups return None)
# ---------------------------------------------------------------------------

class _FakeItem:
    def __init__(self, item_id: str, text: str = "hello"):
        self.id = item_id
        self.text = text

    def to_dict(self):
        return {"id": self.id, "text": self.text, "ts": "2026-01-01T00:00:00Z"}


class _FakeStore:
    """Store that returns items for known IDs only."""

    def __init__(self, data_dir: str, items: dict[str, _FakeItem] | None = None):
        self.data_dir = data_dir
        self._items: dict[str, _FakeItem] = items or {}

    def get_history_item_by_id(self, item_id: str) -> _FakeItem | None:
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_manager(tmp_dir: str, items: dict | None = None) -> SharingManager:
    store = _FakeStore(data_dir=tmp_dir, items=items)
    return SharingManager(store=store)


# ---------------------------------------------------------------------------
# F2: TTL validation
# ---------------------------------------------------------------------------

class TestTTLValidation(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _call(self, ttl_hours_param, extra_ids: list | None = None):
        """Call handle_prepare_share with one valid item and the given ttl_hours."""
        item_id = "item-001"
        mgr = _make_manager(self._tmp, items={item_id: _FakeItem(item_id)})
        params = {
            "item_ids": [item_id],
            "format": "json",
            "ttl_hours": ttl_hours_param,
        }
        return mgr.handle_prepare_share(params)

    def test_ttl_hours_negative_clamped_to_zero(self):
        """Negative ttl_hours must be clamped to 0 (instant expiry, not error)."""
        item_id = "item-neg"
        mgr = _make_manager(self._tmp, items={item_id: _FakeItem(item_id)})
        params = {"item_ids": [item_id], "format": "json", "ttl_hours": -5.0}
        result = mgr.handle_prepare_share(params)
        # expires_at should be approximately now (ttl == 0 means immediate)
        import time
        expires_at = result.get("expires_at")
        self.assertIsNotNone(expires_at, "expires_at must be set when ttl_hours=0")
        self.assertAlmostEqual(expires_at, time.time(), delta=5.0)

    def test_ttl_hours_inf_clamped_to_max(self):
        """float('inf') must be rejected (not finite)."""
        with self.assertRaises(RuntimeError) as ctx:
            self._call(float("inf"))
        self.assertIn("конечным", str(ctx.exception).lower() + str(ctx.exception))

    def test_ttl_hours_nan_rejected(self):
        """float('nan') must be rejected (not finite)."""
        with self.assertRaises(RuntimeError) as ctx:
            self._call(float("nan"))
        self.assertIn("конечным", str(ctx.exception).lower() + str(ctx.exception))

    def test_ttl_hours_above_max_clamped(self):
        """ttl_hours > _MAX_TTL_HOURS must be clamped to _MAX_TTL_HOURS."""
        import time
        item_id = "item-bigttl"
        mgr = _make_manager(self._tmp, items={item_id: _FakeItem(item_id)})
        params = {"item_ids": [item_id], "format": "json", "ttl_hours": 99999.0}
        result = mgr.handle_prepare_share(params)
        expires_at = result.get("expires_at")
        self.assertIsNotNone(expires_at)
        expected_max = time.time() + _MAX_TTL_HOURS * 3600.0
        self.assertLessEqual(expires_at, expected_max + 5.0,
                             "expires_at must not exceed now + _MAX_TTL_HOURS")
        # And must not be less than now + _MAX_TTL_HOURS - epsilon
        self.assertGreaterEqual(expires_at, expected_max - 5.0,
                                "expires_at must be close to max TTL")

    def test_ttl_hours_default_applied_when_missing(self):
        """When ttl_hours is absent, default of 1 hour must be applied."""
        import time
        item_id = "item-default"
        mgr = _make_manager(self._tmp, items={item_id: _FakeItem(item_id)})
        params = {"item_ids": [item_id], "format": "json"}
        result = mgr.handle_prepare_share(params)
        expires_at = result.get("expires_at")
        self.assertIsNotNone(expires_at)
        expected = time.time() + 1.0 * 3600.0
        self.assertAlmostEqual(expires_at, expected, delta=5.0)


# ---------------------------------------------------------------------------
# F3: item_ids cap
# ---------------------------------------------------------------------------

class TestItemIdsCap(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_item_ids_above_100_rejected(self):
        """item_ids list with >100 entries must raise RuntimeError."""
        ids = [f"id-{i}" for i in range(_MAX_SHARE_ITEMS + 1)]
        items = {iid: _FakeItem(iid) for iid in ids}
        mgr = _make_manager(self._tmp, items=items)
        params = {"item_ids": ids, "format": "json", "ttl_hours": 1.0}
        with self.assertRaises(RuntimeError) as ctx:
            mgr.handle_prepare_share(params)
        err = str(ctx.exception)
        self.assertIn(str(_MAX_SHARE_ITEMS), err)

    def test_item_ids_exactly_100_accepted(self):
        """item_ids list with exactly 100 entries must succeed."""
        ids = [f"id-{i}" for i in range(_MAX_SHARE_ITEMS)]
        items = {iid: _FakeItem(iid) for iid in ids}
        mgr = _make_manager(self._tmp, items=items)
        params = {"item_ids": ids, "format": "json", "ttl_hours": 1.0}
        result = mgr.handle_prepare_share(params)
        self.assertIn("share_id", result)

    def test_item_ids_below_100_accepted(self):
        """item_ids list with fewer than 100 entries must succeed."""
        ids = ["id-a", "id-b"]
        items = {iid: _FakeItem(iid) for iid in ids}
        mgr = _make_manager(self._tmp, items=items)
        params = {"item_ids": ids, "format": "json", "ttl_hours": 1.0}
        result = mgr.handle_prepare_share(params)
        self.assertIn("share_id", result)


# ---------------------------------------------------------------------------
# F5: empty content warning
# ---------------------------------------------------------------------------

class TestEmptyItemsWarning(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_empty_item_ids_returns_warning(self):
        """When all item_ids resolve to nothing, result must include warning: 'no_items_found'."""
        # Store has no items — all lookups return None
        mgr = _make_manager(self._tmp, items={})
        params = {
            "item_ids": ["ghost-1", "ghost-2"],
            "format": "json",
            "ttl_hours": 1.0,
        }
        result = mgr.handle_prepare_share(params)
        self.assertEqual(result.get("warning"), "no_items_found",
                         f"Expected warning='no_items_found', got: {result}")

    def test_partial_items_no_warning(self):
        """When at least one item resolves, no warning should appear."""
        ids = ["real-1", "ghost-2"]
        items = {"real-1": _FakeItem("real-1")}
        mgr = _make_manager(self._tmp, items=items)
        params = {
            "item_ids": ids,
            "format": "json",
            "ttl_hours": 1.0,
        }
        result = mgr.handle_prepare_share(params)
        self.assertNotIn("warning", result,
                         "No warning expected when at least one item found")

    def test_all_items_found_no_warning(self):
        """When all items resolve successfully, no warning should appear."""
        ids = ["a", "b"]
        items = {"a": _FakeItem("a"), "b": _FakeItem("b")}
        mgr = _make_manager(self._tmp, items=items)
        params = {
            "item_ids": ids,
            "format": "markdown",
            "ttl_hours": 1.0,
        }
        result = mgr.handle_prepare_share(params)
        self.assertNotIn("warning", result)


if __name__ == "__main__":
    unittest.main()
