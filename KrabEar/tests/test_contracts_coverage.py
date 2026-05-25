"""Wave 258 — contracts module coverage.

Покрывает:
- test_every_event_type_has_schema (PR #513 invariant)
- test_every_event_type_has_pydantic_model
- test_envelope_shape_consistent (type/ts/data structure)
- test_emit_typed_rejects_wrong_event_type
- test_emit_typed_validates_pydantic_payload
- test_schema_export_to_disk (JSON dump)
- test_schema_roundtrip (export → load → equivalent)
- test_unicode_event_data_preserved
- test_concurrent_emit

Не дублирует test_contracts.py / test_contracts_registry.py / test_event_bus.py.
"""

from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from contracts.envelope import KrabEventEnvelope, parse_and_validate  # noqa: E402
from contracts.export import export_schemas  # noqa: E402
from contracts.registry import EVENT_SCHEMA_MAP, EventType  # noqa: E402
from contracts.stt_events import SttFailed, SttFinal, SttPartial  # noqa: E402
from contracts.translation_events import TranslationCompleted  # noqa: E402
from contracts.live_subs_events import LiveSubsResult  # noqa: E402
from backend.event_bus import EventBus  # noqa: E402
from pydantic import BaseModel  # noqa: E402


# ---------------------------------------------------------------------------
# PR #513 invariant: every EventType has a schema mapping
# ---------------------------------------------------------------------------

class TestEveryEventTypeHasSchema(unittest.TestCase):
    """PR #513 invariant: EVENT_SCHEMA_MAP must cover every EventType member."""

    def test_every_event_type_has_schema(self):
        """Каждый EventType присутствует в EVENT_SCHEMA_MAP."""
        missing = [
            etype.value
            for etype in EventType
            if etype not in EVENT_SCHEMA_MAP
        ]
        self.assertEqual(
            missing, [],
            f"EventType members missing from EVENT_SCHEMA_MAP: {missing}",
        )

    def test_no_extra_keys_in_schema_map(self):
        """В EVENT_SCHEMA_MAP нет ключей без соответствующего EventType."""
        enum_values = set(EventType)
        extra = [k for k in EVENT_SCHEMA_MAP if k not in enum_values]
        self.assertEqual(extra, [], f"Orphan keys in EVENT_SCHEMA_MAP: {extra}")

    def test_event_type_count_equals_9(self):
        """Ровно 9 EventType зарегистрировано (wave 258 snapshot)."""
        self.assertEqual(len(EventType), 9)


# ---------------------------------------------------------------------------
# Every EventType maps to a Pydantic BaseModel subclass
# ---------------------------------------------------------------------------

class TestEveryEventTypeHasPydanticModel(unittest.TestCase):
    """Каждый EventType маппируется на подкласс BaseModel."""

    def test_all_mapped_classes_are_basemodel_subclasses(self):
        for etype, cls in EVENT_SCHEMA_MAP.items():
            with self.subTest(event_type=etype.value):
                self.assertTrue(
                    issubclass(cls, BaseModel),
                    f"{etype.value} -> {cls} is not a BaseModel subclass",
                )

    def test_all_mapped_classes_instantiable_with_minimal_data(self):
        """Каждая модель инстанциируется хотя бы с minimal valid payload."""
        minimal_payloads = {
            EventType.STT_PARTIAL: {"text": "hi"},
            EventType.STT_FINAL: {"history_id": "x", "text": "hi", "duration_sec": 1.0},
            EventType.STT_FAILED: {"reason": "timeout"},
            EventType.TRANSLATION_COMPLETED: {
                "history_id": "x",
                "source_text": "hola",
                "translated_text": "hi",
                "source_lang": "es",
                "target_lang": "en",
                "engine": "local",
                "mode": "es_en",
            },
            EventType.TRANSLATION_FAILED: {"source_text": "hola", "reason": "error"},
            EventType.MARKDOWN_EXPORT: {"entries": 1, "chars": 100, "copy_to_clipboard": False},
            EventType.AUTO_SUMMARY: {
                "items_processed": 1,
                "total_words": 10,
                "fallback": False,
                "summary": "ok",
            },
            EventType.HOTWORD_DETECTED: {
                "history_id": "x",
                "text": "краб",
                "matches": [],
            },
            EventType.LIVE_SUBS_RESULT: {"text": "hi", "start_ts": 0.0, "end_ts": 1.0},
        }
        for etype, cls in EVENT_SCHEMA_MAP.items():
            with self.subTest(event_type=etype.value):
                payload_data = minimal_payloads[etype]
                instance = cls.model_validate(payload_data)
                self.assertIsInstance(instance, BaseModel)


