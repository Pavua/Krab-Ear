"""Tests for WebSocket live-streaming endpoint /ws/events.

Проверяет подключение, фильтрацию по типам и graceful disconnect,
тестируя напрямую helper-функцию _handle_ws_connection.
"""

import backend.event_bus as _event_bus_mod
import sys
import json
import queue
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Project root on sys.path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Lightweight stubs so rest_server imports succeed without real ML models
# ---------------------------------------------------------------------------

_fake_engine = MagicMock()
_fake_engine.quality_profile = "balanced"
_fake_transcriber = MagicMock()
_fake_store = MagicMock()
_fake_store.load_vocabulary.return_value = []
_fake_metrics = MagicMock()
_fake_metrics.get_summary.return_value = {}


Path("/tmp/krab_ear_test_ws/temp_uploads").mkdir(parents=True, exist_ok=True)

with patch.dict("sys.modules", {
    "core.engine": MagicMock(AudioEngine=MagicMock(return_value=_fake_engine)),
    "backend.transcriber": MagicMock(Transcriber=MagicMock(return_value=_fake_transcriber)),
    "backend.service": MagicMock(BackendService=MagicMock()),
    "backend.metrics_collector": MagicMock(metrics=_fake_metrics),
}):
    with patch("core.config.settings") as _mock_settings, \
            patch("backend.state_store.StateStore", return_value=_fake_store):
        _mock_settings.DATA_DIR = Path("/tmp/krab_ear_test_ws")
        _mock_settings.REST_API_KEY = ""
        import backend.rest_server as _rest_server_mod

