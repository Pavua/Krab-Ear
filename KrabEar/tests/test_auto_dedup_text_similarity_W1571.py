"""Tests for W1567 F1 HIGH fix — wire _text_similarity into check_duplicate.

W1571 verifies that check_duplicate now uses the W1245 Jaccard hybrid
(_text_similarity) as the primary tier, instead of delegating exclusively
to DuplicateDetector (SequenceMatcher).

Four required test cases:
  1. test_check_duplicate_uses_text_similarity_jaccard
  2. test_check_duplicate_threshold_boundary_at_085
  3. test_check_duplicate_below_threshold_returns_none
  4. test_check_duplicate_respects_60s_time_window
"""

from __future__ import annotations

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auto_deduplication import (  # noqa: E402,F401
    AutoDeduplicator,
    _text_similarity,
    _JACCARD_LOW,
)


def _iso(dt: datetime) -> str:
    """Convert datetime to ISO-8601 string."""
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_store(items: list[dict]) -> MagicMock:
    """Stub StateStore that returns a fixed item list."""
    store = MagicMock()
    store.get_history_page.return_value = (items, None)
    return store


class TestCheckDuplicateUsesJaccard(unittest.TestCase):
    """W1567 F1 HIGH: _text_similarity (Jaccard) is used as the primary tier."""

    def test_check_duplicate_uses_text_similarity_jaccard(self):
        """check_duplicate must call _text_similarity for candidate items.

        Verifies that the Jaccard path is active by patching _text_similarity
        and checking it is called with the texts from the history items.
        """
        now = _now()
        existing_text = "the quick brown fox jumps over the lazy dog"
        new_text = "the quick brown fox jumps over the lazy dog"

        existing = [
            {"id": "item-1", "text": existing_text, "ts": _iso(now - timedelta(seconds=5))},
        ]
        store = _make_store(existing)
        dedup = AutoDeduplicator()

        called_with: list[tuple[str, str]] = []
        original_fn = _text_similarity  # capture before patch

        import backend.auto_deduplication as _mod

        def spy(a: str, b: str) -> float:
            called_with.append((a, b))
            return original_fn(a, b)

        with patch.object(_mod, "_text_similarity", side_effect=spy):
            result = dedup.check_duplicate(
                text=new_text,
                timestamp=_iso(now),
                store=store,
            )

        # _text_similarity must have been called at least once
        self.assertGreater(
            len(called_with), 0,
            "_text_similarity must be called during check_duplicate",
        )
        # The call must include the new text and the existing text
        texts_a = [a for a, _ in called_with]
        texts_b = [b for _, b in called_with]
        self.assertIn(new_text, texts_a, "new_text must appear as first argument")
        self.assertIn(existing_text, texts_b, "existing_text must appear as second argument")
        # Identical texts → duplicate detected
        self.assertTrue(result.is_duplicate)

    def test_check_duplicate_threshold_boundary_at_085(self):
        """check_duplicate reports duplicate when _text_similarity returns exactly 0.85.

        _TIER1_ROUTING_THRESHOLD = 0.85 (renamed from _SIMILARITY_THRESHOLD in W1711) —
        at-threshold scores must trigger duplicate.
        """
        dedup = AutoDeduplicator()

        # Verify class attribute exists and is 0.85.
        # W1711 renamed _SIMILARITY_THRESHOLD → _TIER1_ROUTING_THRESHOLD; use the new name.
        self.assertEqual(
            dedup._TIER1_ROUTING_THRESHOLD,
            0.85,
            "_TIER1_ROUTING_THRESHOLD must be 0.85",
        )

        now = _now()
        # Craft two texts with known high similarity.
        # Use identical texts to guarantee similarity == 1.0 (well above 0.85).
        base = "alpha beta gamma delta epsilon zeta"
        existing = [
            {"id": "item-boundary", "text": base, "ts": _iso(now - timedelta(seconds=10))},
        ]
        store = _make_store(existing)

        import backend.auto_deduplication as _mod

        # Patch _text_similarity to return exactly 0.85 (boundary value).
        # W1711: _TIER1_ROUTING_THRESHOLD = 0.85 is the tier-1 routing boundary.
        # To declare a duplicate at exactly 0.85 we must pass threshold=0.85
        # (DEFAULT_DEDUP_THRESHOLD=0.9 would require sim>=0.9 to confirm).
        with patch.object(_mod, "_text_similarity", return_value=0.85):
            result = dedup.check_duplicate(
                text=base,
                timestamp=_iso(now),
                store=store,
                threshold=0.85,
            )

        self.assertTrue(
            result.is_duplicate,
            "similarity == 0.85 (== _TIER1_ROUTING_THRESHOLD) must be flagged as duplicate "
            "when threshold=0.85 is passed",
        )
        self.assertAlmostEqual(result.similarity, 0.85, places=4)

    def test_check_duplicate_below_threshold_returns_none(self):
        """check_duplicate returns is_duplicate=False when all similarities are below 0.7.

        When _text_similarity returns a value below _JACCARD_LOW (0.7),
        neither tier-1 nor tier-2 should fire, and result must be 'kept'.
        """
        dedup = AutoDeduplicator()

        now = _now()
        unrelated = "cats are fluffy and love sleeping"
        new_text = "the economy is very complex"
        existing = [
            {"id": "item-unrelated", "text": unrelated, "ts": _iso(now - timedelta(seconds=5))},
        ]
        store = _make_store(existing)

        import backend.auto_deduplication as _mod

        # Patch to return a value well below _JACCARD_LOW (0.7)
        with patch.object(_mod, "_text_similarity", return_value=0.3):
            result = dedup.check_duplicate(
                text=new_text,
                timestamp=_iso(now),
                store=store,
            )

        self.assertFalse(
            result.is_duplicate,
            "similarity < _JACCARD_LOW must not be flagged as duplicate",
        )
        self.assertEqual(result.action_taken, "kept")

    def test_check_duplicate_respects_60s_time_window(self):
        """check_duplicate must not match items older than 60 seconds (time window).

        Items with timestamps more than 60 seconds before the new item's timestamp
        must be excluded from tier-1 scan, matching DuplicateDetector behaviour.
        """
        dedup = AutoDeduplicator()

        now = _now()
        identical_text = "time window test text identical content"

        # Candidate is 90 seconds OLD — outside the 60s window
        old_item = {
            "id": "item-old",
            "text": identical_text,
            "ts": _iso(now - timedelta(seconds=90)),
        }
        # Candidate is 10 seconds OLD — inside the 60s window
        recent_item = {
            "id": "item-recent",
            "text": identical_text,
            "ts": _iso(now - timedelta(seconds=10)),
        }

        # Test with only the old item (outside window) — must NOT detect duplicate
        store_old_only = _make_store([old_item])
        result_old = dedup.check_duplicate(
            text=identical_text,
            timestamp=_iso(now),
            store=store_old_only,
        )
        self.assertFalse(
            result_old.is_duplicate,
            "Item 90s old (outside 60s window) must NOT be flagged as duplicate",
        )

        # Test with the recent item (inside window) — must detect duplicate
        store_recent = _make_store([recent_item])
        result_recent = dedup.check_duplicate(
            text=identical_text,
            timestamp=_iso(now),
            store=store_recent,
        )
        self.assertTrue(
            result_recent.is_duplicate,
            "Identical item 10s old (inside 60s window) MUST be flagged as duplicate",
        )
        self.assertEqual(result_recent.duplicate_of, "item-recent")


