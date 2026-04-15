"""Контрактные тесты совместимости Krab Ear ↔ Krab Core.

Проверяют что REST API ответы и SSE события соответствуют формату,
который ожидает Krab Core (Telegram-бот).

Контракт: ROADMAP_KRAB_EAR.md, раздел «Контракт интеграции».
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.stt_events import SttFinal, SttFailed  # noqa: E402
from contracts.envelope import KrabEventEnvelope, parse_and_validate  # noqa: E402
from contracts.registry import EventType, EVENT_SCHEMA_MAP  # noqa: E402
from backend.event_bus import EventBus  # noqa: E402


class SttFinalContractTest(unittest.TestCase):
    """Krab Core expects stt.final to have: text, confidence, duration_ms, engine, segments."""

    def test_required_fields_in_schema(self):
        schema = SttFinal.model_json_schema()
        required = schema.get("required", [])
        # Krab Core requires these fields
        for field in ["history_id", "text", "duration_sec"]:
            self.assertIn(field, required, f"{field} must be required in SttFinal")

    def test_optional_fields_present(self):
        schema = SttFinal.model_json_schema()
        props = schema["properties"]
        for field in ["language", "confidence", "segments"]:
            self.assertIn(field, props, f"{field} must be present in SttFinal schema")

    def test_serialization_produces_json_safe_types(self):
        event = SttFinal(
            history_id="test-123",
            text="hello world",
            duration_sec=2.5,
            language="en",
            confidence=0.95,
            segments=[{"start": 0.0, "end": 1.0, "text": "hello", "speaker": "SPEAKER_0"}],
        )
        data = event.model_dump(mode="json")
        self.assertIsInstance(data["history_id"], str)
        self.assertIsInstance(data["text"], str)
        self.assertIsInstance(data["duration_sec"], float)
        self.assertIsInstance(data["segments"], list)


class SttFailedContractTest(unittest.TestCase):
    """Krab Core expects stt.failed to have: reason."""

    def test_reason_is_required(self):
        schema = SttFailed.model_json_schema()
        required = schema.get("required", [])
        self.assertIn("reason", required)

    def test_serialization_format(self):
        event = SttFailed(reason="model_unavailable", duration_sec=0.5)
        data = event.model_dump(mode="json")
        self.assertIsInstance(data["reason"], str)
        self.assertIsInstance(data["duration_sec"], float)


class EventEnvelopeContractTest(unittest.TestCase):
    """All events must follow {type, ts, data} envelope format."""

    def test_envelope_has_required_fields(self):
        schema = KrabEventEnvelope.model_json_schema()
        required = schema.get("required", [])
        for field in ["type", "ts", "data"]:
            self.assertIn(field, required)

    def test_emitted_event_matches_envelope(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.emit_typed(EventType.STT_FINAL, SttFinal(
            history_id="x", text="hi", duration_sec=1.0,
        ))
        event = q.get_nowait()
        # Must have exactly {type, ts, data}
        self.assertEqual(set(event.keys()), {"type", "ts", "data"})
        self.assertEqual(event["type"], "stt.final")
        self.assertIsInstance(event["ts"], str)  # ISO 8601 string
        self.assertIsInstance(event["data"], dict)
        bus.unsubscribe(q)

    def test_emitted_event_roundtrips_through_parse(self):
        """Contract: emitted events must be parseable by consumers."""
        bus = EventBus()
        q = bus.subscribe()
        bus.emit_typed(EventType.STT_FAILED, SttFailed(reason="timeout"))
        event = q.get_nowait()
        etype, payload = parse_and_validate(event)
        self.assertEqual(etype, EventType.STT_FAILED)
        self.assertEqual(payload.reason, "timeout")
        bus.unsubscribe(q)


class SchemaMapCompletenessTest(unittest.TestCase):
    """All advertised events must have schemas."""

    def test_all_stt_events_registered(self):
        stt_types = [EventType.STT_PARTIAL, EventType.STT_FINAL, EventType.STT_FAILED]
        for t in stt_types:
            self.assertIn(t, EVENT_SCHEMA_MAP)

    def test_all_translation_events_registered(self):
        tr_types = [EventType.TRANSLATION_COMPLETED, EventType.TRANSLATION_FAILED]
        for t in tr_types:
            self.assertIn(t, EVENT_SCHEMA_MAP)

    def test_json_schema_exportable(self):
        """Contract: all schemas must be exportable as JSON Schema for cross-language consumers."""
        for etype, model_cls in EVENT_SCHEMA_MAP.items():
            schema = model_cls.model_json_schema()
            self.assertIn("properties", schema, f"{etype.value} schema has no properties")
            self.assertEqual(schema["type"], "object", f"{etype.value} schema type is not object")


if __name__ == "__main__":
    unittest.main()