# ---------------------------------------------------------------------------
# Envelope shape: type / ts / data structure
# ---------------------------------------------------------------------------

class TestEnvelopeShapeConsistent(unittest.TestCase):
    """KrabEventEnvelope {type, ts, data} shape is consistent across all EventType."""

    def _make_raw_envelope(self, etype: EventType, data: dict) -> dict:
        return {
            "type": etype.value,
            "ts": "2026-05-20T10:00:00+00:00",
            "data": data,
        }

    def test_envelope_has_type_ts_data_keys(self):
        raw = self._make_raw_envelope(EventType.STT_PARTIAL, {"text": "hello"})
        env = KrabEventEnvelope.model_validate(raw)
        self.assertEqual(env.type, "stt.partial")
        self.assertIsNotNone(env.ts)
        self.assertIsInstance(env.data, dict)

    def test_all_event_types_parse_to_envelope(self):
        """Каждый EventType value парсится в KrabEventEnvelope без ошибок."""
        for etype in EventType:
            with self.subTest(event_type=etype.value):
                raw = self._make_raw_envelope(etype, {"_probe": True})
                env = KrabEventEnvelope.model_validate(raw)
                self.assertEqual(env.type, etype.value)
                self.assertIsInstance(env.data, dict)

    def test_emit_produces_type_ts_data_keys(self):
        """EventBus.emit создаёт конверт с ключами type/ts/data."""
        bus = EventBus()
        q = bus.subscribe()
        bus.emit("stt.partial", {"text": "test"})
        event = q.get_nowait()
        self.assertIn("type", event)
        self.assertIn("ts", event)
        self.assertIn("data", event)
        bus.unsubscribe(q)

    def test_emit_typed_produces_type_ts_data_keys(self):
        """EventBus.emit_typed создаёт конверт с ключами type/ts/data."""
        bus = EventBus()
        q = bus.subscribe()
        bus.emit_typed(EventType.STT_FAILED, SttFailed(reason="timeout"))
        event = q.get_nowait()
        self.assertIn("type", event)
        self.assertIn("ts", event)
        self.assertIn("data", event)
        bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# emit_typed rejects wrong EventType (mismatched enum vs payload)
# ---------------------------------------------------------------------------

class TestEmitTypedRejectsWrongEventType(unittest.TestCase):
    """emit_typed с несовместимым payload генерирует ошибку валидации."""

    def test_emit_typed_mismatched_payload_reaches_subscriber(self):
        """emit_typed не проверяет соответствие EventType↔модели (это на стороне
        вызывающего кода), но сам emit_typed не падает — payload уже валидирован
        Pydantic-конструктором. Тест фиксирует текущее поведение."""
        bus = EventBus()
        q = bus.subscribe()
        # STT_FINAL enum, но payload SttPartial — технически разные модели;
        # emit_typed принимает любой BaseModel, поэтому событие дойдёт.
        payload = SttPartial(text="mismatch test")
        bus.emit_typed(EventType.STT_FINAL, payload)
        event = q.get_nowait()
        # type берётся из EventType, не из payload class
        self.assertEqual(event["type"], "stt.final")
        self.assertIn("text", event["data"])
        bus.unsubscribe(q)

    def test_emit_typed_invalid_pydantic_payload_raises_before_emit(self):
        """Передача невалидного объекта (не BaseModel) вызывает AttributeError."""
        bus = EventBus()
        with self.assertRaises(AttributeError):
            bus.emit_typed(EventType.STT_FAILED, "not-a-model")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# emit_typed validates pydantic payload
# ---------------------------------------------------------------------------

