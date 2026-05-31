"""Юнит-тесты для EventBus (KrabEar/backend/event_bus.py)."""

from __future__ import annotations
from contracts.stt_events import SttFailed, SttFinal
from contracts.registry import EventType
from backend.event_bus import EventBus

import queue
import sys
import threading
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


class TestFullQueueIsolation(unittest.TestCase):
    """Переполненная очередь одного подписчика не блокирует доставку другим."""

    def test_full_queue_does_not_block_others(self) -> None:
        from backend.event_bus import _QUEUE_MAXSIZE

        bus = EventBus()
        q_fast = bus.subscribe()
        q_full = bus.subscribe()

        # Заполняем очередь q_full до предела
        for i in range(_QUEUE_MAXSIZE):
            q_full.put_nowait({"type": "fill", "ts": "t", "data": {"i": i}})

        # Эмитируем ещё одно событие — q_full должна пропустить его (Full),
        # но q_fast обязана его получить
        bus.emit("stt.final", {"text": "isolated"})

        event = q_fast.get_nowait()
        self.assertEqual(event["type"], "stt.final")
        self.assertEqual(event["data"]["text"], "isolated")

        # q_full не приняла новое событие (была переполнена)
        # Все элементы в очереди — заглушки из цикла заполнения
        count = 0
        while True:
            try:
                item = q_full.get_nowait()
                if item.get("type") == "stt.final":
                    self.fail("q_full не должна была получить новое событие")
                count += 1
            except queue.Empty:
                break
        self.assertEqual(count, _QUEUE_MAXSIZE)


