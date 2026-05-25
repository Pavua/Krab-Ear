"""Тесты контрактных моделей событий Krab Ear."""

from __future__ import annotations
from pydantic import ValidationError
from contracts.envelope import KrabEventEnvelope, parse_event, parse_and_validate, UnknownEventType
from contracts.registry import EventType, EVENT_SCHEMA_MAP
from contracts.translation_events import TranslationCompleted, TranslationFailed
from contracts.stt_events import SttPartial, SttFinal, SttFailed
from contracts.history_events import MarkdownExportEvent, AutoSummaryEvent
from contracts.hotword_events import HotwordDetected, HotwordMatch
from backend.event_bus import EventBus
from datetime import datetime, timezone

import json
import tempfile
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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


class HistoryEventsTest(unittest.TestCase):
    """Tests for history_events.py — MarkdownExportEvent + AutoSummaryEvent."""

    def test_markdown_export_event_valid(self):
        ev = MarkdownExportEvent(entries=5, chars=1024, copy_to_clipboard=True)
        self.assertEqual(ev.entries, 5)
        self.assertEqual(ev.chars, 1024)
        self.assertTrue(ev.copy_to_clipboard)

    def test_markdown_export_event_missing_chars_raises(self):
        with self.assertRaises(ValidationError):
            MarkdownExportEvent(entries=3, copy_to_clipboard=False)

    def test_auto_summary_event_valid(self):
        ev = AutoSummaryEvent(
            items_processed=10,
            total_words=500,
            fallback=False,
            summary="Session summary.",
        )
        self.assertEqual(ev.items_processed, 10)
        self.assertFalse(ev.fallback)
        self.assertEqual(ev.summary, "Session summary.")

    def test_auto_summary_event_missing_summary_raises(self):
        with self.assertRaises(ValidationError):
            AutoSummaryEvent(items_processed=1, total_words=10, fallback=True)

    def test_markdown_export_event_roundtrip(self):
        """dict -> MarkdownExportEvent -> dict preserves values."""
        ev = MarkdownExportEvent.model_validate(
            {"entries": 2, "chars": 128, "copy_to_clipboard": False}
        )
        dumped = ev.model_dump()
        self.assertEqual(dumped["entries"], 2)
        self.assertFalse(dumped["copy_to_clipboard"])


class HotwordEventsTest(unittest.TestCase):
    """Tests for hotword_events.py — HotwordMatch + HotwordDetected."""

    def test_hotword_match_valid(self):
        m = HotwordMatch(word="краб", position=0, category="trigger", context="краб слышит")
        self.assertEqual(m.word, "краб")
        self.assertEqual(m.position, 0)
        self.assertEqual(m.category, "trigger")

    def test_hotword_match_missing_position_raises(self):
        with self.assertRaises(ValidationError):
            HotwordMatch(word="краб", category="trigger", context="ctx")

    def test_hotword_detected_with_matches(self):
        ev = HotwordDetected(
            history_id="hid-42",
            text="встреча в среду",
            matches=[
                HotwordMatch(word="встреча", position=0, category="meeting", context="встреча в среду"),
            ],
        )
        self.assertEqual(len(ev.matches), 1)
        self.assertEqual(ev.matches[0].category, "meeting")
        self.assertEqual(ev.history_id, "hid-42")

    def test_hotword_detected_empty_matches(self):
        ev = HotwordDetected(history_id="hid-0", text="silence", matches=[])
        self.assertEqual(ev.matches, [])

    def test_hotword_detected_missing_text_raises(self):
        with self.assertRaises(ValidationError):
            HotwordDetected(history_id="hid-1", matches=[])

    def test_hotword_detected_roundtrip(self):
        """parse_and_validate works end-to-end for hotword.detected."""
        raw = {
            "type": "hotword.detected",
            "ts": "2026-04-20T10:00:00+00:00",
            "data": {
                "history_id": "hid-99",
                "text": "тест краб",
                "matches": [
                    {"word": "краб", "position": 5, "category": "trigger", "context": "тест краб"}
                ],
            },
        }
        etype, payload = parse_and_validate(raw)
        self.assertIs(etype, EventType.HOTWORD_DETECTED)
        self.assertIsInstance(payload, HotwordDetected)
        self.assertEqual(payload.matches[0].word, "краб")