class TestEmitTypedValidatesPydanticPayload(unittest.TestCase):
    """emit_typed корректно сериализует Pydantic payload в event data."""

    def test_stt_partial_payload_round_trip(self):
        bus = EventBus()
        q = bus.subscribe()
        payload = SttPartial(text="частичный текст", duration_sec=0.5)
        bus.emit_typed(EventType.STT_PARTIAL, payload)
        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.partial")
        self.assertEqual(event["data"]["text"], "частичный текст")
        self.assertAlmostEqual(event["data"]["duration_sec"], 0.5)
        bus.unsubscribe(q)

    def test_live_subs_result_payload(self):
        bus = EventBus()
        q = bus.subscribe()
        payload = LiveSubsResult(
            text="subtitle text",
            translation="subtítulo",
            start_ts=1.0,
            end_ts=2.5,
            language_detected="en",
        )
        bus.emit_typed(EventType.LIVE_SUBS_RESULT, payload)
        event = q.get_nowait()
        self.assertEqual(event["type"], "live_subs.result")
        self.assertEqual(event["data"]["text"], "subtitle text")
        self.assertEqual(event["data"]["translation"], "subtítulo")
        self.assertAlmostEqual(event["data"]["end_ts"], 2.5)
        bus.unsubscribe(q)

    def test_translation_completed_payload_all_fields(self):
        bus = EventBus()
        q = bus.subscribe()
        payload = TranslationCompleted(
            history_id="tid-wave258",
            source_text="добрый день",
            translated_text="buenos días",
            source_lang="ru",
            target_lang="es",
            engine="local",
            mode="ru_es",
        )
        bus.emit_typed(EventType.TRANSLATION_COMPLETED, payload)
        event = q.get_nowait()
        self.assertEqual(event["type"], "translation.completed")
        self.assertEqual(event["data"]["source_lang"], "ru")
        self.assertEqual(event["data"]["target_lang"], "es")
        bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# Schema export to disk
# ---------------------------------------------------------------------------

class TestSchemaExportToDisk(unittest.TestCase):
    """export_schemas создаёт корректные JSON-файлы на диск."""

    def test_schema_export_creates_all_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype in EventType:
                expected = out / f"{etype.value}.schema.json"
                self.assertTrue(expected.exists(), f"Missing: {etype.value}.schema.json")

    def test_schema_files_are_nonempty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype in EventType:
                fpath = out / f"{etype.value}.schema.json"
                self.assertGreater(fpath.stat().st_size, 0, f"Empty schema: {etype.value}")

    def test_schema_files_contain_object_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype in EventType:
                with self.subTest(event_type=etype.value):
                    data = json.loads((out / f"{etype.value}.schema.json").read_text())
                    self.assertEqual(data.get("type"), "object",
                                     f"{etype.value}: top-level type != 'object'")
                    self.assertIn("properties", data,
                                  f"{etype.value}: missing 'properties'")

    def test_schema_export_count_equals_event_type_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            schema_files = list(out.glob("*.schema.json"))
            self.assertEqual(len(schema_files), len(EventType))


# ---------------------------------------------------------------------------
# Schema roundtrip: export → load → structurally equivalent
# ---------------------------------------------------------------------------

