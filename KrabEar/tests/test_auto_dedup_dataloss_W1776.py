"""Regression tests — W1776: tier-1 bag-of-words data-loss + run_deduplication ts ordering.

These tests FAIL on the pre-W1776 code and PASS after the fix.

HIGH DATA-LOSS (check_duplicate tier-1):
  The tier-1 similarity score is Jaccard over lowercased WORD SETS — it is order-
  AND multiplicity-insensitive.  Two DISTINCT recordings that share the same bag of
  words score 1.0:
    'собака укусила человека' vs 'человека укусила собака' → Jaccard 1.0
    'купи хлеб молоко яйца'   vs 'купи яйца молоко хлеб'   → Jaccard 1.0
    'dog bit man'             vs 'man bit dog'             → Jaccard 1.0
  With auto_dedup_enabled=True, recording_core_service early-returns on
  is_duplicate=True → the recording is NEVER persisted (no add_history_item, no
  .md, no tombstone) = irreversible data loss of a DISTINCT recording.

  Fix: Jaccard is now only a cheap PRE-filter; the final tier-1 duplicate decision
  is confirmed by an ORDER-SENSITIVE difflib.SequenceMatcher ratio meeting the
  caller's threshold.  Order-swapped distinct texts are NOT flagged.  Genuine
  near-duplicates (same text + minor edits) are STILL flagged.

MED (run_deduplication):
  Original/duplicate were picked by PAGE POSITION (group.items[0]).  get_history_page
  returns items newest-first, so the OLDER true-original was de-indexed and the newer
  copy kept.  Fix: pick the original deterministically by timestamp — sort the group
  ascending and treat the EARLIEST as the original.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.auto_deduplication import (  # noqa: E402
    AutoDeduplicator,
    DEFAULT_DEDUP_THRESHOLD,
    _parse_ts_for_sort,
)
from backend.state_store import StateStore  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _make_store(items: list[dict]) -> MagicMock:
    """Stub StateStore that returns a fixed item list from get_history_page."""
    store = MagicMock()
    store.get_history_page.return_value = (items, None)
    return store


# ---------------------------------------------------------------------------
# HIGH: order-swapped distinct texts are NOT flagged as duplicate
# ---------------------------------------------------------------------------
class OrderSwapNotDuplicateW1776TestCase(unittest.TestCase):
    """Word-order swaps (same bag of words, different meaning) must NOT be duplicates."""

    # (new_text, existing_text) — same word set, different order, DISTINCT meaning
    ORDER_SWAP_PAIRS = [
        ("dog bit man", "man bit dog"),
        ("собака укусила человека", "человека укусила собака"),
        ("купи хлеб молоко яйца", "купи яйца молоко хлеб"),
    ]

    def test_order_swap_not_flagged_default_threshold(self) -> None:
        """Order-swapped distinct texts: is_duplicate=False at DEFAULT threshold (0.9).

        PRE-FIX: Jaccard==1.0 short-circuits to is_duplicate=True → recording dropped.
        POST-FIX: order-sensitive SequenceMatcher ratio (~0.45–0.65) < 0.9 → kept.
        """
        now = _now()
        for new_text, existing_text in self.ORDER_SWAP_PAIRS:
            with self.subTest(pair=(new_text, existing_text)):
                existing = [{
                    "id": "orig-1",
                    "text": existing_text,
                    "ts": _iso(now - timedelta(seconds=5)),
                }]
                store = _make_store(existing)
                dedup = AutoDeduplicator()

                result = dedup.check_duplicate(
                    text=new_text,
                    timestamp=_iso(now),
                    store=store,
                    threshold=DEFAULT_DEDUP_THRESHOLD,
                )

                self.assertFalse(
                    result.is_duplicate,
                    f"W1776 DATA-LOSS: order-swap {new_text!r} vs {existing_text!r} "
                    f"must NOT be flagged duplicate (got is_duplicate=True, "
                    f"action={result.action_taken}, sim={result.similarity:.3f}). "
                    f"Jaccard bag-of-words match dropped a DISTINCT recording.",
                )
                self.assertEqual(result.action_taken, "kept")

    def test_order_swap_not_flagged_low_threshold(self) -> None:
        """Even at a low threshold (0.8) order swaps stay below the SequenceMatcher gate.

        The order-sensitive ratios for these pairs are ~0.45–0.65, so a 0.8 threshold
        must still keep them.  This proves the gate is genuinely order-sensitive and
        not merely riding on the high DEFAULT threshold.
        """
        now = _now()
        for new_text, existing_text in self.ORDER_SWAP_PAIRS:
            with self.subTest(pair=(new_text, existing_text)):
                existing = [{
                    "id": "orig-low",
                    "text": existing_text,
                    "ts": _iso(now - timedelta(seconds=5)),
                }]
                store = _make_store(existing)
                dedup = AutoDeduplicator()

                result = dedup.check_duplicate(
                    text=new_text,
                    timestamp=_iso(now),
                    store=store,
                    threshold=0.8,
                )
                self.assertFalse(
                    result.is_duplicate,
                    f"order-swap {new_text!r} vs {existing_text!r} must not be a "
                    f"duplicate even at threshold=0.8 (sim={result.similarity:.3f})",
                )


# ---------------------------------------------------------------------------
# HIGH: genuine near-duplicates ARE still flagged
# ---------------------------------------------------------------------------
class GenuineDuplicateStillFlaggedW1776TestCase(unittest.TestCase):
    """The fix must not regress true-positive detection."""

    def test_identical_text_still_flagged(self) -> None:
        """Identical text in window → is_duplicate=True (regression guard)."""
        now = _now()
        text = "это полностью идентичный текст транскрипции для проверки"
        existing = [{
            "id": "dup-1",
            "text": text,
            "ts": _iso(now - timedelta(seconds=8)),
        }]
        store = _make_store(existing)
        dedup = AutoDeduplicator()

        result = dedup.check_duplicate(
            text=text,
            timestamp=_iso(now),
            store=store,
            threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        self.assertTrue(
            result.is_duplicate,
            "Identical text must still be flagged as a duplicate after W1776",
        )
        self.assertEqual(result.duplicate_of, "dup-1")

    def test_minor_edit_still_flagged(self) -> None:
        """Same text + a single minor edit (one extra word) → still a duplicate.

        SequenceMatcher ratio for a near-identical prefix-superset stays well above
        the DEFAULT threshold, so the order-sensitive gate confirms it.
        """
        now = _now()
        base = "привет это тестовая транскрипция речи для проверки дедупликации сигнала"
        edited = base + " дополнительно"
        existing = [{
            "id": "dup-2",
            "text": base,
            "ts": _iso(now - timedelta(seconds=6)),
        }]
        store = _make_store(existing)
        dedup = AutoDeduplicator()

        result = dedup.check_duplicate(
            text=edited,
            timestamp=_iso(now),
            store=store,
            threshold=DEFAULT_DEDUP_THRESHOLD,
        )
        self.assertTrue(
            result.is_duplicate,
            "A genuine near-duplicate (same text + one extra word) must still be "
            "flagged after W1776",
        )
        self.assertEqual(result.duplicate_of, "dup-2")

    def test_minor_edit_flagged_via_real_store(self) -> None:
        """End-to-end through a real StateStore: genuine near-duplicate still flagged."""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            store = StateStore(data_dir)

            base = "одинаковая запись звонка про работу и результат данных вывод"
            store.add_history_item(text=base, paste_status="ok")

            dedup = AutoDeduplicator()
            result = dedup.check_duplicate(
                text=base + " ещё",
                timestamp=_now().isoformat(),
                store=store,
                threshold=DEFAULT_DEDUP_THRESHOLD,
            )
            self.assertTrue(
                result.is_duplicate,
                "Near-identical text via real StateStore must still be a duplicate",
            )


# ---------------------------------------------------------------------------
# MED: run_deduplication keeps the EARLIEST-ts item as original
# ---------------------------------------------------------------------------
class RunDeduplicationOriginalByTimestampW1776TestCase(unittest.TestCase):
    """run_deduplication must pick the original by timestamp, not page position."""

    def _make_paged_store(self, items: list[dict]) -> MagicMock:
        """Return a store whose get_history_page yields the items (single page)."""
        store = MagicMock()
        store.get_history_page.return_value = (list(items), None)
        return store

    def test_original_is_earliest_ts_not_page_position(self) -> None:
        """The OLDER (earliest-ts) record is the original; newer ones are duplicates.

        get_history_page returns items newest-first.  PRE-FIX code took group.items[0]
        (the NEWEST) as original; POST-FIX sorts by ts ascending → earliest is original.
        """
        base_text = "идентичный дубль текст для проверки выбора оригинала по времени"
        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Page is newest-first (as get_history_page returns): newest at index 0.
        newest = {"id": "id-newest", "text": base_text, "ts": _iso(t0 + timedelta(seconds=20))}
        middle = {"id": "id-middle", "text": base_text, "ts": _iso(t0 + timedelta(seconds=10))}
        oldest = {"id": "id-oldest", "text": base_text, "ts": _iso(t0)}

        store = self._make_paged_store([newest, middle, oldest])
        dedup = AutoDeduplicator()

        result = dedup.run_deduplication(store=store, threshold=0.9)

        self.assertGreater(result["duplicate_groups"], 0, "expected a duplicate group")
        entry = result["duplicates"][0]
        self.assertEqual(
            entry["original_id"],
            "id-oldest",
            f"W1776 MED: original must be the earliest-ts record 'id-oldest', "
            f"got {entry['original_id']!r}. The older true-original was de-indexed.",
        )
        # The two newer copies are the duplicates.
        self.assertEqual(set(entry["duplicate_ids"]), {"id-newest", "id-middle"})
        self.assertNotIn("id-oldest", entry["duplicate_ids"])

    def test_original_earliest_when_ts_missing_sorts_oldest(self) -> None:
        """A record with a missing ts sorts as oldest (epoch 0) → it is the original.

        Both records sit near the epoch-0 placeholder window so they group together
        (DuplicateDetector enforces a 60s window).  The one with a real (later) ts
        must be the duplicate; the missing-ts one (epoch 0) is the original.
        """
        base_text = "запись без метки времени трактуется как самая старая оригинал"
        # Use a ts a few seconds after the missing-ts placeholder (1970 epoch) so both
        # land inside the 60s grouping window.
        epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        with_ts = {"id": "id-with-ts", "text": base_text, "ts": _iso(epoch + timedelta(seconds=5))}
        no_ts = {"id": "id-no-ts", "text": base_text}  # missing ts → epoch 0 → oldest

        # Page order: newest-first → with_ts (5s) before no_ts (epoch 0).
        store = self._make_paged_store([with_ts, no_ts])
        dedup = AutoDeduplicator()
        result = dedup.run_deduplication(store=store, threshold=0.9)

        self.assertGreater(result["duplicate_groups"], 0)
        entry = result["duplicates"][0]
        self.assertEqual(
            entry["original_id"],
            "id-no-ts",
            "Record with missing ts must sort as oldest (epoch 0) and be the original",
        )
        self.assertEqual(entry["duplicate_ids"], ["id-with-ts"])

    def test_parse_ts_helper_orders_correctly(self) -> None:
        """_parse_ts_for_sort: ISO strings parse, missing/empty/garbage → 0.0."""
        t = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            _parse_ts_for_sort({"ts": t.isoformat()}), t.timestamp(), places=3
        )
        self.assertEqual(_parse_ts_for_sort({}), 0.0)
        self.assertEqual(_parse_ts_for_sort({"ts": ""}), 0.0)
        self.assertEqual(_parse_ts_for_sort({"ts": "not-a-date"}), 0.0)
        self.assertEqual(_parse_ts_for_sort({"ts": 1700000000}), 1700000000.0)


if __name__ == "__main__":
    unittest.main()
