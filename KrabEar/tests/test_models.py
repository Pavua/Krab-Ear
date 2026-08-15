"""Тесты для backend/models.py HistoryItem."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class HistoryItemLLMFieldsTestCase(unittest.TestCase):
    """Тесты новых D.10a полей HistoryItem."""

    def test_history_item_has_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(text="test")
        self.assertEqual(item.cleaned_text, "")
        self.assertFalse(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 0)

    def test_history_item_create_accepts_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(
            text="Привет, мир.",
            cleaned_text="привет мир",
            llm_applied=True,
            llm_latency_ms=1500,
        )
        self.assertEqual(item.text, "Привет, мир.")
        self.assertEqual(item.cleaned_text, "привет мир")
        self.assertTrue(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 1500)

    def test_history_item_to_dict_includes_llm_fields(self):
        from backend.models import HistoryItem
        item = HistoryItem.create(
            text="final",
            cleaned_text="cleaned",
            llm_applied=True,
            llm_latency_ms=1000,
        )
        d = item.to_dict()
        self.assertIn("cleaned_text", d)
        self.assertIn("llm_applied", d)
        self.assertIn("llm_latency_ms", d)

    def test_history_item_from_dict_handles_missing_llm_fields(self):
        """Backward compat: старые NDJSON записи без LLM полей должны загружаться с дефолтами."""
        from backend.models import HistoryItem
        legacy_payload = {
            "id": "abc",
            "ts": "2026-04-01T10:00:00",
            "text": "legacy entry",
        }
        item = HistoryItem.from_dict(legacy_payload)
        self.assertEqual(item.text, "legacy entry")
        self.assertEqual(item.cleaned_text, "")
        self.assertFalse(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 0)

    def test_history_item_from_dict_loads_llm_fields(self):
        from backend.models import HistoryItem
        payload = {
            "id": "abc",
            "ts": "2026-04-09T10:00:00",
            "text": "Привет, мир.",
            "cleaned_text": "привет мир",
            "llm_applied": True,
            "llm_latency_ms": 1500,
        }
        item = HistoryItem.from_dict(payload)
        self.assertEqual(item.cleaned_text, "привет мир")
        self.assertTrue(item.llm_applied)
        self.assertEqual(item.llm_latency_ms, 1500)


class TzAwareTimestampTestCase(unittest.TestCase):
    """W1671: tz-aware UTC timestamps + backward-compat helpers."""

    def test_history_item_timestamp_is_tz_aware(self):
        """HistoryItem.create() должен производить UTC-aware timestamp (+00:00)."""
        from backend.models import HistoryItem
        item = HistoryItem.create(text="тест")
        self.assertTrue(
            item.ts.endswith("+00:00"),
            f"Expected +00:00 suffix, got: {item.ts!r}",
        )
        # Must parse as tz-aware datetime
        dt = datetime.fromisoformat(item.ts)
        self.assertIsNotNone(dt.tzinfo, "Parsed datetime should be tz-aware")
        self.assertEqual(dt.utcoffset().total_seconds(), 0)

    def test_timestamp_lexicographic_sort_still_works(self):
        """Два tz-aware UTC timestamp-а должны сортироваться лексикографически корректно."""
        from backend.models import HistoryItem
        from datetime import timedelta
        item_a = HistoryItem.create(text="первый")
        # Simulate a strictly later timestamp via timedelta to avoid modulo 60 rollover bug
        dt_a = datetime.fromisoformat(item_a.ts)
        dt_b_aware = dt_a + timedelta(seconds=1)
        ts_b = dt_b_aware.isoformat(timespec="seconds")

        # Both end in +00:00 — strip suffix to get naive str for lex comparison
        ts_a_naive = item_a.ts[:-6] if item_a.ts.endswith("+00:00") else item_a.ts
        ts_b_naive = ts_b[:-6] if ts_b.endswith("+00:00") else ts_b

        # Lexicographic order should reflect chronological order
        self.assertLessEqual(ts_a_naive, ts_b_naive)

    def test_naive_legacy_timestamp_parse_compat(self):
        """StateStore._parse_ts_to_naive_utc() корректно обрабатывает tz-naive legacy timestamps."""
        from backend.state_store import StateStore

        # Legacy naive timestamp (pre-W1671)
        naive_ts = "2026-01-15T14:30:00"
        result = StateStore._parse_ts_to_naive_utc(naive_ts)
        self.assertIsNone(result.tzinfo, "Result should be tz-naive")
        self.assertEqual(result, datetime(2026, 1, 15, 14, 30, 0))

        # New tz-aware UTC timestamp (W1671+)
        aware_ts = "2026-01-15T14:30:00+00:00"
        result2 = StateStore._parse_ts_to_naive_utc(aware_ts)
        self.assertIsNone(result2.tzinfo, "Result should be tz-naive")
        self.assertEqual(result2, datetime(2026, 1, 15, 14, 30, 0))

        # Both parse to the same naive UTC datetime
        self.assertEqual(result, result2)

    def test_ts_to_naive_utc_str_strips_offset(self):
        """StateStore._ts_to_naive_utc_str() должен убирать +00:00 для смешанных сравнений."""
        from backend.state_store import StateStore

        aware = "2026-05-29T12:00:00+00:00"
        naive = "2026-05-29T12:00:00"
        self.assertEqual(StateStore._ts_to_naive_utc_str(aware), naive)
        self.assertEqual(StateStore._ts_to_naive_utc_str(naive), naive)

        # Z suffix
        z_ts = "2026-05-29T12:00:00Z"
        self.assertEqual(StateStore._ts_to_naive_utc_str(z_ts), naive)


if __name__ == "__main__":
    unittest.main()
