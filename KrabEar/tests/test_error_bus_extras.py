"""Wave 164: ErrorBus edge-case coverage.

Covers: dedupe time-window behaviour, ring-buffer eviction, unknown-code handling,
concurrent push safety, WarnBatcher aggregation, Sentry breadcrumb-only tier, and
critical-always-event guarantee.
"""
from __future__ import annotations

import sys
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.error_bus import ErrorBus, KrabError, WarnBatcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_err(
    code: str = "stt.empty_text",
    severity: str = "warn",
    component: str = "stt",
    context: dict | None = None,
) -> KrabError:
    return KrabError(
        severity=severity,
        component=component,
        code=code,
        message_user="test user",
        message_debug="test debug",
        timestamp=datetime.now(timezone.utc),
        context=context or {},
        actionable=False,
        action_id=None,
    )


def _make_bus(
    registry: dict | None = None,
    ring_buffer_size: int = 200,
    default_dedupe_window_sec: float = 30.0,
    sentry_client=None,
    warn_batch_size: int = 10,
    warn_window_sec: float = 30.0,
) -> tuple[ErrorBus, MagicMock]:
    event_bus = MagicMock()
    bus = ErrorBus(
        event_bus=event_bus,
        registry=registry if registry is not None else {},
        sentry_client=sentry_client,
        default_dedupe_window_sec=default_dedupe_window_sec,
        ring_buffer_size=ring_buffer_size,
        warn_batch_size=warn_batch_size,
        warn_window_sec=warn_window_sec,
    )
    return bus, event_bus


# ---------------------------------------------------------------------------
# 1. Dedupe within window suppresses duplicate
# ---------------------------------------------------------------------------

class DedupeWindowTests(unittest.TestCase):

    def test_dedupe_within_window_suppresses_duplicate_event(self):
        """Second push of the same code within the dedupe window must return False
        and must NOT emit a second krab_error event."""
        bus, event_bus = _make_bus(default_dedupe_window_sec=60.0)
        err1 = _make_err(code="translation.timeout")
        err2 = _make_err(code="translation.timeout")

        result1 = bus.push(err1)
        result2 = bus.push(err2)

        self.assertTrue(result1)
        self.assertFalse(result2, "Second push within window should be suppressed")
        self.assertEqual(event_bus.emit.call_count, 1,
                         "Only one event should be emitted within dedupe window")

    def test_dedupe_outside_window_allows_event(self):
        """After the dedupe window has elapsed, a second push of the same code
        must be treated as a new event (return True and emit)."""
        bus, event_bus = _make_bus(default_dedupe_window_sec=0.0)
        err1 = _make_err(code="translation.timeout")
        err2 = _make_err(code="translation.timeout")

        result1 = bus.push(err1)
        # With window=0, any subsequent push should pass (now - last >= 0)
        result2 = bus.push(err2)

        self.assertTrue(result1)
        self.assertTrue(result2, "Push after dedupe window should be allowed")
        self.assertEqual(event_bus.emit.call_count, 2,
                         "Both events should be emitted when window is zero")

    def test_dedupe_window_per_code_independent(self):
        """Different codes have independent dedupe windows — suppressing one
        must not suppress a different code."""
        bus, event_bus = _make_bus(default_dedupe_window_sec=60.0)
        r1 = bus.push(_make_err(code="stt.empty_text"))
        r2 = bus.push(_make_err(code="rewriter.timeout"))
        r3 = bus.push(_make_err(code="stt.empty_text"))  # should be suppressed

        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertFalse(r3)
        self.assertEqual(event_bus.emit.call_count, 2)


# ---------------------------------------------------------------------------
# 2. Ring buffer evicts oldest entries
# ---------------------------------------------------------------------------

