"""Юнит-тесты для EventBus (KrabEar/backend/event_bus.py)."""

from __future__ import annotations
from contracts.stt_events import SttFailed, SttFinal
from contracts.registry import EventType
from backend.event_bus import EventBus

import queue
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestEmitAndSubscribe(unittest.TestCase):
    """test_emit_and_subscribe — подписаться, эмитировать, проверить что событие получено."""

    def test_emit_and_subscribe(self) -> None:
        bus = EventBus()
        q = bus.subscribe()

        bus.emit("stt.final", {"text": "привет"})

        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.final")
        self.assertEqual(event["data"], {"text": "привет"})
        self.assertIn("ts", event)


class TestEmitTypedValidatesSchema(unittest.TestCase):
    """test_emit_typed_validates_schema — emit_typed с корректными данными проходит."""

    def test_emit_typed_valid_data(self) -> None:
        bus = EventBus()
        q = bus.subscribe()

        payload = SttFailed(reason="mic_error", duration_sec=1.5)
        bus.emit_typed(EventType.STT_FAILED, payload)

        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.failed")
        self.assertEqual(event["data"]["reason"], "mic_error")
        self.assertAlmostEqual(event["data"]["duration_sec"], 1.5)

    def test_emit_typed_stt_final(self) -> None:
        bus = EventBus()
        q = bus.subscribe()

        payload = SttFinal(
            history_id="abc-123",
            text="hola mundo",
            duration_sec=2.0,
            language="es",
            confidence=0.95,
        )
        bus.emit_typed(EventType.STT_FINAL, payload)

        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.final")
        self.assertEqual(event["data"]["history_id"], "abc-123")
        self.assertEqual(event["data"]["text"], "hola mundo")


class TestEmitTypedInvalidDataRaises(unittest.TestCase):
    """test_emit_typed_invalid_data_raises — emit_typed с невалидными данными бросает ValidationError."""

    def test_missing_required_field_raises(self) -> None:
        from pydantic import ValidationError

        # SttFailed требует поле reason — передаём без него
        with self.assertRaises((ValidationError, TypeError)):
            bad_payload = SttFailed()  # type: ignore[call-arg]
            bus = EventBus()
            bus.emit_typed(EventType.STT_FAILED, bad_payload)

    def test_wrong_type_raises(self) -> None:
        from pydantic import ValidationError

        # duration_sec должен быть float; передаём строку — Pydantic должен либо скастить, либо упасть
        # Проверяем что хотя бы SttFinal с невалидным типом не молча игнорируется
        with self.assertRaises((ValidationError, TypeError, ValueError)):
            bad = SttFinal(
                history_id=None,  # type: ignore[arg-type]
                text=None,  # type: ignore[arg-type]
                duration_sec="not-a-float",  # type: ignore[arg-type]
            )
            bus = EventBus()
            bus.emit_typed(EventType.STT_FINAL, bad)


class TestMultipleSubscribers(unittest.TestCase):
    """test_multiple_subscribers — два подписчика получают одно и то же событие."""

    def test_both_subscribers_receive_event(self) -> None:
        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        self.assertEqual(bus.subscriber_count(), 2)

        bus.emit("stt.partial", {"text": "тест"})

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()

        self.assertEqual(e1["type"], "stt.partial")
        self.assertEqual(e2["type"], "stt.partial")
        self.assertEqual(e1["data"], e2["data"])

    def test_second_subscriber_misses_earlier_events(self) -> None:
        """Подписчик, добавленный после emit, не получает прошлые события."""
        bus = EventBus()
        q1 = bus.subscribe()
        bus.emit("stt.partial", {"text": "раньше"})

        q2 = bus.subscribe()  # подписался после эмита

        q1.get_nowait()  # q1 получил

        with self.assertRaises(queue.Empty):
            q2.get_nowait()  # q2 не должен получить


class TestUnsubscribe(unittest.TestCase):
    """test_unsubscribe — отписка удаляет callback из шины."""

    def test_unsubscribed_queue_no_longer_receives(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        self.assertEqual(bus.subscriber_count(), 1)

        bus.unsubscribe(q)
        self.assertEqual(bus.subscriber_count(), 0)

        bus.emit("stt.final", {"text": "после отписки"})

        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_unsubscribe_unknown_queue_is_noop(self) -> None:
        """Повторная или ложная отписка не должна бросать исключение."""
        bus = EventBus()
        orphan: queue.Queue = queue.Queue()

        try:
            bus.unsubscribe(orphan)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"unsubscribe бросил неожиданное исключение: {exc}")

    def test_remaining_subscribers_still_receive(self) -> None:
        """После отписки одного другие подписчики продолжают получать события."""
        bus = EventBus()
        q_stay = bus.subscribe()
        q_leave = bus.subscribe()

        bus.unsubscribe(q_leave)
        bus.emit("stt.failed", {"reason": "timeout"})

        event = q_stay.get_nowait()
        self.assertEqual(event["data"]["reason"], "timeout")

        with self.assertRaises(queue.Empty):
            q_leave.get_nowait()


if __name__ == "__main__":
    unittest.main()
