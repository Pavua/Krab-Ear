"""Тесты контрактных моделей событий Krab Ear."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timezone

from backend.event_bus import EventBus
from contracts.stt_events import SttPartial, SttFinal, SttFailed
from contracts.translation_events import TranslationCompleted, TranslationFailed
from contracts.registry import EventType, EVENT_SCHEMA_MAP
from contracts.envelope import KrabEventEnvelope, parse_event, parse_and_validate, UnknownEventType
from pydantic import ValidationError


class SttPartialTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttPartial(text="hello")
        self.assertEqual(e.text, "hello")
        self.assertIsNone(e.duration_sec)

    def test_valid_full(self):
        e = SttPartial(text="hello", duration_sec=1.5)
        self.assertEqual(e.duration_sec, 1.5)

    def test_missing_text_raises(self):
        with self.assertRaises(ValidationError):
            SttPartial()


class SttFinalTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttFinal(history_id="abc-123", text="hello world", duration_sec=2.3)
        self.assertEqual(e.history_id, "abc-123")
        self.assertEqual(e.text, "hello world")
        self.assertEqual(e.duration_sec, 2.3)
        self.assertIsNone(e.language)
        self.assertIsNone(e.confidence)
        self.assertEqual(e.segments, [])

    def test_valid_full(self):
        e = SttFinal(
            history_id="abc-123",
            text="hello",
            duration_sec=2.3,
            language="ru",
            confidence=0.95,
            segments=[{"start": 0.0, "end": 1.0, "text": "hello"}],
        )
        self.assertEqual(e.language, "ru")
        self.assertEqual(e.confidence, 0.95)
        self.assertEqual(len(e.segments), 1)

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            SttFinal(text="hello")  # missing history_id, duration_sec


class SttFailedTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = SttFailed(reason="timeout")
        self.assertEqual(e.reason, "timeout")
        self.assertEqual(e.duration_sec, 0.0)

    def test_valid_with_duration(self):
        e = SttFailed(reason="model_unavailable", duration_sec=1.2)
        self.assertEqual(e.duration_sec, 1.2)

    def test_missing_reason_raises(self):
        with self.assertRaises(ValidationError):
            SttFailed()


class TranslationCompletedTest(unittest.TestCase):

    def test_valid(self):
        e = TranslationCompleted(
            history_id="abc-123",
            source_text="hola mundo",
            translated_text="hello world",
            source_lang="es",
            target_lang="en",
            engine="local",
            mode="es_en",
        )
        self.assertEqual(e.source_text, "hola mundo")
        self.assertEqual(e.translated_text, "hello world")
        self.assertEqual(e.engine, "local")

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            TranslationCompleted(
                history_id="abc",
                source_text="hola",
            )


class TranslationFailedTest(unittest.TestCase):

    def test_valid_minimal(self):
        e = TranslationFailed(source_text="hola", reason="engine_unavailable")
        self.assertEqual(e.reason, "engine_unavailable")
        self.assertIsNone(e.history_id)
        self.assertIsNone(e.source_lang)

    def test_valid_full(self):
        e = TranslationFailed(
            history_id="abc-123",
            source_text="hola",
            reason="timeout",
            source_lang="es",
            target_lang="en",
        )
        self.assertEqual(e.history_id, "abc-123")

    def test_missing_required_raises(self):
        with self.assertRaises(ValidationError):
            TranslationFailed()


class EventRegistryTest(unittest.TestCase):

    def test_all_event_types_have_schema(self):
        """Каждый EventType имеет маппинг в EVENT_SCHEMA_MAP."""
        for etype in EventType:
            self.assertIn(etype, EVENT_SCHEMA_MAP, f"{etype.value} missing from EVENT_SCHEMA_MAP")

    def test_no_orphan_schemas(self):
        """Нет записей в EVENT_SCHEMA_MAP без EventType."""
        for key in EVENT_SCHEMA_MAP:
            self.assertIn(key, EventType.__members__.values())

    def test_event_type_values(self):
        self.assertEqual(EventType.STT_PARTIAL.value, "stt.partial")
        self.assertEqual(EventType.STT_FINAL.value, "stt.final")
        self.assertEqual(EventType.STT_FAILED.value, "stt.failed")
        self.assertEqual(EventType.TRANSLATION_COMPLETED.value, "translation.completed")
        self.assertEqual(EventType.TRANSLATION_FAILED.value, "translation.failed")

    def test_schema_map_types(self):
        self.assertIs(EVENT_SCHEMA_MAP[EventType.STT_FINAL], SttFinal)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.STT_FAILED], SttFailed)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.STT_PARTIAL], SttPartial)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.TRANSLATION_COMPLETED], TranslationCompleted)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.TRANSLATION_FAILED], TranslationFailed)


class EnvelopeTest(unittest.TestCase):

    def test_valid_envelope(self):
        now = datetime.now(timezone.utc)
        e = KrabEventEnvelope(type="stt.final", ts=now, data={"text": "hi"})
        self.assertEqual(e.type, "stt.final")
        self.assertEqual(e.data, {"text": "hi"})

    def test_missing_type_raises(self):
        with self.assertRaises(ValidationError):
            KrabEventEnvelope(ts=datetime.now(timezone.utc), data={})


class ParseEventTest(unittest.TestCase):

    def test_parse_valid(self):
        raw = {
            "type": "stt.final",
            "ts": "2026-04-06T12:00:00+00:00",
            "data": {"history_id": "x", "text": "hi", "duration_sec": 1.0},
        }
        env = parse_event(raw)
        self.assertEqual(env.type, "stt.final")

    def test_parse_invalid_raises(self):
        with self.assertRaises(ValidationError):
            parse_event({"data": {}})  # missing type and ts


class ParseAndValidateTest(unittest.TestCase):

    def test_known_event(self):
        raw = {
            "type": "stt.failed",
            "ts": "2026-04-06T12:00:00+00:00",
            "data": {"reason": "timeout"},
        }
        etype, payload = parse_and_validate(raw)
        self.assertEqual(etype, EventType.STT_FAILED)
        self.assertIsInstance(payload, SttFailed)
        self.assertEqual(payload.reason, "timeout")

    def test_unknown_event_raises(self):
        raw = {
            "type": "tts.completed",
            "ts": "2026-04-06T12:00:00+00:00",
            "data": {"audio_url": "file.mp3"},
        }
        with self.assertRaises(UnknownEventType):
            parse_and_validate(raw)

    def test_known_event_bad_data_raises(self):
        raw = {
            "type": "stt.final",
            "ts": "2026-04-06T12:00:00+00:00",
            "data": {"wrong_field": "x"},
        }
        with self.assertRaises(ValidationError):
            parse_and_validate(raw)


class EventBusTypedEmitTest(unittest.TestCase):

    def test_emit_typed_creates_valid_envelope(self):
        bus = EventBus()
        q = bus.subscribe()
        payload = SttFailed(reason="timeout", duration_sec=1.5)
        bus.emit_typed(EventType.STT_FAILED, payload)
        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.failed")
        self.assertIn("ts", event)
        self.assertEqual(event["data"]["reason"], "timeout")
        self.assertEqual(event["data"]["duration_sec"], 1.5)
        bus.unsubscribe(q)

    def test_emit_typed_roundtrip_validates(self):
        """emit_typed output can be parsed back by parse_and_validate."""
        bus = EventBus()
        q = bus.subscribe()
        payload = SttFinal(
            history_id="abc", text="hello", duration_sec=2.0,
            language="en", confidence=0.9,
        )
        bus.emit_typed(EventType.STT_FINAL, payload)
        event = q.get_nowait()
        etype, parsed = parse_and_validate(event)
        self.assertEqual(etype, EventType.STT_FINAL)
        self.assertEqual(parsed.text, "hello")
        bus.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
