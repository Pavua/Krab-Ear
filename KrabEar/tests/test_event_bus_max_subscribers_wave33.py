"""Tests for EventBus wave-33 fixes:
  E1 (MED) — MAX_SUBSCRIBERS cap in subscribe()
  E2 (LOW) — len(self._subscribers) read now snapshotted under lock
  E3 (LOW) — auth-note documentation smoke-check
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import EventBus, MAX_SUBSCRIBERS  # noqa: E402


class TestMaxSubscribersCap(unittest.TestCase):
    """E1 (MED): subscribe() must reject the 101st connection."""

    def test_exactly_max_subscribers_succeeds(self) -> None:
        """100 subscribers all succeed; no exception raised."""
        bus = EventBus()
        queues = []
        for _ in range(MAX_SUBSCRIBERS):
            queues.append(bus.subscribe())
        self.assertEqual(bus.subscriber_count(), MAX_SUBSCRIBERS)
        # Cleanup
        for q in queues:
            bus.unsubscribe(q)

    def test_one_over_max_raises_runtime_error(self) -> None:
        """101st subscribe() raises RuntimeError."""
        bus = EventBus()
        queues = []
        for _ in range(MAX_SUBSCRIBERS):
            queues.append(bus.subscribe())

        with self.assertRaises(RuntimeError) as ctx:
            bus.subscribe()

        self.assertIn("max_subscribers", str(ctx.exception).lower())
        # Cleanup
        for q in queues:
            bus.unsubscribe(q)

    def test_unsubscribe_reduces_count_below_cap(self) -> None:
        """After unsubscribing one slot, a new subscribe() succeeds."""
        bus = EventBus()
        queues = []
        for _ in range(MAX_SUBSCRIBERS):
            queues.append(bus.subscribe())

        # Remove one subscriber → count drops below cap
        freed = queues.pop()
        bus.unsubscribe(freed)
        self.assertEqual(bus.subscriber_count(), MAX_SUBSCRIBERS - 1)

        # Now a new subscription fits
        new_q = bus.subscribe()
        self.assertEqual(bus.subscriber_count(), MAX_SUBSCRIBERS)

        # Cleanup
        bus.unsubscribe(new_q)
        for q in queues:
            bus.unsubscribe(q)

    def test_cap_is_constant_100(self) -> None:
        """MAX_SUBSCRIBERS is exactly 100."""
        self.assertEqual(MAX_SUBSCRIBERS, 100)


class TestSubscriberCountUnderLock(unittest.TestCase):
    """E2 (LOW): count snapshots are taken under the lock — no torn reads."""

    def test_subscribe_returns_queue_and_count_increases(self) -> None:
        """subscribe() returns a valid Queue and counter is consistent."""
        bus = EventBus()
        import queue as _q_mod
        q = bus.subscribe()
        self.assertIsInstance(q, _q_mod.Queue)
        self.assertEqual(bus.subscriber_count(), 1)
        bus.unsubscribe(q)
        self.assertEqual(bus.subscriber_count(), 0)

    def test_concurrent_subscribe_unsubscribe_no_race(self) -> None:
        """Concurrent subscribe + unsubscribe don't corrupt the count."""
        bus = EventBus()
        errors: list[Exception] = []
        queues_lock = threading.Lock()
        shared_queues: list = []

        def do_subscribe() -> None:
            try:
                q = bus.subscribe()
                with queues_lock:
                    shared_queues.append(q)
            except RuntimeError:
                # Hit the cap — expected under heavy concurrency
                pass
            except Exception as e:
                errors.append(e)

        def do_unsubscribe() -> None:
            try:
                with queues_lock:
                    if shared_queues:
                        q = shared_queues.pop()
                    else:
                        return
                bus.unsubscribe(q)
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(30):
            threads.append(threading.Thread(target=do_subscribe))
            threads.append(threading.Thread(target=do_unsubscribe))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No unexpected exceptions
        self.assertEqual(errors, [], f"Race errors: {errors}")
        # count must be non-negative and ≤ MAX_SUBSCRIBERS
        count = bus.subscriber_count()
        self.assertGreaterEqual(count, 0)
        self.assertLessEqual(count, MAX_SUBSCRIBERS)
        # Cleanup remaining
        with queues_lock:
            for q in shared_queues:
                bus.unsubscribe(q)


class TestSubscribeDocstring(unittest.TestCase):
    """E3 (LOW): subscribe() docstring must mention auth is at HTTP layer."""

    def test_subscribe_docstring_mentions_auth(self) -> None:
        """subscribe() docstring must reference HTTP-layer auth (require_auth)."""
        doc = EventBus.subscribe.__doc__ or ""
        self.assertIn(
            "require_auth",
            doc,
            "subscribe() docstring must mention that SSE auth is enforced at the "
            "HTTP layer via require_auth in rest_server.py (E3 LOW fix).",
        )

    def test_subscribe_docstring_mentions_max_subscribers(self) -> None:
        """subscribe() docstring must mention MAX_SUBSCRIBERS cap."""
        doc = EventBus.subscribe.__doc__ or ""
        self.assertIn(
            "MAX_SUBSCRIBERS",
            doc,
            "subscribe() docstring must document the RuntimeError raised when "
            "MAX_SUBSCRIBERS is reached (E1 MED fix).",
        )


if __name__ == "__main__":
    unittest.main()