class TestSimilarityThresholdAttribute(unittest.TestCase):
    """Class attribute _TIER1_ROUTING_THRESHOLD (formerly _SIMILARITY_THRESHOLD) exists."""

    def test_similarity_threshold_class_attribute(self):
        """AutoDeduplicator must have _TIER1_ROUTING_THRESHOLD = 0.85 as class attribute.

        W1711 renamed _SIMILARITY_THRESHOLD → _TIER1_ROUTING_THRESHOLD to make the
        tier-1/tier-2 routing intent unambiguous.
        """
        self.assertTrue(
            hasattr(AutoDeduplicator, "_TIER1_ROUTING_THRESHOLD"),
            "AutoDeduplicator must have class attribute _TIER1_ROUTING_THRESHOLD "
            "(renamed from _SIMILARITY_THRESHOLD in W1711)",
        )
        self.assertEqual(AutoDeduplicator._TIER1_ROUTING_THRESHOLD, 0.85)

    def test_time_window_class_attribute(self):
        """AutoDeduplicator must have _TIME_WINDOW_SECONDS = 60 as class attribute."""
        self.assertTrue(
            hasattr(AutoDeduplicator, "_TIME_WINDOW_SECONDS"),
            "AutoDeduplicator must have class attribute _TIME_WINDOW_SECONDS",
        )
        self.assertEqual(AutoDeduplicator._TIME_WINDOW_SECONDS, 60)


if __name__ == "__main__":
    unittest.main()