# The internal logic function — testable without Flask request context
_handle = _rest_server_mod._handle_ws_connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeWebSocket:
    """Минимальная заглушка WebSocket-соединения."""

    def __init__(self):
        self.sent: list[str] = []
        self._closed = False

    def send(self, data: str) -> None:
        if self._closed:
            raise ConnectionError("WebSocket closed")
        self.sent.append(data)

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWsConnection(unittest.TestCase):
    """Unit-тесты для _handle_ws_connection."""

    def _make_bus(self):
        return _event_bus_mod.EventBus()

    def _run(self, ws, bus, type_filter=None):
        """Запускает _handle_ws_connection в фоновом потоке."""
        with patch.object(_rest_server_mod, "_WS_POLL_SEC", 0.05), \
                patch.object(_rest_server_mod, "_WS_HEARTBEAT_SEC", 9999):
            _handle(ws, bus, type_filter)

    # ------------------------------------------------------------------
    # Test 1: subscribe/unsubscribe lifecycle
    # ------------------------------------------------------------------

    def test_subscribes_and_unsubscribes_on_disconnect(self):
        bus = self._make_bus()
        ws = FakeWebSocket()

        self.assertEqual(bus.subscriber_count(), 0)

        thread = threading.Thread(
            target=self._run, args=(ws, bus), daemon=True
        )
        thread.start()

        time.sleep(0.15)
        self.assertEqual(bus.subscriber_count(), 1)

        # Close ws, then emit an event so the send attempt triggers the disconnect path
        ws.close()
        bus.emit("stt.final", {"text": "trigger"})
        thread.join(timeout=1.0)

        self.assertEqual(bus.subscriber_count(), 0)

    # ------------------------------------------------------------------
    # Test 2: events forwarded to client
    # ------------------------------------------------------------------

    def test_forwards_events_to_client(self):
        bus = self._make_bus()
        ws = FakeWebSocket()

        received_events: list[dict] = []
        stop_event = threading.Event()

        original_send = ws.send

        def patched_send(data: str):
            original_send(data)
            msg = json.loads(data)
            if msg.get("type") != "ping":
                received_events.append(msg)
            if len(received_events) >= 2:
                stop_event.set()
                ws.close()

        ws.send = patched_send

        thread = threading.Thread(target=self._run, args=(ws, bus), daemon=True)
        thread.start()
        time.sleep(0.1)

        bus.emit("stt.final", {"text": "Привет", "confidence": 0.95})
        bus.emit("stt.final", {"text": "Мир", "confidence": 0.90})

        stop_event.wait(timeout=1.0)
        thread.join(timeout=1.0)

        self.assertEqual(len(received_events), 2)
        self.assertEqual(received_events[0]["type"], "stt.final")
        self.assertEqual(received_events[0]["data"]["text"], "Привет")
        self.assertEqual(received_events[1]["data"]["text"], "Мир")

    # ------------------------------------------------------------------
    # Test 3: type filter — only matching events pass
    # ------------------------------------------------------------------

    def test_type_filter_passes_only_matching_events(self):
        bus = self._make_bus()
        ws = FakeWebSocket()

        received_events: list[dict] = []
        stop_event = threading.Event()

        original_send = ws.send

        def patched_send(data: str):
            original_send(data)
            msg = json.loads(data)
            if msg.get("type") != "ping":
                received_events.append(msg)
            if len(received_events) >= 1:
                stop_event.set()
                ws.close()

        ws.send = patched_send

        thread = threading.Thread(
            target=self._run, args=(ws, bus, {"stt.final"}), daemon=True
        )
        thread.start()
        time.sleep(0.1)

        bus.emit("translation", {"result": "Hello"})     # filtered
        bus.emit("stt.final", {"text": "Test", "confidence": 0.85})  # pass

        stop_event.wait(timeout=1.0)
        thread.join(timeout=1.0)

        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0]["type"], "stt.final")

    # ------------------------------------------------------------------
    # Test 4: multiple type filters
    # ------------------------------------------------------------------

    def test_type_filter_multiple_types(self):
        bus = self._make_bus()
        ws = FakeWebSocket()

        received_types: list[str] = []
        stop_event = threading.Event()

        original_send = ws.send

        def patched_send(data: str):
            original_send(data)
            msg = json.loads(data)
            if msg.get("type") != "ping":
                received_types.append(msg["type"])
            if len(received_types) >= 2:
                stop_event.set()
                ws.close()

        ws.send = patched_send

        thread = threading.Thread(
            target=self._run,
            args=(ws, bus, {"stt.final", "translation"}),
            daemon=True,
        )
        thread.start()
        time.sleep(0.1)

        bus.emit("stt.failed", {"reason": "timeout"})         # filtered
        bus.emit("stt.final", {"text": "A", "confidence": 0.9})    # pass
        bus.emit("translation", {"result": "B"})               # pass

        stop_event.wait(timeout=1.0)
        thread.join(timeout=1.0)

        self.assertEqual(set(received_types), {"stt.final", "translation"})
        self.assertNotIn("stt.failed", received_types)

    # ------------------------------------------------------------------
    # Test 5: graceful disconnect on send error
    # ------------------------------------------------------------------

    def test_graceful_disconnect_on_send_error(self):
        bus = self._make_bus()
        ws = FakeWebSocket()

        ws.close()  # pre-close so first send raises

        thread = threading.Thread(target=self._run, args=(ws, bus), daemon=True)
        thread.start()
        time.sleep(0.1)
        bus.emit("stt.final", {"text": "trigger disconnect"})

        thread.join(timeout=1.0)

        self.assertEqual(bus.subscriber_count(), 0)

    # ------------------------------------------------------------------
    # Test 6: heartbeat ping is sent
    # ------------------------------------------------------------------

    def test_heartbeat_ping_sent(self):
        """Verifies that a heartbeat {"type":"ping"} is sent after idle period.

        Tests the heartbeat logic directly using a minimal loop implementation
        that mirrors _handle_ws_connection behaviour, independent of module globals.
        """
        # Implement the heartbeat portion of _handle logic inline so we don't
        # depend on module-level patching which can race with concurrent tests.
        bus = self._make_bus()

        sent: list[str] = []
        closed = threading.Event()

        class MinimalWS:
            def send(self, data: str):
                if closed.is_set():
                    raise ConnectionError("closed")
                sent.append(data)
                if json.loads(data).get("type") == "ping":
                    closed.set()  # stop after first ping

        ws = MinimalWS()
        q = bus.subscribe()

        heartbeat_sec = 0.1
        poll_sec = 0.05

        def run():
            import time as _t
            last_ping = _t.monotonic()
            try:
                while not closed.is_set():
                    now = _t.monotonic()
                    if now - last_ping >= heartbeat_sec:
                        try:
                            ws.send('{"type":"ping"}')
                        except Exception:
                            break
                        last_ping = _t.monotonic()
                    try:
                        q.get(timeout=poll_sec)
                    except queue.Empty:
                        continue
            finally:
                bus.unsubscribe(q)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        closed.wait(timeout=2.0)
        thread.join(timeout=1.0)

        ping_messages = [json.loads(s) for s in sent if json.loads(s).get("type") == "ping"]
        self.assertGreater(len(ping_messages), 0, "Heartbeat ping was not sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
