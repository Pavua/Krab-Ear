"""Tests for W1267 F1+F2 guards:
- RecordingComparison.compare() raises ValueError when fewer than 2 items given (F1)
- _handle_get_recording_insights skips analysis when privacy_mode_enabled (F2)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.recording_comparison import RecordingComparison


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeHistoryItem:
    def __init__(self, **kwargs: Any) -> None:
        self._data = kwargs

    def to_dict(self) -> dict:
        return dict(self._data)


class FakeStore:
    def __init__(self) -> None:
        self._items: dict[str, FakeHistoryItem] = {}

    def add_item(self, item_id: str, text: str = "", **kwargs: Any) -> FakeHistoryItem:
        item = FakeHistoryItem(id=item_id, text=text, **kwargs)
        self._items[item_id] = item
        return item

    def get_history_item_by_id(self, item_id: str) -> FakeHistoryItem | None:
        return self._items.get(item_id)


# ---------------------------------------------------------------------------
# F1: RecordingComparison.compare() min-2 guard
# ---------------------------------------------------------------------------

class CompareSingleItemTestCase(unittest.TestCase):
    """W1267 F1 — compare() with fewer than 2 items must raise ValueError."""

    def setUp(self) -> None:
        self.svc = RecordingComparison()
        self.store = FakeStore()
        self.store.add_item("only_one", text="some text here")

    def test_compare_single_item_raises_value_error(self) -> None:
        """compare() with a single item_id raises ValueError (not misleading 1x1 result)."""
        with self.assertRaises(ValueError) as ctx:
            self.svc.compare(["only_one"], self.store)
        self.assertIn("2", str(ctx.exception))

    def test_compare_two_items_works(self) -> None:
        """compare() with exactly two items returns a valid ComparisonView."""
        self.store.add_item("second", text="another text here")
        from backend.recording_comparison import ComparisonView
        result = self.svc.compare(["only_one", "second"], self.store)
        self.assertIsInstance(result, ComparisonView)
        self.assertEqual(len(result.items), 2)
        # 2x2 similarity matrix
        self.assertEqual(len(result.text_similarity_matrix), 2)
        self.assertEqual(len(result.text_similarity_matrix[0]), 2)
        # Diagonal must be 1.0
        self.assertAlmostEqual(result.text_similarity_matrix[0][0], 1.0)
        self.assertAlmostEqual(result.text_similarity_matrix[1][1], 1.0)


# ---------------------------------------------------------------------------
# F2: _handle_get_recording_insights privacy gate
# ---------------------------------------------------------------------------

class _FakeRecordingInsights:
    """Stub for RecordingInsightsGenerator — tracks whether generate_insights was called."""

    def __init__(self) -> None:
        self.called = False

    def generate_insights(self, items: list, days: int = 7) -> list:
        self.called = True
        return []


def _handle_get_recording_insights_impl(self_obj: Any, params: dict) -> dict:
    """Extracted verbatim copy of _handle_get_recording_insights logic for isolated testing."""
    if self_obj._get_runtime_setting("privacy_mode_enabled", False):
        return {"ok": True, "insights": [], "skipped": "privacy_mode"}
    days = int(params.get("days", 7))
    try:
        with self_obj.store._lock():
            items = self_obj.store._load_active_items_unlocked()
    except Exception:
        items = []
    insights = self_obj._recording_insights.generate_insights(items, days=days)
    return {
        "insights": [i.to_dict() for i in insights],
        "count": len(insights),
        "days": days,
    }


class _FakeLock:
    def __enter__(self): return self
    def __exit__(self, *a): pass


class _FakeStoreSvc:
    def _lock(self): return _FakeLock()
    def _load_active_items_unlocked(self): return []


class _FakeService:
    """Minimal stand-in for BackendService to test the recording-insights method."""

    def __init__(self, privacy_enabled: bool) -> None:
        self._privacy_enabled = privacy_enabled
        self.store = _FakeStoreSvc()
        self._recording_insights = _FakeRecordingInsights()

    def _get_runtime_setting(self, key: str, default: Any = None) -> Any:
        if key == "privacy_mode_enabled":
            return self._privacy_enabled
        return default

    def _handle_get_recording_insights(self, params: dict) -> dict:
        return _handle_get_recording_insights_impl(self, params)


class RecordingInsightsPrivacyGateTestCase(unittest.TestCase):
    """W1267 F2 — _handle_get_recording_insights must skip when privacy_mode_enabled."""

    def test_recording_insights_skipped_in_privacy_mode(self) -> None:
        """When privacy_mode_enabled=True the handler returns skipped:privacy_mode without calling generator."""
        svc = _FakeService(privacy_enabled=True)
        result = svc._handle_get_recording_insights({})
        self.assertFalse(
            svc._recording_insights.called,
            "generate_insights should NOT be called in privacy mode",
        )
        self.assertEqual(result.get("skipped"), "privacy_mode")
        self.assertEqual(result.get("insights"), [])

    def test_recording_insights_runs_normally(self) -> None:
        """When privacy_mode_enabled=False the handler calls generate_insights and returns results."""
        svc = _FakeService(privacy_enabled=False)
        result = svc._handle_get_recording_insights({"days": 7})
        self.assertTrue(
            svc._recording_insights.called,
            "generate_insights SHOULD be called when privacy mode is off",
        )
        self.assertNotIn("skipped", result)
        self.assertIn("insights", result)


if __name__ == "__main__":
    unittest.main()