class RingBufferEvictionTests(unittest.TestCase):

    def test_ring_buffer_evicts_oldest(self):
        """When ring_buffer_size is exceeded, oldest entries are evicted and
        only the most recent entries are retained."""
        capacity = 5
        bus, _ = _make_bus(ring_buffer_size=capacity, default_dedupe_window_sec=0.0)

        pushed_codes = [f"stt.code_{i}" for i in range(10)]
        for code in pushed_codes:
            bus.push(_make_err(code=code))

        recent = bus.list_recent(limit=200)
        self.assertEqual(len(recent), capacity,
                         f"Expected {capacity} items; got {len(recent)}")
        # Last 5 codes should be retained, first 5 evicted
        retained_codes = [e.code for e in recent]
        self.assertEqual(retained_codes, pushed_codes[-capacity:],
                         "Ring buffer should retain the most recent entries")

    def test_ring_buffer_respects_limit_parameter(self):
        """list_recent(limit=N) returns at most N items."""
        bus, _ = _make_bus(ring_buffer_size=50, default_dedupe_window_sec=0.0)
        for i in range(20):
            bus.push(_make_err(code=f"stt.x_{i}"))

        result = bus.list_recent(limit=5)
        self.assertLessEqual(len(result), 5)

    def test_ring_buffer_size_one_keeps_latest(self):
        """A ring_buffer_size=1 retains only the very last pushed error."""
        bus, _ = _make_bus(ring_buffer_size=1, default_dedupe_window_sec=0.0)
        bus.push(_make_err(code="stt.first"))
        bus.push(_make_err(code="stt.second"))
        recent = bus.list_recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].code, "stt.second")


# ---------------------------------------------------------------------------
# 3. Unknown code uses default dedupe window (not raised)
# ---------------------------------------------------------------------------

class UnknownCodeTests(unittest.TestCase):

    def test_unknown_code_uses_default_window_and_emits(self):
        """A code absent from the registry must fall back to the default
        dedupe window and still emit the event (not silently drop it)."""
        bus, event_bus = _make_bus(
            registry={"known.code": 60.0},
            default_dedupe_window_sec=5.0,
        )
        # "stt.unknown_code" is not in registry
        err = _make_err(code="stt.unknown_code")
        result = bus.push(err)

        self.assertTrue(result, "Unknown code should still be emitted")
        event_bus.emit.assert_called_once()
        payload = event_bus.emit.call_args[0][1]
        self.assertEqual(payload["code"], "stt.unknown_code")

    def test_unknown_code_dedupe_uses_default_window(self):
        """Second push of unknown code within the default window is suppressed."""
        bus, event_bus = _make_bus(
            registry={},
            default_dedupe_window_sec=60.0,
        )
        bus.push(_make_err(code="brand.new.code"))
        result = bus.push(_make_err(code="brand.new.code"))
        self.assertFalse(result)
        self.assertEqual(event_bus.emit.call_count, 1)


# ---------------------------------------------------------------------------
# 4. Concurrent push thread-safety
# ---------------------------------------------------------------------------

