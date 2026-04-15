"""Тесты контрактов событий истории Krab Ear."""

from __future__ import annotations
from contracts.registry import EVENT_SCHEMA_MAP, EventType
from contracts.history_events import AutoSummaryEvent, MarkdownExportEvent

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestMarkdownExportEvent(unittest.TestCase):
    def test_valid_event(self):
        evt = MarkdownExportEvent(entries=10, chars=500, copy_to_clipboard=True)
        self.assertEqual(evt.entries, 10)
        self.assertEqual(evt.chars, 500)
        self.assertTrue(evt.copy_to_clipboard)

    def test_no_clipboard(self):
        evt = MarkdownExportEvent(entries=0, chars=0, copy_to_clipboard=False)
        self.assertFalse(evt.copy_to_clipboard)

    def test_serialization(self):
        evt = MarkdownExportEvent(entries=3, chars=120, copy_to_clipboard=False)
        data = json.loads(evt.model_dump_json())
        self.assertIn("entries", data)
        self.assertIn("chars", data)
        self.assertIn("copy_to_clipboard", data)

    def test_json_schema_export(self):
        schema = MarkdownExportEvent.model_json_schema()
        props = schema["properties"]
        self.assertIn("entries", props)
        self.assertIn("chars", props)
        self.assertIn("copy_to_clipboard", props)

    def test_missing_required_field_raises(self):
        with self.assertRaises(Exception):
            MarkdownExportEvent(entries=1, chars=10)  # missing copy_to_clipboard


class TestAutoSummaryEvent(unittest.TestCase):
    def test_valid_event(self):
        evt = AutoSummaryEvent(
            items_processed=5,
            total_words=200,
            fallback=False,
            summary="Short summary.",
        )
        self.assertEqual(evt.items_processed, 5)
        self.assertEqual(evt.total_words, 200)
        self.assertFalse(evt.fallback)
        self.assertEqual(evt.summary, "Short summary.")

    def test_fallback_true(self):
        evt = AutoSummaryEvent(
            items_processed=1, total_words=10, fallback=True, summary=""
        )
        self.assertTrue(evt.fallback)

    def test_serialization(self):
        evt = AutoSummaryEvent(
            items_processed=2, total_words=50, fallback=False, summary="ok"
        )
        data = json.loads(evt.model_dump_json())
        self.assertIn("items_processed", data)
        self.assertIn("total_words", data)
        self.assertIn("fallback", data)
        self.assertIn("summary", data)

    def test_json_schema_export(self):
        schema = AutoSummaryEvent.model_json_schema()
        props = schema["properties"]
        self.assertIn("items_processed", props)
        self.assertIn("total_words", props)
        self.assertIn("fallback", props)
        self.assertIn("summary", props)


class TestRegistryIntegration(unittest.TestCase):
    def test_markdown_export_in_schema_map(self):
        self.assertIn(EventType.MARKDOWN_EXPORT, EVENT_SCHEMA_MAP)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.MARKDOWN_EXPORT], MarkdownExportEvent)

    def test_auto_summary_in_schema_map(self):
        self.assertIn(EventType.AUTO_SUMMARY, EVENT_SCHEMA_MAP)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.AUTO_SUMMARY], AutoSummaryEvent)

    def test_event_type_values(self):
        self.assertEqual(EventType.MARKDOWN_EXPORT.value, "markdown_export")
        self.assertEqual(EventType.AUTO_SUMMARY.value, "auto_summary")

    def test_all_registered_schemas_exportable(self):
        for event_type, model_cls in EVENT_SCHEMA_MAP.items():
            schema = model_cls.model_json_schema()
            self.assertIn("properties", schema, f"Missing properties in {event_type}")


if __name__ == "__main__":
    unittest.main()
