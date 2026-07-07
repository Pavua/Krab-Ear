"""test_event_bus_emit_envelope.py — EventBus.emit_envelope() (event-bridge design,
spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.3).

emit_envelope() доставляет УЖЕ ГОТОВЫЙ конверт {type, ts, data[, origin]}
подписчикам (SSE/WS) КАК ЕСТЬ — БЕЗ вызова push-листенеров (структурный
no-echo guard: REST-сторона моста не должна повторно триггерить вебхуки —
исходный emit() в IPC-процессе их уже вызвал) и БЕЗ перештамповки ts.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_event_bus_emit_envelope.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus  # noqa: E402


class EmitEnvelopeTestCase(unittest.TestCase):
    def test_subscriber_receives_envelope_as_is(self):
        bus = EventBus()
        q = bus.subscribe()
        envelope = {
            "type": "krab_error",
            "ts": "2026-07-07T00:00:00+00:00",
            "data": {"code": "x"},
            "origin": "ipc",
        }
        bus.emit_envelope(envelope)
        received = q.get_nowait()
        self.assertEqual(received, envelope)

    def test_ts_not_restamped(self):
        bus = EventBus()
        q = bus.subscribe()
        original_ts = "2020-01-01T00:00:00+00:00"  # заведомо не "сейчас"
        bus.emit_envelope({"type": "x", "ts": original_ts, "data": {}})
        received = q.get_nowait()
        self.assertEqual(received["ts"], original_ts)

    def test_listeners_not_invoked(self):
        bus = EventBus()
        calls = []
        bus.add_listener(lambda et, pl: calls.append((et, pl)))
        bus.emit_envelope({"type": "stt.final", "ts": "t", "data": {"text": "secret"}})
        self.assertEqual(calls, [], "emit_envelope НЕ должен вызывать push-листенеры (no-echo guard)")

    def test_regular_emit_still_invokes_listeners(self):
        """Контроль: emit() (нативный путь) листенеры всё ещё вызывает — только
        emit_envelope() их пропускает."""
        bus = EventBus()
        calls = []
        bus.add_listener(lambda et, pl: calls.append((et, pl)))
        bus.emit("stt.final", {"text": "x"})
        self.assertEqual(len(calls), 1)

    def test_missing_type_key_is_ignored_defensively(self):
        bus = EventBus()
        q = bus.subscribe()
        bus.emit_envelope({"ts": "t", "data": {}})  # нет "type"
        self.assertTrue(q.empty(), "конверт без 'type' должен быть проигнорирован, не упасть")

    def test_full_subscriber_queue_drops_without_raising(self):
        bus = EventBus()
        bus.subscribe()  # подписчик с maxsize=64, ничего не читает
        for i in range(100):
            bus.emit_envelope({"type": "x", "ts": str(i), "data": {}})
        # Не должно поднять исключение — переполнение просто логируется и дропается.


if __name__ == "__main__":
    unittest.main(verbosity=2)