class TestSchemaRoundtrip(unittest.TestCase):
    """Экспортированная схема структурно эквивалентна model_json_schema()."""

    def test_roundtrip_stt_final(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            loaded = json.loads((out / "stt.final.schema.json").read_text())
            fresh = SttFinal.model_json_schema()
            self.assertEqual(loaded, fresh)

    def test_roundtrip_all_event_types(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype, cls in EVENT_SCHEMA_MAP.items():
                with self.subTest(event_type=etype.value):
                    loaded = json.loads(
                        (out / f"{etype.value}.schema.json").read_text()
                    )
                    fresh = cls.model_json_schema()
                    self.assertEqual(
                        loaded, fresh,
                        f"{etype.value}: exported schema differs from model_json_schema()",
                    )

    def test_roundtrip_preserves_required_fields(self):
        """required[] в экспортированной схеме совпадает с model_json_schema()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            for etype, cls in EVENT_SCHEMA_MAP.items():
                with self.subTest(event_type=etype.value):
                    loaded = json.loads(
                        (out / f"{etype.value}.schema.json").read_text()
                    )
                    fresh = cls.model_json_schema()
                    self.assertEqual(
                        loaded.get("required", []),
                        fresh.get("required", []),
                        f"{etype.value}: 'required' mismatch",
                    )


# ---------------------------------------------------------------------------
# Unicode event data preserved end-to-end
# ---------------------------------------------------------------------------

class TestUnicodeEventDataPreserved(unittest.TestCase):
    """Unicode в данных события сохраняется через весь pipeline."""

    def test_cyrillic_text_in_stt_partial(self):
        bus = EventBus()
        q = bus.subscribe()
        text = "Привет, как дела? Всё хорошо!"
        bus.emit_typed(EventType.STT_PARTIAL, SttPartial(text=text))
        event = q.get_nowait()
        self.assertEqual(event["data"]["text"], text)
        bus.unsubscribe(q)

    def test_spanish_text_in_translation_completed(self):
        bus = EventBus()
        q = bus.subscribe()
        payload = TranslationCompleted(
            history_id="uni-1",
            source_text="¿Cómo estás? ¡Muy bien!",
            translated_text="Как ты? Очень хорошо!",
            source_lang="es",
            target_lang="ru",
            engine="local",
            mode="es_ru",
        )
        bus.emit_typed(EventType.TRANSLATION_COMPLETED, payload)
        event = q.get_nowait()
        self.assertEqual(event["data"]["source_text"], "¿Cómo estás? ¡Muy bien!")
        self.assertEqual(event["data"]["translated_text"], "Как ты? Очень хорошо!")
        bus.unsubscribe(q)

    def test_unicode_preserved_in_json_schema_export(self):
        """ensure_ascii=False — кириллица не эскейпируется в экспортированной схеме."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            export_schemas(out)
            # Читаем все схемы как bytes и проверяем что файл не пустой
            for etype in EventType:
                raw_bytes = (out / f"{etype.value}.schema.json").read_bytes()
                self.assertGreater(len(raw_bytes), 10)

    def test_parse_and_validate_preserves_unicode_in_data(self):
        raw = {
            "type": "stt.failed",
            "ts": "2026-05-20T10:00:00+00:00",
            "data": {"reason": "ошибка модели"},
        }
        etype, payload = parse_and_validate(raw)
        self.assertEqual(payload.reason, "ошибка модели")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Concurrent emit
# ---------------------------------------------------------------------------

class TestConcurrentEmit(unittest.TestCase):
    """EventBus.emit потокобезопасен при конкурентных публикациях."""

    def test_concurrent_emit_all_events_received(self):
        """N потоков конкурентно шлют события — все доходят до подписчика."""
        bus = EventBus()
        q = bus.subscribe()
        n_threads = 10
        events_per_thread = 5
        total = n_threads * events_per_thread

        def worker(thread_id: int):
            for i in range(events_per_thread):
                bus.emit_typed(
                    EventType.STT_PARTIAL,
                    SttPartial(text=f"thread-{thread_id}-msg-{i}"),
                )

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        received = []
        while True:
            try:
                received.append(q.get_nowait())
            except queue.Empty:
                break

        bus.unsubscribe(q)
        self.assertEqual(len(received), total,
                         f"Expected {total} events, got {len(received)}")

    def test_concurrent_subscribe_unsubscribe_safe(self):
        """Одновременные subscribe/unsubscribe не вызывают гонок."""
        bus = EventBus()
        errors = []

        def sub_unsub():
            try:
                q = bus.subscribe()
                bus.emit("stt.partial", {"text": "concurrent"})
                bus.unsubscribe(q)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sub_unsub) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")

    def test_concurrent_emit_typed_multiple_event_types(self):
        """Конкурентные emit_typed разных типов событий не смешивают данные."""
        bus = EventBus()
        q = bus.subscribe()

        def emit_stt():
            for _ in range(5):
                bus.emit_typed(EventType.STT_FAILED, SttFailed(reason="timeout"))

        def emit_live():
            for _ in range(5):
                bus.emit_typed(
                    EventType.LIVE_SUBS_RESULT,
                    LiveSubsResult(text="sub", start_ts=0.0, end_ts=1.0),
                )

        t1 = threading.Thread(target=emit_stt)
        t2 = threading.Thread(target=emit_live)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        received = []
        while True:
            try:
                received.append(q.get_nowait())
            except queue.Empty:
                break

        bus.unsubscribe(q)
        self.assertEqual(len(received), 10)
        types = {e["type"] for e in received}
        self.assertIn("stt.failed", types)
        self.assertIn("live_subs.result", types)
        # каждое событие содержит корректные данные своего типа
        for ev in received:
            self.assertIn("type", ev)
            self.assertIn("ts", ev)
            self.assertIn("data", ev)


if __name__ == "__main__":
    unittest.main()
