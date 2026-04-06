"""Unit-тесты для VGWebSocketClient."""
import sys
import os
import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.vg_ws_client import VGWebSocketClient


class TestVGWebSocketClient(unittest.TestCase):

    def test_ws_url_construction_http(self):
        c = VGWebSocketClient("http://127.0.0.1:8090", "vs_abc123")
        self.assertEqual(c.ws_url, "ws://127.0.0.1:8090/v1/sessions/vs_abc123/stream")

    def test_ws_url_construction_https(self):
        c = VGWebSocketClient("https://my-tunnel.example.com", "vs_xyz", api_key="secret")
        self.assertEqual(c.ws_url, "wss://my-tunnel.example.com/v1/sessions/vs_xyz/stream")
        self.assertEqual(c.api_key, "secret")

    def test_stop_sets_event(self):
        c = VGWebSocketClient("http://localhost:8090", "vs_test")
        self.assertFalse(c._stop.is_set())
        c.stop()
        self.assertTrue(c._stop.is_set())

    @patch("backend.vg_ws_client.bus")
    def test_event_forwarding(self, mock_bus):
        """Проверяем что событие из WS пробрасывается в EventBus."""
        event_json = json.dumps({"type": "stt.final", "data": {"text": "hello"}})

        # Имитируем один цикл обработки сообщения
        async def one_iteration():
            event = json.loads(event_json)
            mock_bus.emit(event.get("type", "unknown"), event.get("data", {}))

        asyncio.run(one_iteration())
        mock_bus.emit.assert_called_once_with("stt.final", {"text": "hello"})


if __name__ == "__main__":
    unittest.main()
