import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pydantic

from backend.error_bus import ErrorBus, KrabError


class KrabErrorModelTests(unittest.TestCase):
    def test_minimal_valid_construction(self):
        err = KrabError(
            severity="warn",
            component="rewriter",
            code="rewriter.timeout",
            message_user="Rewriter недоступен",
            message_debug="HTTP timeout after 45s",
            timestamp=datetime.now(timezone.utc),
            context={"model": "gemma"},
            actionable=False,
            action_id=None,
        )
        self.assertEqual(err.severity, "warn")
        self.assertIsNone(err.action_id)

    def test_invalid_severity_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            KrabError(
                severity="catastrophic",  # not in Literal
                component="rewriter",
                code="rewriter.timeout",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_invalid_component_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            KrabError(
                severity="warn",
                component="nonexistent",  # not in Literal
                code="x.y",
                message_user="x",
                message_debug="x",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=False,
                action_id=None,
            )

    def test_model_dump_json_mode_serialises_datetime(self):
        ts = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        err = KrabError(
            severity="info",
            component="stt",
            code="stt.empty_text",
            message_user="x",
            message_debug="x",
            timestamp=ts,
            context={"k": "v"},
            actionable=False,
            action_id=None,
        )
        dumped = err.model_dump(mode="json")
        self.assertEqual(dumped["timestamp"], "2026-05-04T12:00:00+00:00")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_err(code: str = "stt.empty_text", severity: str = "warn") -> KrabError:
    return KrabError(
        severity=severity,
        component="stt",
        code=code,
        message_user="test",
        message_debug="test debug",
        timestamp=datetime.now(timezone.utc),
        context={},
        actionable=False,
        action_id=None,
    )


# ---------------------------------------------------------------------------
# Task 3 tests: ErrorBus
# ---------------------------------------------------------------------------

class ErrorBusPushTests(unittest.TestCase):

    def _make_bus(
        self,
        registry: dict | None = None,
        ring_buffer_size: int = 200,
        default_dedupe_window_sec: float = 30.0,
    ) -> tuple["ErrorBus", MagicMock]:
        event_bus = MagicMock()
        bus = ErrorBus(
            event_bus=event_bus,
            registry=registry if registry is not None else {},
            default_dedupe_window_sec=default_dedupe_window_sec,
            ring_buffer_size=ring_buffer_size,
        )
        return bus, event_bus

    def test_push_emits_event(self):
        """push() must call event_bus.emit('krab_error', payload_dict)."""
        bus, event_bus = self._make_bus()
        err = _make_err()
        result = bus.push(err)
        self.assertTrue(result)
        event_bus.emit.assert_called_once()
        call_args = event_bus.emit.call_args
        self.assertEqual(call_args[0][0], "krab_error")
        payload = call_args[0][1]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["code"], err.code)

    def test_push_dedupe_within_window(self):
        """Second push of same code within dedupe window returns False and does NOT emit again."""
        bus, event_bus = self._make_bus(default_dedupe_window_sec=60.0)
        err = _make_err(code="stt.empty_text")
        first = bus.push(err)
        second = bus.push(_make_err(code="stt.empty_text"))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(event_bus.emit.call_count, 1)

    def test_push_dedupe_per_code_window_from_registry(self):
        """Different codes can both push; same code respects registry window."""
        registry = {"paste.ax_denied": 60.0}
        bus, event_bus = self._make_bus(registry=registry, default_dedupe_window_sec=30.0)

        # paste.ax_denied has 60 s window — first push allowed
        ax_err = _make_err(code="paste.ax_denied")
        r1 = bus.push(ax_err)
        self.assertTrue(r1)

        # different code — not deduped
        other_err = _make_err(code="stt.empty_text")
        r2 = bus.push(other_err)
        self.assertTrue(r2)

        # same code again — deduped within 60 s
        r3 = bus.push(_make_err(code="paste.ax_denied"))
        self.assertFalse(r3)

        # total emits = 2 (paste.ax_denied + stt.empty_text)
        self.assertEqual(event_bus.emit.call_count, 2)

    def test_ring_buffer_caps_at_max(self):
        """Ring buffer must not grow beyond ring_buffer_size."""
        bus, _ = self._make_bus(ring_buffer_size=5, default_dedupe_window_sec=0.0)
        for i in range(20):
            bus.push(_make_err(code=f"stt.code_{i}"))
        recent = bus.list_recent(limit=200)
        self.assertLessEqual(len(recent), 5)

    def test_clear_returns_count(self):
        """clear() returns the number of items cleared; list_recent() is empty after."""
        bus, _ = self._make_bus(default_dedupe_window_sec=0.0)
        for i in range(3):
            bus.push(_make_err(code=f"stt.x_{i}"))
        count = bus.clear()
        self.assertEqual(count, 3)
        self.assertEqual(bus.list_recent(), [])

    def test_thread_safety_smoke(self):
        """10 threads × 100 pushes with distinct codes must not raise."""
        bus, _ = self._make_bus(default_dedupe_window_sec=0.0, ring_buffer_size=200)
        errors: list[Exception] = []

        def worker(tid: int) -> None:
            try:
                for i in range(100):
                    bus.push(_make_err(code=f"stt.t{tid}_i{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"Thread errors: {errors}")
