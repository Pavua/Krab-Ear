"""Тесты интеграции WarnBatcher/ErrorBus flush с graceful shutdown.

Wave 1234 закрывает W1231 F3 MED: ``flush_all`` должен сбросить накопленные
ошибки, а metadata-handler — вызвать ErrorBus hook без signal-регистрации.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_bus import ErrorBus, KrabError, WarnBatcher
from backend.shutdown_handler import GracefulShutdownHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_warn_err(code: str = "rewriter.timeout") -> KrabError:
    return KrabError(
        severity="warn",
        component="rewriter",
        code=code,
        message_user="Warn msg",
        message_debug=f"debug {code}",
        timestamp=datetime.now(timezone.utc),
        context={"detail": "x"},
        actionable=False,
        action_id=None,
    )


# ---------------------------------------------------------------------------
# WarnBatcher.flush_all tests
# ---------------------------------------------------------------------------

class WarnBatcherFlushAllTests(unittest.TestCase):
    def _make_batcher(self, batch_size: int = 100, window: float = 9999.0):
        sentry = MagicMock()
        batcher = WarnBatcher(sentry_client=sentry, batch_size=batch_size, window=window)
        return batcher, sentry

    def test_warn_batcher_flush_all_drains_pending(self):
        """flush_all() sends all pending codes to Sentry and empties the buffer."""
        batcher, sentry = self._make_batcher()
        batcher.add(_make_warn_err("code.a"))
        batcher.add(_make_warn_err("code.a"))
        batcher.add(_make_warn_err("code.b"))

        # Before flush, buffer should still have items (batch_size=100 not reached)
        self.assertIn("code.a", batcher._buffer)
        self.assertIn("code.b", batcher._buffer)

        batcher.flush_all()

        # After flush: buffer must be empty
        self.assertEqual(batcher._buffer, {})
        self.assertEqual(batcher._first_seen, {})

        # Sentry must have been called once per code
        self.assertEqual(sentry.capture_message.call_count, 2)

    def test_warn_batcher_flush_all_returns_count(self):
        """flush_all() returns the total number of individual errors flushed."""
        batcher, _ = self._make_batcher()
        batcher.add(_make_warn_err("code.x"))
        batcher.add(_make_warn_err("code.x"))
        batcher.add(_make_warn_err("code.y"))

        count = batcher.flush_all()

        self.assertEqual(count, 3)

    def test_warn_batcher_flush_all_empty_returns_zero(self):
        """flush_all() returns 0 when no pending batches exist."""
        batcher, sentry = self._make_batcher()
        count = batcher.flush_all()
        self.assertEqual(count, 0)
        sentry.capture_message.assert_not_called()

    def test_warn_batcher_flush_all_idempotent(self):
        """Second flush_all() on already-flushed batcher returns 0."""
        batcher, sentry = self._make_batcher()
        batcher.add(_make_warn_err("code.z"))
        batcher.flush_all()
        sentry.reset_mock()

        count2 = batcher.flush_all()
        self.assertEqual(count2, 0)
        sentry.capture_message.assert_not_called()


# ---------------------------------------------------------------------------
# ErrorBus.flush_all tests
# ---------------------------------------------------------------------------

class ErrorBusFlushAllTests(unittest.TestCase):
    def _make_bus(self):
        event_bus = MagicMock()
        sentry = MagicMock()
        bus = ErrorBus(
            event_bus=event_bus,
            registry={},
            sentry_client=sentry,
            warn_batch_size=100,   # high threshold — won't auto-flush
            warn_window_sec=9999.0,
        )
        return bus, sentry

    def test_error_bus_flush_all_delegates_to_batcher(self):
        """ErrorBus.flush_all() invokes WarnBatcher.flush_all() and returns count."""
        bus, sentry = self._make_bus()

        # Push 3 warn errors with different codes to bypass dedupe window.
        # ErrorBus.push() dedupes per-code; use distinct codes to get 3 items
        # into the WarnBatcher buffer.
        bus.push(_make_warn_err("rewriter.timeout"))
        bus.push(_make_warn_err("rewriter.chatbot"))
        bus.push(_make_warn_err("stt.model_unavailable"))

        count = bus.flush_all()

        self.assertEqual(count, 3)
        # Sentry must have been called (once per batch/code)
        self.assertTrue(sentry.capture_message.called)

    def test_error_bus_flush_all_no_batcher_returns_zero(self):
        """ErrorBus.flush_all() returns 0 when Sentry is disabled (no batcher)."""
        event_bus = MagicMock()
        bus = ErrorBus(event_bus=event_bus, registry={}, sentry_client=None)
        count = bus.flush_all()
        self.assertEqual(count, 0)

    def test_error_bus_flush_all_empty_batcher_returns_zero(self):
        """ErrorBus.flush_all() returns 0 when WarnBatcher has no pending items."""
        bus, _ = self._make_bus()
        count = bus.flush_all()
        self.assertEqual(count, 0)


# ---------------------------------------------------------------------------
# GracefulShutdownHandler error_bus close hook tests
# ---------------------------------------------------------------------------

class _FakeIPCServer:
    """Минимальная duck-typed заглушка IPC-контракта (F3, приёмка 2026-07-23).

    Эти тесты проверяют error_bus flush через bind()+shutdown(), а не сам
    IPC-слой — им нужна лишь форма контракта, не поведение.
    """

    def stop(self, *args, **kwargs):
        return True

    def request_stop_from_signal(self):
        pass


class ShutdownHandlerErrorBusTests(unittest.TestCase):
    def _make_service(self, error_bus=None):
        svc = MagicMock()
        svc.vocabulary = None
        svc._audit_logger = None
        svc._usage_tracker = None
        svc._playback_tracker = None
        svc.store = MagicMock()
        svc.store.maybe_compact.return_value = False
        svc.store.data_dir = None
        # F3 (приёмка 2026-07-23): bind() требует полный IPC-контракт — тесты
        # этого файла про error_bus, не про IPC, но обязаны дать duck-typed
        # заглушку, иначе строгая валидация (справедливо) их отклонит.
        svc._ipc_server = _FakeIPCServer()
        if error_bus is not None:
            svc._error_bus = error_bus
        else:
            # Ensure no _error_bus attribute (simulate None case)
            del svc._error_bus
        return svc

    def test_shutdown_handler_calls_error_bus_flush(self):
        """GracefulShutdownHandler.shutdown() calls error_bus.flush_all()."""
        mock_bus = MagicMock()
        mock_bus.flush_all.return_value = 5

        handler = GracefulShutdownHandler(data_dir=None, error_bus=mock_bus)
        svc = self._make_service()
        handler.bind(svc)
        handler.shutdown()

        mock_bus.flush_all.assert_called_once()

    def test_shutdown_handler_flush_count_logged(self):
        """flush_all returning >0 should not raise."""
        mock_bus = MagicMock()
        mock_bus.flush_all.return_value = 3

        handler = GracefulShutdownHandler(data_dir=None, error_bus=mock_bus)
        svc = self._make_service()
        handler.bind(svc)
        # Should not raise
        handler.shutdown()

        mock_bus.flush_all.assert_called_once()

    def test_shutdown_handler_no_error_bus_does_not_raise(self):
        """GracefulShutdownHandler.shutdown() is safe when error_bus is None."""
        handler = GracefulShutdownHandler(data_dir=None, error_bus=None)
        svc = self._make_service()
        handler.bind(svc)
        # Should complete without exceptions
        handler.shutdown()

    def test_shutdown_handler_falls_back_to_service_error_bus(self):
        """_close_error_bus() falls back to service._error_bus when handler has no bus."""
        mock_bus = MagicMock()
        mock_bus.flush_all.return_value = 2

        # No error_bus passed to constructor
        handler = GracefulShutdownHandler(data_dir=None, error_bus=None)
        svc = MagicMock()
        svc.vocabulary = None
        svc._audit_logger = None
        svc._usage_tracker = None
        svc._playback_tracker = None
        svc.store = MagicMock()
        svc.store.maybe_compact.return_value = False
        svc._ipc_server = _FakeIPCServer()  # F3: bind() требует контракт
        svc._error_bus = mock_bus  # Service has the bus

        handler.bind(svc)
        handler.shutdown()

        mock_bus.flush_all.assert_called_once()

    def test_shutdown_handler_error_bus_flush_exception_does_not_abort(self):
        """If flush_all() raises, shutdown still completes (error captured, clean=False)."""
        mock_bus = MagicMock()
        mock_bus.flush_all.side_effect = RuntimeError("sentry down")

        handler = GracefulShutdownHandler(data_dir=None, error_bus=mock_bus)
        svc = self._make_service()
        handler.bind(svc)
        # Must not propagate the exception — clean shutdown with error recorded
        handler.shutdown()

        # shutdown completed (event is set)
        self.assertTrue(handler._shutdown_done.is_set())


if __name__ == "__main__":
    unittest.main()