class SchemaExportTest(unittest.TestCase):

    def test_export_creates_schema_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            from contracts.export import export_schemas
            export_schemas(out)
            expected_files = [
                "stt.partial.schema.json",
                "stt.final.schema.json",
                "stt.failed.schema.json",
                "translation.completed.schema.json",
                "translation.failed.schema.json",
            ]
            for fname in expected_files:
                fpath = out / fname
                self.assertTrue(fpath.exists(), f"Missing {fname}")
                data = json.loads(fpath.read_text())
                self.assertIn("properties", data)
                self.assertEqual(data["type"], "object")


# ---------------------------------------------------------------------------
# Wave 162 — additional required tests
# ---------------------------------------------------------------------------

class LiveSubsEventPayloadTest(unittest.TestCase):
    """test_live_subs_event_payload_valid — LiveSubsResult payload validation."""

    def test_live_subs_event_payload_valid(self):
        from contracts.live_subs_events import LiveSubsResult
        ev = LiveSubsResult(
            text="Привет, мир",
            translation="Hola, mundo",
            start_ts=0.0,
            end_ts=3.5,
            language_detected="ru",
        )
        self.assertEqual(ev.text, "Привет, мир")
        self.assertEqual(ev.translation, "Hola, mundo")
        self.assertAlmostEqual(ev.end_ts, 3.5)
        self.assertEqual(ev.language_detected, "ru")

    def test_live_subs_event_payload_minimal(self):
        """translation and language_detected are optional."""
        from contracts.live_subs_events import LiveSubsResult
        ev = LiveSubsResult(text="hello", start_ts=0.0, end_ts=1.0)
        self.assertIsNone(ev.translation)
        self.assertIsNone(ev.language_detected)

    def test_live_subs_missing_required_raises(self):
        from contracts.live_subs_events import LiveSubsResult
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            LiveSubsResult(text="hi")  # missing start_ts and end_ts

    def test_live_subs_schema_map_entry(self):
        """LIVE_SUBS_RESULT is in EVENT_SCHEMA_MAP."""
        from contracts.live_subs_events import LiveSubsResult
        self.assertIs(EVENT_SCHEMA_MAP[EventType.LIVE_SUBS_RESULT], LiveSubsResult)


class SttEventPayloadMissingFieldTest(unittest.TestCase):
    """test_stt_event_payload_missing_field_rejected."""

    def test_stt_event_payload_missing_field_rejected(self):
        from pydantic import ValidationError
        # SttFinal requires history_id, text, duration_sec
        with self.assertRaises(ValidationError):
            SttFinal(text="hello")

    def test_stt_partial_missing_text_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SttPartial()

    def test_stt_failed_missing_reason_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SttFailed()


class SttEventPayloadValidTest(unittest.TestCase):
    """test_stt_event_payload_valid — all required fields accepted."""

    def test_stt_event_payload_valid(self):
        ev = SttFinal(
            history_id="hid-wave162",
            text="Краб слышит всё",
            duration_sec=4.2,
            language="ru",
            confidence=0.97,
        )
        self.assertEqual(ev.history_id, "hid-wave162")
        self.assertAlmostEqual(ev.confidence, 0.97)


class TranslationEventPayloadValidTest(unittest.TestCase):
    """test_translation_event_payload_valid."""

    def test_translation_event_payload_valid(self):
        from contracts.translation_events import TranslationCompleted
        ev = TranslationCompleted(
            history_id="hid-t1",
            source_text="buenos días amigo",
            translated_text="good morning friend",
            source_lang="es",
            target_lang="en",
            engine="local",
            mode="es_en",
        )
        self.assertEqual(ev.source_lang, "es")
        self.assertEqual(ev.target_lang, "en")
        self.assertEqual(ev.engine, "local")


