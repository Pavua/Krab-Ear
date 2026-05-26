"""Edge-case tests for history_service.handle_cleanup_old_history — W844 tz-aware fix.

W844 replaced lexicographic string comparison (item.ts < cutoff_iso) with proper
POSIX timestamp comparison (datetime.fromisoformat().timestamp()), so that:
  - tz-naive ISO strings (old format, no offset) are handled via local-time parsing
  - tz-aware strings with +00:00 suffix are handled correctly
  - "Z" suffix strings are normalised to +00:00 before parsing
  - Malformed / empty ts values return float("inf") and are NEVER deleted

These tests inject HistoryItem records with controlled .ts strings directly into the
NDJSON store, bypassing HistoryItem.create() (which always generates tz-naive local
timestamps), so we can cover every branch of the _item_ts() helper.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from backend.history_service import HistoryService
    from backend.state_store import StateStore
    _SKIP = False
except ImportError:
    _SKIP = True


def _write_item(store: "StateStore", ts: str, text: str = "test") -> str:
    """Append a raw NDJSON row with the given ts directly to history.ndjson.

    Returns the synthetic item id so callers can verify deletion counts.
    """
    import uuid
    item_id = str(uuid.uuid4())
    row = {"id": item_id, "ts": ts, "text": text, "paste_status": "ok"}
    store.history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(store.history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return item_id


@unittest.skipIf(_SKIP, "HistoryService or StateStore not available")
class CleanupTzAwareTestCase(unittest.TestCase):
    """Tests for W844 fix: tz-aware POSIX timestamp comparison in cleanup."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.svc = HistoryService(store=self.store)

    # ------------------------------------------------------------------
    # Test 1: tz-naive old timestamp is correctly identified as old
    # ------------------------------------------------------------------
    def test_tz_naive_old_item_is_deleted(self):
        """A tz-naive ISO timestamp 10 years in the past must be cleaned up.

        Before W844 fix the lexicographic comparison "2016-01-01T12:00:00" <
        "2026-05-26T12:00:00+00:00" could silently fail because the offset
        suffix makes the cutoff string lexicographically *longer*, not smaller
        in the right place.  The POSIX comparison always returns the correct
        answer.
        """
        very_old_naive = "2016-01-01T12:00:00"  # 10 years ago, no tz offset
        _write_item(self.store, very_old_naive, text="ancient naive record")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 1, "Old tz-naive item must be deleted")
        self.assertEqual(result["remaining"], 0)

    # ------------------------------------------------------------------
    # Test 2: tz-aware (+00:00) old timestamp is correctly deleted
    # ------------------------------------------------------------------
    def test_tz_aware_utc_old_item_is_deleted(self):
        """A tz-aware +00:00 timestamp 60 days in the past must be cleaned up."""
        old_aware = (
            datetime.now(timezone.utc) - timedelta(days=60)
        ).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        _write_item(self.store, old_aware, text="old utc-aware record")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 1, "Old tz-aware item must be deleted")

    # ------------------------------------------------------------------
    # Test 3: tz-aware "Z" suffix timestamp that is old is deleted
    # ------------------------------------------------------------------
    def test_tz_aware_z_suffix_old_item_is_deleted(self):
        """A timestamp using 'Z' suffix (ISO 8601 UTC shorthand) must be normalised
        and deleted correctly when it is older than the threshold.
        """
        old_z = (
            datetime.now(timezone.utc) - timedelta(days=40)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_item(self.store, old_z, text="old Z-suffix record")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 1, "Old Z-suffix item must be deleted")

    # ------------------------------------------------------------------
    # Test 4: malformed ts is NEVER deleted (returns inf)
    # ------------------------------------------------------------------
    def test_malformed_ts_item_is_never_deleted(self):
        """An item with a malformed (but non-empty) timestamp must never be deleted.

        _item_ts() returns float("inf") for unparseable strings, meaning the
        item appears to be infinitely far in the future and is therefore
        never older than any threshold.

        Note: items with empty ts strings are filtered by StateStore
        (_load_active_items_unlocked requires item.ts to be truthy), so they
        don't reach the cleanup logic at all — that's tested separately in
        test_empty_ts_item_not_loaded_as_active.
        """
        _write_item(self.store, "not-a-date", text="malformed ts record")
        _write_item(self.store, "TOTALLY_BAD_TS", text="garbage ts record")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 1})
        self.assertEqual(result["deleted_count"], 0, "Malformed-ts items must not be deleted")
        self.assertEqual(result["remaining"], 2)

    def test_empty_ts_item_not_loaded_as_active(self):
        """Items with an empty ts string are excluded by StateStore validation.

        StateStore._load_active_items_unlocked filters out items where
        not item.ts, so an empty-ts item never reaches handle_cleanup_old_history.
        This test confirms that the remaining count is 0 (item was silently
        dropped by the store, not by the cleanup logic).
        """
        _write_item(self.store, "", text="empty ts record")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 1})
        # Empty-ts item is invisible to the cleanup method — deleted_count=0, remaining=0
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["remaining"], 0)

    # ------------------------------------------------------------------
    # Test 6: mixed bag — only old items deleted, fresh + malformed survive
    # ------------------------------------------------------------------
    def test_mixed_tz_naive_aware_and_malformed(self):
        """Mixed store: one old tz-naive, one old tz-aware (Z), one fresh, one malformed.

        Only the two old items should be cleaned up; the fresh tz-naive and
        the malformed item must survive.
        """
        # Old tz-naive (5 years ago)
        old_naive = (datetime.now() - timedelta(days=365 * 5)).isoformat(timespec="seconds")
        _write_item(self.store, old_naive, text="old naive")

        # Old tz-aware with Z suffix (45 days ago)
        old_z = (
            datetime.now(timezone.utc) - timedelta(days=45)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_item(self.store, old_z, text="old z-suffix")

        # Fresh tz-naive (just now — will definitely not be older than 30 days)
        fresh_naive = datetime.now().isoformat(timespec="seconds")
        _write_item(self.store, fresh_naive, text="fresh naive")

        # Malformed ts — should survive (returns inf in _item_ts)
        _write_item(self.store, "BADTS!!!", text="malformed")

        result = self.svc.handle_cleanup_old_history({"older_than_days": 30})
        self.assertEqual(result["deleted_count"], 2, "Only the two old items must be deleted")
        self.assertEqual(result["remaining"], 2, "Fresh + malformed items must survive")


if __name__ == "__main__":
    unittest.main()