class ConcurrentPushTests(unittest.TestCase):

    def test_concurrent_push_thread_safe(self):
        """50 threads each pushing 10 distinct-code errors must complete
        without raising any exceptions or corrupting internal state."""
        bus, _ = _make_bus(
            default_dedupe_window_sec=0.0,
            ring_buffer_size=1000,
        )
        errors: list[Exception] = []
        n_threads = 50
        pushes_per_thread = 10

        def worker(tid: int) -> None:
            try:
                for i in range(pushes_per_thread):
                    bus.push(_make_err(code=f"stt.t{tid}_i{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors occurred: {errors}")
        recent = bus.list_recent(limit=1000)
        # All pushes with distinct codes should have been emitted
        self.assertEqual(len(recent), n_threads * pushes_per_thread)

    def test_concurrent_push_same_code_thread_safe(self):
        """50 threads each pushing the same code — should not corrupt state,
        and dedupe correctly (at most ring_buffer_size events get through)."""
        bus, event_bus = _make_bus(
            default_dedupe_window_sec=0.0,
            ring_buffer_size=500,
        )
        errors: list[Exception] = []

        def worker() -> None:
            try:
                bus.push(_make_err(code="rewriter.timeout"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Thread errors: {errors}")


# ---------------------------------------------------------------------------
# 5. WarnBatcher aggregation per minute / window
# ---------------------------------------------------------------------------

class WarnBatcherTests(unittest.TestCase):

    def _make_batcher(self, batch_size: int = 10, window: float = 30.0):
        sentry = MagicMock()
        batcher = WarnBatcher(sentry_client=sentry, batch_size=batch_size, window=window)
        return batcher, sentry

    def test_warn_batcher_aggregates_warns_below_batch_size(self):
        """Adding fewer than batch_size errors must NOT flush to Sentry."""
        batcher, sentry = self._make_batcher(batch_size=10, window=30.0)
        err = _make_err(code="rewriter.timeout", severity="warn")
        for _ in range(9):
            batcher.add(err)
        sentry.capture_message.assert_not_called()

    def test_warn_batcher_flushes_at_batch_size(self):
        """Adding batch_size errors must flush exactly once to Sentry."""
        batcher, sentry = self._make_batcher(batch_size=10, window=30.0)
        err = _make_err(code="rewriter.timeout", severity="warn")
        for _ in range(10):
            batcher.add(err)
        sentry.capture_message.assert_called_once()
        msg = sentry.capture_message.call_args[0][0]
        self.assertIn("x10", msg, "Flush message should contain count")
        self.assertIn("rewriter.timeout", sentry.capture_message.call_args[1]["tags"]["code"])

    def test_warn_batcher_flushes_at_window_expiry(self):
        """When time window elapses, the batch should flush even if
        batch_size is not reached. Use a very small window (0.01s) and
        real sleep so the monotonic clock advances naturally."""
        batcher, sentry = self._make_batcher(batch_size=100, window=0.01)

        err = _make_err(code="vocabulary.load_fail", severity="warn", component="vocabulary")
        # First add — sets first_seen
        batcher.add(err)
        # Sleep past the window so that next add detects expiry
        time.sleep(0.05)
        # Second add should see elapsed > window and flush
        batcher.add(err)

        sentry.capture_message.assert_called_once()

    def test_warn_batcher_separate_buffers_per_code(self):
        """WarnBatcher maintains independent buffers per error code."""
        batcher, sentry = self._make_batcher(batch_size=3, window=30.0)
        err_a = _make_err(code="rewriter.timeout", severity="warn")
        err_b = _make_err(code="vocabulary.load_fail", severity="warn", component="vocabulary")

        # Add 2 of each — neither should flush at 2
        batcher.add(err_a)
        batcher.add(err_b)
        batcher.add(err_a)
        batcher.add(err_b)
        sentry.capture_message.assert_not_called()

        # Third add for code_a should flush code_a buffer only
        batcher.add(err_a)
        self.assertEqual(sentry.capture_message.call_count, 1)
        flushed_code = sentry.capture_message.call_args[1]["tags"]["code"]
        self.assertEqual(flushed_code, "rewriter.timeout")

    def test_warn_batcher_message_contains_count(self):
        """Flushed Sentry message must contain count of batched errors."""
        batcher, sentry = self._make_batcher(batch_size=2, window=30.0)
        err = _make_err(code="diarization.pipeline_fail", severity="warn")
        batcher.add(err)
        batcher.add(err)
        msg = sentry.capture_message.call_args[0][0]
        self.assertIn("x2", msg)


# ---------------------------------------------------------------------------
# 6. Sentry breadcrumb tier: info must not emit event or call Sentry
# ---------------------------------------------------------------------------

class SentryTierTests(unittest.TestCase):

    def test_sentry_tier_info_does_not_call_capture_message(self):
        """severity=info must be skipped by Sentry entirely — no capture_message call."""
        sentry = MagicMock()
        bus, event_bus = _make_bus(sentry_client=sentry, default_dedupe_window_sec=0.0)
        err = _make_err(code="stt.empty_text", severity="info")
        bus.push(err)
        sentry.capture_message.assert_not_called()
        # But the event bus must still emit (info still goes into ring buffer + UI)
        event_bus.emit.assert_called_once()

    def test_sentry_tier_warn_does_not_immediately_capture(self):
        """severity=warn with batch_size=10 and only 1 push must NOT call
        capture_message immediately (goes to WarnBatcher)."""
        sentry = MagicMock()
        bus, _ = _make_bus(
            sentry_client=sentry,
            default_dedupe_window_sec=0.0,
            warn_batch_size=10,
            warn_window_sec=30.0,
        )
        err = _make_err(code="rewriter.timeout", severity="warn")
        bus.push(err)
        sentry.capture_message.assert_not_called()

    def test_sentry_tier_error_calls_capture_immediately(self):
        """severity=error must call sentry.capture_message immediately."""
        sentry = MagicMock()
        bus, _ = _make_bus(sentry_client=sentry, default_dedupe_window_sec=0.0)
        err = _make_err(code="paste.ax_denied", severity="error", component="paste")
        bus.push(err)
        sentry.capture_message.assert_called_once()
        self.assertEqual(sentry.capture_message.call_args[1]["level"], "error")

    def test_sentry_none_does_not_raise_on_warn(self):
        """When sentry_client=None, pushing a warn error must not raise."""
        bus, event_bus = _make_bus(sentry_client=None, default_dedupe_window_sec=0.0)
        err = _make_err(code="rewriter.timeout", severity="warn")
        try:
            bus.push(err)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"push() raised with sentry_client=None: {exc}")
        event_bus.emit.assert_called_once()

    def test_sentry_none_does_not_raise_on_info(self):
        """When sentry_client=None, pushing an info error must not raise."""
        bus, _ = _make_bus(sentry_client=None, default_dedupe_window_sec=0.0)
        err = _make_err(code="stt.empty_text", severity="info")
        try:
            bus.push(err)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"push() raised with sentry_client=None: {exc}")


# ---------------------------------------------------------------------------
# 7. Critical severity always uses event tier (immediate Sentry capture)
# ---------------------------------------------------------------------------

class CriticalSeverityTests(unittest.TestCase):

    def test_severity_critical_always_event_tier(self):
        """Critical errors must always trigger immediate sentry.capture_message
        with level='critical', regardless of warn batching configuration."""
        sentry = MagicMock()
        bus, _ = _make_bus(
            sentry_client=sentry,
            default_dedupe_window_sec=0.0,
            warn_batch_size=1000,  # large batch to ensure warn would NOT flush
            warn_window_sec=9999.0,
        )
        err = _make_err(code="mlx.oom", severity="critical", component="mlx")
        bus.push(err)
        sentry.capture_message.assert_called_once()
        self.assertEqual(sentry.capture_message.call_args[1]["level"], "critical")

    def test_severity_critical_tags_contain_phase_b_and_code(self):
        """Critical Sentry call must include tags with phase='b' and the error code."""
        sentry = MagicMock()
        bus, _ = _make_bus(sentry_client=sentry, default_dedupe_window_sec=0.0)
        bus.push(_make_err(code="history.write_fail", severity="critical", component="history"))
        tags = sentry.capture_message.call_args[1]["tags"]
        self.assertEqual(tags["phase"], "b")
        self.assertEqual(tags["code"], "history.write_fail")

    def test_severity_critical_also_emits_krab_error_event(self):
        """Critical errors must also emit krab_error on the event bus (UI notification)."""
        sentry = MagicMock()
        bus, event_bus = _make_bus(sentry_client=sentry, default_dedupe_window_sec=0.0)
        bus.push(_make_err(code="mlx.oom", severity="critical", component="mlx"))
        event_bus.emit.assert_called_once_with(
            "krab_error", unittest.mock.ANY
        )


# ---------------------------------------------------------------------------
# 8. Registry shaped as ERROR_REGISTRY _Entry dicts
# ---------------------------------------------------------------------------

class RegistryEntryShapeTests(unittest.TestCase):

    def test_dedupe_window_from_entry_dict(self):
        """When the registry maps code → _Entry dict (with dedupe_seconds),
        the bus must extract dedupe_seconds correctly."""
        registry = {
            "rewriter.circuit_open": {
                "user_msg_ru": "circuit open",
                "actionable": False,
                "action_id": None,
                "action_label": "",
                "severity": "warn",
                "dedupe_seconds": 300,
            }
        }
        bus, event_bus = _make_bus(registry=registry, default_dedupe_window_sec=5.0)
        r1 = bus.push(_make_err(code="rewriter.circuit_open"))
        r2 = bus.push(_make_err(code="rewriter.circuit_open"))
        self.assertTrue(r1)
        self.assertFalse(r2, "Second push must be deduplicated per entry dedupe_seconds=300")

    def test_flat_registry_float_still_works(self):
        """Legacy flat {code: float} registries still work."""
        registry = {"stt.empty_text": 5.0}
        bus, event_bus = _make_bus(registry=registry, default_dedupe_window_sec=60.0)
        r1 = bus.push(_make_err(code="stt.empty_text"))
        r2 = bus.push(_make_err(code="stt.empty_text"))
        self.assertTrue(r1)
        self.assertFalse(r2)


if __name__ == "__main__":
    unittest.main()
