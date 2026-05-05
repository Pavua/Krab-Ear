import threading
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

    def test_push_dedupe_with_canonical_error_registry_entry(self):
        """Registry value may be the canonical ERROR_REGISTRY _Entry dict
        (with ``dedupe_seconds``) rather than a flat float — both formats work.

        Regression: KRAB-EAR-BACKEND-7/8 — push() raised TypeError when the
        whole entry dict was compared to a numeric monotonic delta.
        """
        registry = {
            "rewriter.timeout": {
                "user_msg_ru": "...",
                "actionable": True,
                "action_id": "disable_rewriter",
                "action_label": "Выключить rewriter",
                "severity": "warn",
                "dedupe_seconds": 60,
            }
        }
        bus, event_bus = self._make_bus(registry=registry, default_dedupe_window_sec=5.0)

        first = bus.push(_make_err(code="rewriter.timeout"))
        second = bus.push(_make_err(code="rewriter.timeout"))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(event_bus.emit.call_count, 1)

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


# ---------------------------------------------------------------------------
# Task 4 tests: Sentry tier routing
# ---------------------------------------------------------------------------

def _make_err_component(
    code: str = "stt.empty_text",
    severity: str = "warn",
    component: str = "stt",
) -> KrabError:
    return KrabError(
        severity=severity,
        component=component,
        code=code,
        message_user="test user",
        message_debug="test debug",
        timestamp=datetime.now(timezone.utc),
        context={"k": "v"},
        actionable=False,
        action_id=None,
    )


class ErrorBusSentryRoutingTests(unittest.TestCase):

    def _make_bus_with_sentry(
        self,
        default_dedupe_window_sec: float = 0.0,
        warn_batch_size: int = 10,
        warn_window_sec: float = 30.0,
    ) -> tuple["ErrorBus", MagicMock, MagicMock]:
        event_bus = MagicMock()
        sentry = MagicMock()
        bus = ErrorBus(
            event_bus=event_bus,
            registry={},
            sentry_client=sentry,
            default_dedupe_window_sec=default_dedupe_window_sec,
            warn_batch_size=warn_batch_size,
            warn_window_sec=warn_window_sec,
        )
        return bus, event_bus, sentry

    def test_info_skipped(self):
        """push() with severity=info must NOT call sentry.capture_message."""
        bus, _, sentry = self._make_bus_with_sentry()
        err = _make_err_component(code="stt.empty_text", severity="info", component="stt")
        bus.push(err)
        sentry.capture_message.assert_not_called()

    def test_error_immediate(self):
        """push() with severity=error calls sentry.capture_message once with level='error'
        and tags containing phase='b' and the error code."""
        bus, _, sentry = self._make_bus_with_sentry()
        err = _make_err_component(code="paste.ax_denied", severity="error", component="paste")
        bus.push(err)
        sentry.capture_message.assert_called_once()
        call_kwargs = sentry.capture_message.call_args
        # message is positional arg 0
        self.assertIn("test debug", call_kwargs[0][0])
        self.assertEqual(call_kwargs[1]["level"], "error")
        tags = call_kwargs[1]["tags"]
        self.assertEqual(tags["phase"], "b")
        self.assertEqual(tags["code"], "paste.ax_denied")

    def test_critical_immediate(self):
        """push() with severity=critical calls sentry.capture_message with level='critical'."""
        bus, _, sentry = self._make_bus_with_sentry()
        err = _make_err_component(code="mlx.crash", severity="critical", component="mlx")
        bus.push(err)
        sentry.capture_message.assert_called_once()
        call_kwargs = sentry.capture_message.call_args
        self.assertEqual(call_kwargs[1]["level"], "critical")

    def test_warn_batched(self):
        """9 consecutive warn pushes must NOT call sentry.capture_message (batch not full)."""
        bus, _, sentry = self._make_bus_with_sentry(warn_batch_size=10, warn_window_sec=30.0)
        for i in range(9):
            bus._dedupe.pop("rewriter.timeout", None) if hasattr(bus, "_dedupe") else None
            bus._last_emitted.pop("rewriter.timeout", None)
            bus.push(_make_err_component(
                code="rewriter.timeout", severity="warn", component="rewriter"
            ))
        sentry.capture_message.assert_not_called()

    def test_warn_batch_flush_at_10(self):
        """10 consecutive warn pushes MUST call sentry.capture_message (batch flushed)."""
        bus, _, sentry = self._make_bus_with_sentry(warn_batch_size=10, warn_window_sec=30.0)
        for i in range(10):
            bus._last_emitted.pop("rewriter.timeout", None)
            bus.push(_make_err_component(
                code="rewriter.timeout", severity="warn", component="rewriter"
            ))
        sentry.capture_message.assert_called_once()
