"""Tests for HistoryItem forward-compat sidecar (_extra) — W1228 F2 MED fix.

Verifies that unknown fields written by a newer binary survive round-trips
through from_dict → to_dict (the path taken by _compact_unlocked).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class HistoryItemExtraFieldPreservationTestCase(unittest.TestCase):
    """Unknown fields from a newer binary must survive compaction round-trips."""

    def _make_payload(self, **extra_kwargs):
        """Return a minimal valid payload dict with optional extra keys."""
        base = {
            "id": "test-id-1",
            "ts": "2026-05-26T10:00:00",
            "text": "hello world",
        }
        base.update(extra_kwargs)
        return base

    # ------------------------------------------------------------------
    # test_unknown_field_preserved_via_extra
    # ------------------------------------------------------------------
    def test_unknown_field_preserved_via_extra(self):
        """from_dict captures unknown keys into _extra without raising."""
        from backend.models import HistoryItem

        payload = self._make_payload(
            future_feature_flag=True,
            new_score=0.99,
            nested_new={"key": "val"},
        )
        item = HistoryItem.from_dict(payload)

        self.assertEqual(item._extra["future_feature_flag"], True)
        self.assertEqual(item._extra["new_score"], 0.99)
        self.assertEqual(item._extra["nested_new"], {"key": "val"})

    # ------------------------------------------------------------------
    # test_to_dict_merges_extra_back_in
    # ------------------------------------------------------------------
    def test_to_dict_merges_extra_back_in(self):
        """to_dict emits unknown fields at the top level, not nested under '_extra'."""
        from backend.models import HistoryItem

        payload = self._make_payload(
            future_feature_flag=True,
            new_score=0.99,
        )
        item = HistoryItem.from_dict(payload)
        output = item.to_dict()

        # Top-level presence
        self.assertIn("future_feature_flag", output)
        self.assertIn("new_score", output)
        self.assertEqual(output["future_feature_flag"], True)
        self.assertEqual(output["new_score"], 0.99)

        # Internal sidecar must NOT leak into the serialised form
        self.assertNotIn("_extra", output)

    # ------------------------------------------------------------------
    # test_compaction_preserves_unknown_fields
    # ------------------------------------------------------------------
    def test_compaction_preserves_unknown_fields(self):
        """Round-trip from_dict → to_dict (the compaction path) preserves unknowns.

        Simulates what _compact_unlocked does: reads NDJSON record as dict,
        reconstructs HistoryItem via from_dict, then serialises back to dict
        via to_dict.  The output dict must contain the unknown keys.
        """
        from backend.models import HistoryItem

        # Simulate a record written by a newer binary that added two new fields.
        original_record = json.dumps(self._make_payload(
            future_field_a="value_a",
            future_field_b=42,
        ))

        # Compaction step: deserialise then re-serialise.
        loaded = json.loads(original_record)
        item = HistoryItem.from_dict(loaded)
        reserialised = item.to_dict()

        # Unknown fields must survive the round-trip.
        self.assertIn("future_field_a", reserialised)
        self.assertIn("future_field_b", reserialised)
        self.assertEqual(reserialised["future_field_a"], "value_a")
        self.assertEqual(reserialised["future_field_b"], 42)

        # Core known fields must also be intact.
        self.assertEqual(reserialised["id"], "test-id-1")
        self.assertEqual(reserialised["text"], "hello world")

    # ------------------------------------------------------------------
    # test_known_fields_take_precedence_over_extra
    # ------------------------------------------------------------------
    def test_known_fields_take_precedence_over_extra(self):
        """If a future payload somehow shadows a known field in _extra, the known
        field's explicitly validated value wins in to_dict output."""
        from backend.models import HistoryItem

        # Construct an item with a known field and force a collision via _extra
        # by directly instantiating with _extra set.
        item = HistoryItem(
            id="id-x",
            ts="2026-05-26T10:00:00",
            text="canonical text",
            _extra={"text": "SHOULD NOT WIN", "unknown_key": "preserved"},
        )
        output = item.to_dict()

        # Known field must win.
        self.assertEqual(output["text"], "canonical text")
        # Unknown key is still preserved.
        self.assertEqual(output["unknown_key"], "preserved")
        # _extra must not appear in output.
        self.assertNotIn("_extra", output)

    # ------------------------------------------------------------------
    # Regression: no-extra items still serialise correctly
    # ------------------------------------------------------------------
    def test_no_extra_items_serialise_correctly(self):
        """Items without unknown fields must serialise identically to before."""
        from backend.models import HistoryItem

        item = HistoryItem.create(text="normal item")
        output = item.to_dict()

        self.assertEqual(output["text"], "normal item")
        self.assertNotIn("_extra", output)
        self.assertIn("id", output)
        self.assertIn("ts", output)

    def test_create_factory_has_empty_extra(self):
        """Items created via HistoryItem.create() start with empty _extra."""
        from backend.models import HistoryItem

        item = HistoryItem.create(text="created item")
        self.assertEqual(item._extra, {})

    def test_from_dict_no_unknown_keys_has_empty_extra(self):
        """Payloads with only known fields result in empty _extra."""
        from backend.models import HistoryItem

        payload = self._make_payload()  # only id, ts, text
        item = HistoryItem.from_dict(payload)
        self.assertEqual(item._extra, {})


if __name__ == "__main__":
    unittest.main()
