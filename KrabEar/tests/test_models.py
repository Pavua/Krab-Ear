"""Тесты для backend/models.py HistoryItem."""

from __future__ import annotations

import sys
import unittest
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


if __name__ == "__main__":
    unittest.main()
