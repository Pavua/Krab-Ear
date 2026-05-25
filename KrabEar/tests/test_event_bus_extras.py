"""Wave 162 — дополнительные тесты EventBus (KrabEar/backend/event_bus.py).

Покрывает: concurrent subscribe/unsubscribe, handler exception isolation,
SSE stream yield, unicode data, high-volume no-loss, typed emit via EventType,
subscribe→emit→handler flow, unsubscribe stops delivery.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus, sse_stream, _QUEUE_MAXSIZE  # noqa: E402
from contracts.registry import EventType  # noqa: E402
from contracts.stt_events import SttFinal, SttPartial  # noqa: E402


class TestSubscribeThenEmitCallsHandler(unittest.TestCase):
    """test_subscribe_then_emit_calls_handler."""

    def test_subscribe_then_emit_calls_handler(self):
        bus = EventBus()
        received: list[dict] = []

        q = bus.subscribe()

        bus.emit("stt.partial", {"text": "тест wave162"})

        event = q.get(timeout=1.0)
        received.append(event)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "stt.partial")
        self.assertEqual(received[0]["data"]["text"], "тест wave162")
        self.assertIn("ts", received[0])

        bus.unsubscribe(q)


class TestUnsubscribeStopsHandler(unittest.TestCase):
    """test_unsubscribe_stops_handler."""

    def test_unsubscribe_stops_handler(self):
        bus = EventBus()
        q = bus.subscribe()

        # Confirm events arrive before unsubscribe
        bus.emit("stt.partial", {"text": "before"})
        first = q.get(timeout=1.0)
        self.assertEqual(first["data"]["text"], "before")

        bus.unsubscribe(q)
        self.assertEqual(bus.subscriber_count(), 0)

        # After unsubscribe, no more events
        bus.emit("stt.partial", {"text": "after"})
        with self.assertRaises(queue.Empty):
            q.get_nowait()


class TestEmitTypedUsesEventType(unittest.TestCase):
    """test_emit_typed_uses_EventType — EventType enum value used as event type string."""

    def test_emit_typed_uses_EventType(self):
        bus = EventBus()
        q = bus.subscribe()

        payload = SttPartial(text="typed emit test", duration_sec=0.5)
        bus.emit_typed(EventType.STT_PARTIAL, payload)

        event = q.get(timeout=1.0)
        # The event type must be the EventType string value, not the enum member name
        self.assertEqual(event["type"], EventType.STT_PARTIAL.value)
        self.assertEqual(event["type"], "stt.partial")
        self.assertEqual(event["data"]["text"], "typed emit test")
        self.assertAlmostEqual(event["data"]["duration_sec"], 0.5)

        bus.unsubscribe(q)

    def test_emit_typed_stt_final_uses_correct_type_string(self):
        bus = EventBus()
        q = bus.subscribe()

        payload = SttFinal(
            history_id="hid-typed",
            text="Краб слышит",
            duration_sec=2.0,
            language="ru",
            confidence=0.91,
        )
        bus.emit_typed(EventType.STT_FINAL, payload)

        event = q.get(timeout=1.0)
        self.assertEqual(event["type"], "stt.final")
        self.assertEqual(event["data"]["history_id"], "hid-typed")
        self.assertEqual(event["data"]["language"], "ru")

        bus.unsubscribe(q)


class TestConcurrentSubscribeUnsubscribe(unittest.TestCase):
    """test_concurrent_subscribe_unsubscribe — thread-safety of subscribe/unsubscribe."""

    def test_concurrent_subscribe_unsubscribe(self):
        bus = EventBus()
        errors: list[Exception] = []
        queues_created: list[queue.Queue] = []
        lock = threading.Lock()

        def subscribe_and_record():
            try:
                q = bus.subscribe()
                with lock:
                    queues_created.append(q)
            except Exception as e:
                with lock:
                    errors.append(e)

        def unsubscribe_random():
            try:
                with lock:
                    if queues_created:
                        q = queues_created.pop(0)
                    else:
                        return
                bus.unsubscribe(q)
            except Exception as e:
                with lock:
                    errors.append(e)

        threads = []
        for _ in range(20):
            threads.append(threading.Thread(target=subscribe_and_record))
        for _ in range(10):
            threads.append(threading.Thread(target=unsubscribe_random))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Thread errors: {errors}")
        # Cleanup remaining
        with lock:
            for q in queues_created:
                bus.unsubscribe(q)

    def test_concurrent_emit_and_subscribe(self):
        """Concurrent emit + subscribe does not deadlock or corrupt state."""
        bus = EventBus()
        errors: list[Exception] = []
        stop_flag = threading.Event()

        def emitter():
            for i in range(100):
                if stop_flag.is_set():
                    break
                try:
                    bus.emit("stt.partial", {"i": i})
                except Exception as e:
                    errors.append(e)

        def subscriber():
            for _ in range(10):
                if stop_flag.is_set():
                    break
                try:
                    q = bus.subscribe()
                    bus.unsubscribe(q)
                except Exception as e:
                    errors.append(e)

        t_emit = threading.Thread(target=emitter)
        t_sub = threading.Thread(target=subscriber)
        t_emit.start()
        t_sub.start()
        t_emit.join(timeout=5.0)
        t_sub.join(timeout=5.0)
        stop_flag.set()

        self.assertEqual(errors, [], f"Concurrent errors: {errors}")


class TestHandlerExceptionDoesNotBreakOthers(unittest.TestCase):
    """test_handler_exception_does_not_break_others.

    Since EventBus uses queues (not callbacks), we verify that a full/bad queue
    for one subscriber does not prevent delivery to another subscriber.
    """

    def test_handler_exception_does_not_break_others(self):
        """A full queue (simulated bad handler) does not block other subscribers."""
        bus = EventBus()
        q_good = bus.subscribe()
        q_full = bus.subscribe()

        # Fill q_full to capacity to simulate a "stuck handler"
        for i in range(_QUEUE_MAXSIZE):
            q_full.put_nowait({"type": "fill", "ts": "t", "data": {"i": i}})

        # Emit event — q_full will drop it, q_good should receive it
        bus.emit("stt.failed", {"reason": "test_isolation"})

        # Good subscriber must receive the event
        event = q_good.get(timeout=1.0)
        self.assertEqual(event["type"], "stt.failed")
        self.assertEqual(event["data"]["reason"], "test_isolation")

        bus.unsubscribe(q_good)
        bus.unsubscribe(q_full)

    def test_multiple_full_queues_dont_block_remaining(self):
        """Multiple full queues simultaneously — the one good queue still gets events."""
        bus = EventBus()
        q_good = bus.subscribe()
        bad_queues = []
        for _ in range(3):
            q = bus.subscribe()
            bad_queues.append(q)
            for i in range(_QUEUE_MAXSIZE):
                q.put_nowait({"type": "fill", "ts": "t", "data": {}})

        bus.emit("stt.partial", {"text": "isolation test"})

        event = q_good.get(timeout=1.0)
        self.assertEqual(event["data"]["text"], "isolation test")

        bus.unsubscribe(q_good)
        for q in bad_queues:
            bus.unsubscribe(q)


class TestSseStreamYieldsEvents(unittest.TestCase):
    """test_sse_stream_yields_events — SSE generator yields correctly formatted frames."""

    def test_sse_stream_yields_events(self):
        import json

        bus = EventBus()
        result: list[str] = []
        ready = threading.Event()

        def _run_gen():
            gen = sse_stream(bus)
            ready.set()
            try:
                result.append(next(gen))
            finally:
                gen.close()

        t = threading.Thread(target=_run_gen, daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        time.sleep(0.02)  # give thread time to subscribe
        bus.emit("stt.final", {"history_id": "h1", "text": "stream test"})
        t.join(timeout=5.0)

        self.assertEqual(len(result), 1)
        frame = result[0]
        self.assertTrue(frame.startswith("event: stt.final\n"), f"Bad frame: {frame!r}")
        self.assertIn("data: ", frame)
        self.assertTrue(frame.endswith("\n\n"))

        data_line = next(ln for ln in frame.splitlines() if ln.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["text"], "stream test")

    def test_sse_stream_event_filter(self):
        """event_filter restricts which event types are yielded."""
        bus = EventBus()
        result: list[str] = []
        ready = threading.Event()

        def _run_gen():
            gen = sse_stream(bus, event_filter="stt.final")
            ready.set()
            try:
                # Collect up to 2 items or timeout
                for _ in range(2):
                    result.append(next(gen))
            except StopIteration:
                pass
            finally:
                gen.close()

        t = threading.Thread(target=_run_gen, daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        time.sleep(0.02)

        # Emit a filtered-out event then a passing event
        bus.emit("stt.partial", {"text": "filtered out"})
        bus.emit("stt.final", {"history_id": "h2", "text": "passes filter"})
        t.join(timeout=5.0)

        # Should have received exactly the stt.final
        self.assertGreaterEqual(len(result), 1)
        self.assertTrue(result[0].startswith("event: stt.final\n"))

    def test_sse_stream_terminates_on_none_sentinel(self):
        """None sentinel in queue causes the generator to stop."""
        bus = EventBus()
        result: list[str] = []
        ready = threading.Event()

        def _run_gen():
            gen = sse_stream(bus)
            ready.set()
            for chunk in gen:
                result.append(chunk)

        t = threading.Thread(target=_run_gen, daemon=True)
        t.start()
        ready.wait(timeout=2.0)
        time.sleep(0.02)

        # Put None sentinel directly into the bus subscriber queue
        with bus._lock:
            for q in list(bus._subscribers):
                q.put_nowait(None)

        t.join(timeout=5.0)
        # Generator should have ended cleanly (not hung)
        self.assertFalse(t.is_alive(), "SSE generator did not stop on None sentinel")


class TestUnicodeEventData(unittest.TestCase):
    """test_unicode_event_data — Cyrillic, CJK, emoji survive pub/sub round-trip."""

    def test_unicode_event_data(self):
        bus = EventBus()
        q = bus.subscribe()

        unicode_text = "Привет! 🦀 日本語 ¡Hola! Ñoño"
        bus.emit("stt.partial", {"text": unicode_text})

        event = q.get(timeout=1.0)
        self.assertEqual(event["data"]["text"], unicode_text)

        bus.unsubscribe(q)

    def test_unicode_in_typed_emit(self):
        bus = EventBus()
        q = bus.subscribe()

        payload = SttPartial(text="Краб: ¡Привет! 🎤 你好")
        bus.emit_typed(EventType.STT_PARTIAL, payload)

        event = q.get(timeout=1.0)
        self.assertEqual(event["data"]["text"], "Краб: ¡Привет! 🎤 你好")

        bus.unsubscribe(q)

    def test_unicode_nested_in_data(self):
        """Nested unicode dicts survive EventBus intact."""
        bus = EventBus()
        q = bus.subscribe()

        nested = {"speaker": "Говорящий_1", "phrases": ["Краб", "🦀", "Ñoño"]}
        bus.emit("stt.partial", nested)

        event = q.get(timeout=1.0)
        self.assertEqual(event["data"]["speaker"], "Говорящий_1")
        self.assertIn("🦀", event["data"]["phrases"])

        bus.unsubscribe(q)


class TestHighVolumeNoLoss(unittest.TestCase):
    """test_high_volume_no_loss — 1000 events back-to-back, no events lost."""

    def test_high_volume_no_loss(self):
        """1000 events emitted sequentially — all received in order."""
        bus = EventBus()
        # Use a large-capacity queue to avoid drops
        big_q: queue.Queue = queue.Queue(maxsize=0)  # 0 = infinite
        with bus._lock:
            bus._subscribers.append(big_q)

        N = 1000
        for i in range(N):
            bus.emit("stt.partial", {"i": i})

        received = []
        for _ in range(N):
            event = big_q.get(timeout=2.0)
            received.append(event["data"]["i"])

        self.assertEqual(len(received), N)
        self.assertEqual(received, list(range(N)))

        with bus._lock:
            bus._subscribers.remove(big_q)

    def test_high_volume_two_subscribers(self):
        """1000 events — both subscribers receive all of them (unbounded queues)."""
        bus = EventBus()
        q1: queue.Queue = queue.Queue(maxsize=0)
        q2: queue.Queue = queue.Queue(maxsize=0)
        with bus._lock:
            bus._subscribers.append(q1)
            bus._subscribers.append(q2)

        N = 1000
        for i in range(N):
            bus.emit("stt.partial", {"i": i})

        for q in (q1, q2):
            received = []
            for _ in range(N):
                event = q.get(timeout=2.0)
                received.append(event["data"]["i"])
            self.assertEqual(len(received), N)
            self.assertEqual(received, list(range(N)))

        with bus._lock:
            bus._subscribers.remove(q1)
            bus._subscribers.remove(q2)

    def test_high_volume_threaded_emit(self):
        """1000 events emitted from a background thread — subscriber receives all."""
        bus = EventBus()
        big_q: queue.Queue = queue.Queue(maxsize=0)
        with bus._lock:
            bus._subscribers.append(big_q)

        N = 1000

        def emitter():
            for i in range(N):
                bus.emit("stt.partial", {"i": i})

        t = threading.Thread(target=emitter)
        t.start()
        t.join(timeout=10.0)

        received_count = 0
        while True:
            try:
                big_q.get_nowait()
                received_count += 1
            except queue.Empty:
                break

        self.assertEqual(received_count, N)

        with bus._lock:
            bus._subscribers.remove(big_q)


if __name__ == "__main__":
    unittest.main()