class EventEnvelopeFormatTest(unittest.TestCase):
    """test_event_envelope_format — {type, ts, data} required keys."""

    def test_event_envelope_format(self):
        from contracts.envelope import KrabEventEnvelope
        now = datetime.now(timezone.utc)
        env = KrabEventEnvelope(type="stt.final", ts=now, data={"text": "hi"})
        dumped = env.model_dump()
        self.assertIn("type", dumped)
        self.assertIn("ts", dumped)
        self.assertIn("data", dumped)
        self.assertEqual(dumped["type"], "stt.final")

    def test_envelope_missing_type_raises(self):
        from contracts.envelope import KrabEventEnvelope
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            KrabEventEnvelope(ts=datetime.now(timezone.utc), data={})

    def test_envelope_missing_ts_raises(self):
        from contracts.envelope import KrabEventEnvelope
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            KrabEventEnvelope(type="stt.partial", data={})


class EventTypeEnumCompleteTest(unittest.TestCase):
    """test_event_type_enum_complete — all known event type values present."""

    _EXPECTED = {
        "stt.partial",
        "stt.final",
        "stt.failed",
        "translation.completed",
        "translation.failed",
        "markdown_export",
        "auto_summary",
        "hotword.detected",
        "live_subs.result",
    }

    def test_event_type_enum_complete(self):
        actual = {e.value for e in EventType}
        self.assertEqual(actual, self._EXPECTED)

    def test_no_unexpected_values(self):
        actual = {e.value for e in EventType}
        extras = actual - self._EXPECTED
        self.assertEqual(extras, set(), f"Unexpected EventType values: {extras}")


class SchemaMapCompleteTest(unittest.TestCase):
    """test_schema_map_complete — EVENT_SCHEMA_MAP has entry for each EventType."""

    def test_schema_map_complete(self):
        for etype in EventType:
            self.assertIn(
                etype, EVENT_SCHEMA_MAP,
                f"EVENT_SCHEMA_MAP missing entry for {etype.value!r}",
            )

    def test_schema_map_no_orphan_entries(self):
        valid_types = set(EventType)
        for key in EVENT_SCHEMA_MAP:
            self.assertIn(key, valid_types, f"Orphan key in EVENT_SCHEMA_MAP: {key!r}")

    def test_schema_map_all_pydantic_models(self):
        from pydantic import BaseModel
        for etype, cls in EVENT_SCHEMA_MAP.items():
            self.assertTrue(
                issubclass(cls, BaseModel),
                f"EVENT_SCHEMA_MAP[{etype.value!r}] is not a BaseModel subclass",
            )


class UnicodeInPayloadFieldsTest(unittest.TestCase):
    """test_unicode_in_payload_fields — Cyrillic, Spanish, emoji in payload fields."""

    def test_unicode_in_stt_final(self):
        ev = SttFinal(
            history_id="uid-кириллица",
            text="Привет, как дела? ¡Hola! 🦀",
            duration_sec=2.0,
            language="ru",
        )
        self.assertIn("кириллица", ev.history_id)
        self.assertIn("🦀", ev.text)

    def test_unicode_roundtrip_json(self):
        ev = SttFinal(
            history_id="uid-1",
            text="Краб слышит: ¡Привет! 你好 🎤",
            duration_sec=1.5,
        )
        restored = SttFinal.model_validate_json(ev.model_dump_json())
        self.assertEqual(restored.text, ev.text)

    def test_unicode_in_translation_completed(self):
        from contracts.translation_events import TranslationCompleted
        ev = TranslationCompleted(
            history_id="hid-u",
            source_text="Привет, это тест с Unicode: 🦀",
            translated_text="Hola, esta es una prueba con Unicode: 🦀",
            source_lang="ru",
            target_lang="es",
            engine="local",
            mode="ru_es",
        )
        self.assertIn("🦀", ev.source_text)
        self.assertIn("🦀", ev.translated_text)

    def test_unicode_in_live_subs(self):
        from contracts.live_subs_events import LiveSubsResult
        ev = LiveSubsResult(
            text="日本語テスト — Японский текст",
            translation="Japanese test text",
            start_ts=0.0,
            end_ts=2.0,
        )
        self.assertIn("日本語", ev.text)


if __name__ == "__main__":
    unittest.main()
