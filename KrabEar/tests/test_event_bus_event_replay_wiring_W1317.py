"""Tests for EventBus → EventReplayManager wiring (W1314 F2 HIGH fix).

Covers:
- test_event_bus_emit_calls_event_replay_record  — emit forwards to record_event
- test_event_bus_emit_when_event_replay_none_no_op — no_op when _event_replay is None
- test_record_failure_does_not_break_emit          — broken record_event doesn't crash emit
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import ANY, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus  # noqa: E402


class TestEventBusEmitCallsEventReplayRecord(unittest.TestCase):
    """emit() должен вызывать record_event на _event_replay когда он задан."""

    def test_event_bus_emit_calls_event_replay_record(self) -> None:
        bus = EventBus()
        mock_replay = MagicMock()
        bus._event_replay = mock_replay

        bus.emit("stt.final", {"text": "hello"})

        # W1677 F4 LOW: emit() now passes ts= kwarg carrying the delivery timestamp.
        mock_replay.record_event.assert_called_once_with("stt.final", {"text": "hello"}, ts=ANY)

    def test_emit_forwards_payload_exactly(self) -> None:
        """Payload передаётся в record_event без изменений."""
        bus = EventBus()
        mock_replay = MagicMock()
        bus._event_replay = mock_replay

        payload = {"confidence": 0.95, "duration_sec": 3.1, "lang": "ru"}
        bus.emit("stt.complete", payload)

        # W1677 F4 LOW: emit() now passes ts= kwarg carrying the delivery timestamp.
        mock_replay.record_event.assert_called_once_with("stt.complete", payload, ts=ANY)

    def test_emit_multiple_events_each_recorded(self) -> None:
        """Каждый emit вызывает record_event ровно по одному разу."""
        bus = EventBus()
        mock_replay = MagicMock()
        bus._event_replay = mock_replay

        bus.emit("evt.a", {"x": 1})
        bus.emit("evt.b", {"x": 2})
        bus.emit("evt.c", {"x": 3})

        # W1677 F4 LOW: emit() now passes ts= kwarg carrying the delivery timestamp.
        self.assertEqual(mock_replay.record_event.call_count, 3)
        mock_replay.record_event.assert_any_call("evt.a", {"x": 1}, ts=ANY)
        mock_replay.record_event.assert_any_call("evt.b", {"x": 2}, ts=ANY)
        mock_replay.record_event.assert_any_call("evt.c", {"x": 3}, ts=ANY)


class TestEventBusEmitWhenEventReplayNoneNoOp(unittest.TestCase):
    """Когда _event_replay is None — emit работает без ошибок."""

    def test_event_bus_emit_when_event_replay_none_no_op(self) -> None:
        bus = EventBus()
        # По умолчанию _event_replay должен быть None
        self.assertIsNone(bus._event_replay)

        # emit должен работать без исключений
        try:
            bus.emit("stt.final", {"text": "world"})
        except Exception as exc:
            self.fail(f"emit() raised unexpectedly when _event_replay is None: {exc}")

    def test_default_event_replay_is_none(self) -> None:
        """Новый EventBus не имеет _event_replay по умолчанию."""
        bus = EventBus()
        self.assertIsNone(bus._event_replay)

    def test_subscribers_still_receive_events_when_replay_none(self) -> None:
        """SSE-подписчики по-прежнему получают события при _event_replay=None."""
        bus = EventBus()
        q = bus.subscribe()

        bus.emit("test.event", {"v": 42})

        event = q.get_nowait()
        self.assertEqual(event["type"], "test.event")
        self.assertEqual(event["data"]["v"], 42)


class TestRecordFailureDoesNotBreakEmit(unittest.TestCase):
    """Исключение в record_event не должно обрывать emit."""

    def test_record_failure_does_not_break_emit(self) -> None:
        bus = EventBus()
        broken_replay = MagicMock()
        broken_replay.record_event.side_effect = RuntimeError("disk full")
        bus._event_replay = broken_replay

        q = bus.subscribe()

        # emit не должен выбрасывать исключение
        try:
            bus.emit("stt.final", {"text": "test"})
        except Exception as exc:
            self.fail(f"emit() raised when record_event failed: {exc}")

        # Подписчик всё равно получил событие
        event = q.get_nowait()
        self.assertEqual(event["type"], "stt.final")

    def test_broken_replay_does_not_affect_subsequent_emits(self) -> None:
        """После сбоя record_event следующий emit тоже работает."""
        bus = EventBus()
        broken_replay = MagicMock()
        broken_replay.record_event.side_effect = OSError("io error")
        bus._event_replay = broken_replay

        q = bus.subscribe()

        bus.emit("evt.one", {"n": 1})
        bus.emit("evt.two", {"n": 2})

        e1 = q.get_nowait()
        e2 = q.get_nowait()
        self.assertEqual(e1["type"], "evt.one")
        self.assertEqual(e2["type"], "evt.two")

    def test_late_injection_replaces_none(self) -> None:
        """Late-injection устанавливает _event_replay после создания шины."""
        bus = EventBus()
        self.assertIsNone(bus._event_replay)

        mock_replay = MagicMock()
        bus._event_replay = mock_replay

        bus.emit("post.inject", {"k": "v"})

        # W1677 F4 LOW: emit() now passes ts= kwarg carrying the delivery timestamp.
        mock_replay.record_event.assert_called_once_with("post.inject", {"k": "v"}, ts=ANY)


if __name__ == "__main__":
    unittest.main()