class TestSseStreamFormat(unittest.TestCase):
    """SSE-поток: события отдаются в правильном формате event:/data:."""

    def test_sse_event_format(self) -> None:
        """SSE-генератор форматирует события как event:/data: с двойным переносом."""
        import json
        from backend.event_bus import sse_stream

        bus = EventBus()

        # Нужно сначала создать генератор (он внутри вызывает subscribe),
        # затем эмитировать событие чтобы оно попало в очередь подписчика.
        # Используем threading: генератор ждёт событие в отдельном потоке.
        import threading

        result: list[str] = []

        def _run_gen():
            gen = sse_stream(bus)
            try:
                result.append(next(gen))
            finally:
                gen.close()

        t = threading.Thread(target=_run_gen, daemon=True)
        t.start()

        # Даём потоку время подписаться, затем эмитируем
        import time
        time.sleep(0.05)
        bus.emit("stt.partial", {"text": "привет"})
        t.join(timeout=5.0)

        self.assertEqual(len(result), 1, "Генератор должен был вернуть один фрейм")
        first_chunk = result[0]

        self.assertTrue(
            first_chunk.startswith("event: stt.partial\n"),
            f"Неожиданный SSE-фрейм: {first_chunk!r}",
        )
        self.assertIn("data: ", first_chunk)
        self.assertTrue(
            first_chunk.endswith("\n\n"),
            "SSE-фрейм должен заканчиваться на \\n\\n",
        )

        data_line = [ln for ln in first_chunk.splitlines() if ln.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        self.assertEqual(payload["text"], "привет")

    def test_sse_keepalive_on_timeout(self) -> None:
        """При отсутствии событий генератор возвращает keepalive-комментарий."""
        from backend.event_bus import sse_stream
        from unittest.mock import patch

        bus = EventBus()

        with patch.object(bus, 'subscribe', side_effect=lambda: _make_timeout_then_stop_queue()):
            gen = sse_stream(bus)
            chunk = next(gen)
            self.assertEqual(chunk, ": keepalive\n\n")
            gen.close()


def _make_timeout_then_stop_queue():
    """Вспомогательная очередь: первый get() -> Empty, второй get() -> None."""
    import queue as q_mod

    class _OneTimeoutQueue(q_mod.Queue):
        _calls = 0

        def get(self, block=True, timeout=None):  # noqa: D102
            self._calls += 1
            if self._calls == 1:
                raise q_mod.Empty
            return None  # сигнал завершения

    return _OneTimeoutQueue()


class TestShutdownSentinelOnFullQueue(unittest.TestCase):
    """W1716 BUG 1: sentinel must be delivered even when the subscriber queue is full.

    Before the fix, broadcast_shutdown_sentinel() did q.put_nowait(None) and
    swallowed queue.Full — so a slow SSE consumer whose queue had filled to
    _QUEUE_MAXSIZE never received the sentinel and stalled for the full
    _SSE_POLL_TIMEOUT_SEC (15 s) instead of disconnecting immediately.
    """

    def test_sentinel_delivered_when_queue_full(self) -> None:
        """Sentinel arrives even if the queue was filled to capacity before shutdown."""
        from backend.event_bus import _QUEUE_MAXSIZE

        bus = EventBus()
        q = bus.subscribe()

        # Fill queue to the brim with regular events
        for i in range(_QUEUE_MAXSIZE):
            q.put_nowait({"type": "audio.level", "ts": "t", "data": {"i": i}})

        self.assertTrue(q.full(), "Pre-condition: queue must be full before the call")

        # broadcast_shutdown_sentinel must drain then put the sentinel
        sent = bus.broadcast_shutdown_sentinel()
        self.assertEqual(sent, 1, "Sentinel should have been sent to the 1 subscriber")

        # The sentinel (None) must be the only item left in the queue now
        sentinel = q.get_nowait()
        self.assertIsNone(sentinel, "Sentinel (None) must be the item in the queue after drain")

        # Queue should be empty afterwards
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_sentinel_delivered_to_multiple_full_queues(self) -> None:
        """All full subscriber queues are drained and each receives the sentinel."""
        from backend.event_bus import _QUEUE_MAXSIZE

        bus = EventBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        for i in range(_QUEUE_MAXSIZE):
            q1.put_nowait({"type": "fill", "ts": "t", "data": {}})
            q2.put_nowait({"type": "fill", "ts": "t", "data": {}})

        sent = bus.broadcast_shutdown_sentinel()
        self.assertEqual(sent, 2)

        self.assertIsNone(q1.get_nowait())
        self.assertIsNone(q2.get_nowait())

    def test_sentinel_count_correct_for_empty_queues(self) -> None:
        """When queues are NOT full the return value is still correct."""
        bus = EventBus()
        bus.subscribe()
        bus.subscribe()
        bus.subscribe()

        sent = bus.broadcast_shutdown_sentinel()
        self.assertEqual(sent, 3)


class TestSseStreamEmptyFilter(unittest.TestCase):
    """W1716 BUG 2: an empty-after-parse event_filter must mean 'receive all',
    not 'receive nothing' (silent blackhole).

    Before the fix, event_filter=',' or event_filter=' ' produced allowed=set()
    (empty set, not None).  The guard `event["type"] not in allowed` was then True
    for every event, so the SSE stream silently discarded all events and yielded
    only keepalives.
    """

    def _collect_first_event(self, event_filter: str) -> str | None:
        """Run sse_stream with the given filter, emit one event, return the chunk."""
        import time
        from backend.event_bus import sse_stream

        bus = EventBus()
        result: list[str] = []

        def _run() -> None:
            gen = sse_stream(bus, event_filter=event_filter)
            try:
                result.append(next(gen))
            finally:
                gen.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.05)  # give the thread time to subscribe

        bus.emit("stt.final", {"text": "hello"})
        t.join(timeout=5.0)

        return result[0] if result else None

    def test_comma_only_filter_receives_all(self) -> None:
        """event_filter=',' (comma only) must NOT blackhole — events pass through."""
        chunk = self._collect_first_event(",")
        self.assertIsNotNone(chunk, "No chunk received — events were silently blackholed")
        self.assertIn("stt.final", chunk)

    def test_whitespace_filter_receives_all(self) -> None:
        """event_filter=' ' (spaces only) must NOT blackhole — events pass through."""
        chunk = self._collect_first_event("  ")
        self.assertIsNotNone(chunk, "No chunk received — events were silently blackholed")
        self.assertIn("stt.final", chunk)

    def test_comma_separated_blanks_receives_all(self) -> None:
        """event_filter=' , , ' (all blank tokens) must NOT blackhole."""
        chunk = self._collect_first_event(" , , ")
        self.assertIsNotNone(chunk, "No chunk received — events were silently blackholed")
        self.assertIn("stt.final", chunk)

    def test_valid_filter_still_filters(self) -> None:
        """A non-empty valid filter must still filter out non-matching events."""
        import time
        from backend.event_bus import sse_stream

        bus = EventBus()
        received: list[str] = []

        def _run() -> None:
            gen = sse_stream(bus, event_filter="stt.failed")
            try:
                for chunk in gen:
                    received.append(chunk)
                    break
            finally:
                gen.close()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        time.sleep(0.05)

        # Emit a non-matching event — should be filtered
        bus.emit("stt.final", {"text": "filtered out"})
        # Emit matching event — should get through
        bus.emit("stt.failed", {"reason": "timeout"})

        t.join(timeout=5.0)

        self.assertEqual(len(received), 1)
        self.assertIn("stt.failed", received[0])


if __name__ == "__main__":
    unittest.main()
