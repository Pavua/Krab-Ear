"""Unit-тесты реестра контрактов Krab Ear: EventType + EVENT_SCHEMA_MAP."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.envelope import UnknownEventType, parse_and_validate  # noqa: E402
from contracts.export import export_schemas  # noqa: E402
from contracts.registry import EVENT_SCHEMA_MAP, EventType  # noqa: E402
from contracts.stt_events import SttPartial  # noqa: E402
from contracts.translation_events import TranslationCompleted  # noqa: E402


_EXPECTED_EVENT_TYPE_VALUES = {
    "stt.partial",
    "stt.final",
    "stt.failed",
    "translation.completed",
    "translation.failed",
    "markdown_export",
    "auto_summary",
    "hotword.detected",
}


class TestEventTypeAllValues(unittest.TestCase):
    """EventType enum содержит ровно ожидаемый набор значений."""

    def test_all_known_values_present(self):
        actual = {e.value for e in EventType}
        self.assertEqual(actual, _EXPECTED_EVENT_TYPE_VALUES)

    def test_enum_count_matches_expected(self):
        self.assertEqual(len(EventType), len(_EXPECTED_EVENT_TYPE_VALUES))

    def test_event_type_is_str_enum(self):
        self.assertIsInstance(EventType.STT_PARTIAL, str)
        self.assertEqual(EventType.STT_PARTIAL, "stt.partial")

    def test_event_type_from_string(self):
        self.assertIs(EventType("stt.final"), EventType.STT_FINAL)
        self.assertIs(EventType("hotword.detected"), EventType.HOTWORD_DETECTED)

    def test_event_type_invalid_string_raises(self):
        with self.assertRaises(ValueError):
            EventType("nonexistent.event")


class TestEventSchemaMapCompleteness(unittest.TestCase):
    """EVENT_SCHEMA_MAP охватывает все EventType без лишних записей."""

    def test_map_length_equals_enum_count(self):
        self.assertEqual(len(EVENT_SCHEMA_MAP), len(EventType))

    def test_all_enum_values_mapped(self):
        for etype in EventType:
            self.assertIn(etype, EVENT_SCHEMA_MAP, f"{etype.value!r} not in EVENT_SCHEMA_MAP")

    def test_mapped_types_for_remaining_events(self):
        from contracts.history_events import AutoSummaryEvent, MarkdownExportEvent
        from contracts.hotword_events import HotwordDetected
        from contracts.stt_events import SttFailed

        self.assertIs(EVENT_SCHEMA_MAP[EventType.MARKDOWN_EXPORT], MarkdownExportEvent)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.AUTO_SUMMARY], AutoSummaryEvent)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.HOTWORD_DETECTED], HotwordDetected)
        self.assertIs(EVENT_SCHEMA_MAP[EventType.STT_FAILED], SttFailed)

    def test_all_mapped_values_are_pydantic_models(self):
        from pydantic import BaseModel
        for etype, cls in EVENT_SCHEMA_MAP.items():
            self.assertTrue(
                issubclass(cls, BaseModel),
                f"EVENT_SCHEMA_MAP[{etype.value!r}] is not a BaseModel subclass",
            )


class TestSttPartialSerialization(unittest.TestCase):
    """SttPartial сериализуется в корректный JSON и восстанавливается обратно."""

    def test_model_dump_json_minimal(self):
        ev = SttPartial(text="привет")
        raw = json.loads(ev.model_dump_json())
        self.assertEqual(raw["text"], "привет")
        self.assertIsNone(raw["duration_sec"])

    def test_model_dump_json_with_duration(self):
        ev = SttPartial(text="hola", duration_sec=0.8)
        raw = json.loads(ev.model_dump_json())
        self.assertAlmostEqual(raw["duration_sec"], 0.8)

    def test_roundtrip_validate(self):
        ev = SttPartial(text="test", duration_sec=1.23)
        restored = SttPartial.model_validate(ev.model_dump())
        self.assertEqual(restored.text, ev.text)
        self.assertAlmostEqual(restored.duration_sec, ev.duration_sec)


class TestTranslationCompletedRoundTrip(unittest.TestCase):
    """TranslationCompleted: dict → model → dict сохраняет все поля."""

    def _make(self):
        return TranslationCompleted(
            history_id="tid-1",
            source_text="buenos días",
            translated_text="good morning",
            source_lang="es",
            target_lang="en",
            engine="local",
            mode="es_en",
        )

    def test_roundtrip_model_validate(self):
        original = self._make()
        restored = TranslationCompleted.model_validate(original.model_dump())
        self.assertEqual(restored.model_dump(), original.model_dump())

    def test_json_serialization_preserves_unicode(self):
        ev = self._make()
        raw = json.loads(ev.model_dump_json())
        self.assertEqual(raw["source_text"], "buenos días")


class TestSchemaExportAllEvents(unittest.TestCase):
    """export_schemas создаёт файлы для ВСЕХ EventType."""

    def test_all_schema_files_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype in EventType:
                fname = f"{etype.value}.schema.json"
                fpath = out / fname
                self.assertTrue(fpath.exists(), f"Missing schema file: {fname}")

    def test_schema_files_are_valid_json_objects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype in EventType:
                fpath = out / f"{etype.value}.schema.json"
                data = json.loads(fpath.read_text())
                self.assertIsInstance(data, dict, f"{etype.value}: schema is not a JSON object")
                self.assertEqual(
                    data.get("type"), "object",
                    f"{etype.value}: schema missing top-level 'type: object'",
                )
                self.assertIn("properties", data, f"{etype.value}: schema has no 'properties'")


class TestUnknownEventTypeException(unittest.TestCase):
    """UnknownEventType хранит имя неизвестного типа и имеет читаемое сообщение."""

    def test_exception_stores_event_type(self):
        exc = UnknownEventType("tts.completed")
        self.assertEqual(exc.event_type, "tts.completed")

    def test_exception_message_contains_type(self):
        exc = UnknownEventType("tts.completed")
        self.assertIn("tts.completed", str(exc))

    def test_parse_and_validate_raises_for_foreign_domain(self):
        raw = {
            "type": "voice_gateway.session_started",
            "ts": "2026-04-22T08:00:00+00:00",
            "data": {"session_id": "s-1"},
        }
        with self.assertRaises(UnknownEventType) as ctx:
            parse_and_validate(raw)
        self.assertEqual(ctx.exception.event_type, "voice_gateway.session_started")


if __name__ == "__main__":
    unittest.main()
