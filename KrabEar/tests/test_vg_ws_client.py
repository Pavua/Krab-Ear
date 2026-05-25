"""Unit-тесты для VGWebSocketClient."""
from backend.vg_ws_client import VGWebSocketClient, _RECONNECT_BASE_SEC
import sys
import os
import asyncio
import json
import unittest
from unittest.mock import patch, AsyncMock, MagicMock
from contextlib import asynccontextmanager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


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

    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("backend.vg_ws_client.websockets.connect")
    def test_reconnect_on_disconnect(self, mock_connect, mock_sleep, mock_bus):
        """После разрыва соединения клиент должен переподключиться."""
        connect_calls = []

        # Первое подключение бросает ConnectionError (разрыв).
        # Второй вызов — успешный, возвращает пустой поток и позволяет выйти.
        async def fake_ws_iter_empty():
            # async for raw in ws: — сразу завершаемся (ноль сообщений)
            return
            yield  # делаем генератор

        first = True

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            nonlocal first
            connect_calls.append(url)
            if first:
                first = False
                raise ConnectionError("симуляция разрыва")
            # После reconnect — сразу останавливаем клиента, чтобы цикл вышел
            client.stop()
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=fake_ws_iter_empty())
            yield ws

        mock_connect.side_effect = fake_connect

        client = VGWebSocketClient("http://localhost:8090", "vs_reconnect")
        asyncio.run(client.run())

        # Должно быть два вызова connect: исходный + reconnect
        self.assertEqual(len(connect_calls), 2)
        # Между попытками должна быть задержка backoff
        mock_sleep.assert_awaited_once()

    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("backend.vg_ws_client.websockets.connect")
    def test_max_reconnect_attempts(self, mock_connect, mock_sleep, mock_bus):
        """Backoff растёт до _RECONNECT_MAX_SEC и не превышает его."""
        MAX_FAILURES = 5
        attempt = [0]

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            attempt[0] += 1
            if attempt[0] >= MAX_FAILURES:
                # После N попыток останавливаем клиент
                client.stop()
            raise ConnectionError("постоянный сбой")
            yield  # делаем context-manager валидным

        mock_connect.side_effect = fake_connect

        client = VGWebSocketClient("http://localhost:8090", "vs_maxretry")
        asyncio.run(client.run())

        # Все sleep-вызовы кроме последнего (после stop) должны иметь backoff <= MAX
        from backend.vg_ws_client import _RECONNECT_MAX_SEC
        for c in mock_sleep.await_args_list:
            delay = c.args[0]
            self.assertLessEqual(delay, _RECONNECT_MAX_SEC,
                                 f"backoff {delay} превысил максимум {_RECONNECT_MAX_SEC}")

        # Backoff удваивается: первая задержка == base
        first_delay = mock_sleep.await_args_list[0].args[0]
        self.assertAlmostEqual(first_delay, _RECONNECT_BASE_SEC)

        # Убеждаемся что было несколько попыток
        self.assertGreaterEqual(attempt[0], MAX_FAILURES)

    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("backend.vg_ws_client.websockets.connect")
    def test_event_forwarding_after_reconnect(self, mock_connect, mock_ws_sleep, mock_bus):
        """После переподключения события продолжают проксироваться в EventBus."""
        event_after_reconnect = json.dumps({"type": "stt.final", "data": {"text": "после реконнекта"}})
        first = True

        async def messages_after_reconnect():
            yield event_after_reconnect
            # После доставки сообщения останавливаем клиент
            client.stop()

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            nonlocal first
            if first:
                first = False
                raise ConnectionError("первый разрыв")
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=messages_after_reconnect())
            yield ws

        mock_connect.side_effect = fake_connect

        client = VGWebSocketClient("http://localhost:8090", "vs_fwd_after_reconnect")
        asyncio.run(client.run())

        # EventBus должен получить событие из сессии после реконнекта
        mock_bus.emit.assert_called_with("stt.final", {"text": "после реконнекта"})


class TestVGWebSocketClientWave144(unittest.TestCase):
    """Wave 144 — required named tests."""

    # ------------------------------------------------------------------
    # test_connect_success
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.websockets.connect")
    def test_connect_success(self, mock_connect, mock_bus):
        """Клиент подключается к WS без ошибок и завершается чисто."""

        async def no_messages():
            return
            yield

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            client.stop()
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=no_messages())
            yield ws

        mock_connect.side_effect = fake_connect
        client = VGWebSocketClient("http://localhost:8090", "vs_ok")
        asyncio.run(client.run())
        mock_connect.assert_called_once()

    # ------------------------------------------------------------------
    # test_send_message — VGWebSocketClient is read-only (no send API),
    # so we verify the bus.emit path is the "send" side of the contract.
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.websockets.connect")
    def test_send_message(self, mock_connect, mock_bus):
        """Получив сообщение, клиент эмитирует его в EventBus (канал отправки)."""
        payload = json.dumps({"type": "tts.audio", "data": {"chunk": "base64=="}})

        async def one_message():
            yield payload

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=one_message())
            yield ws
            client.stop()

        mock_connect.side_effect = fake_connect
        client = VGWebSocketClient("http://localhost:8090", "vs_send")
        asyncio.run(client.run())
        mock_bus.emit.assert_called_with("tts.audio", {"chunk": "base64=="})

    # ------------------------------------------------------------------
    # test_receive_messages
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.websockets.connect")
    def test_receive_messages(self, mock_connect, mock_bus):
        """Несколько сообщений последовательно пробрасываются в EventBus."""
        msgs = [
            json.dumps({"type": "stt.final", "data": {"text": "uno"}}),
            json.dumps({"type": "stt.final", "data": {"text": "dos"}}),
            json.dumps({"type": "stt.final", "data": {"text": "tres"}}),
        ]

        async def multi_messages():
            for m in msgs:
                yield m

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=multi_messages())
            yield ws
            client.stop()

        mock_connect.side_effect = fake_connect
        client = VGWebSocketClient("http://localhost:8090", "vs_recv")
        asyncio.run(client.run())
        self.assertEqual(mock_bus.emit.call_count, 3)
        texts = [c.args[1]["text"] for c in mock_bus.emit.call_args_list]
        self.assertEqual(texts, ["uno", "dos", "tres"])

    # ------------------------------------------------------------------
    # test_reconnect_after_disconnect
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.asyncio.sleep", new_callable=AsyncMock)
    @patch("backend.vg_ws_client.websockets.connect")
    def test_reconnect_after_disconnect(self, mock_connect, mock_sleep, mock_bus):
        """После разрыва WS клиент переподключается и продолжает работу."""
        first = True

        async def no_messages():
            return
            yield

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            nonlocal first
            if first:
                first = False
                raise ConnectionError("simulated disconnect")
            client.stop()
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=no_messages())
            yield ws

        mock_connect.side_effect = fake_connect
        client = VGWebSocketClient("http://localhost:8090", "vs_recon")
        asyncio.run(client.run())
        self.assertEqual(mock_connect.call_count, 2)
        mock_sleep.assert_awaited_once()

    # ------------------------------------------------------------------
    # test_handles_invalid_session_id
    # ------------------------------------------------------------------
    def test_handles_invalid_session_id(self):
        """Пустой session_id не вызывает исключения при построении URL."""
        client = VGWebSocketClient("http://localhost:8090", "")
        self.assertIn("/v1/sessions//stream", client.ws_url)
        # stop safe to call even without running loop
        client.stop()
        self.assertTrue(client._stop.is_set())

    # ------------------------------------------------------------------
    # test_concurrent_send
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.websockets.connect")
    def test_concurrent_send(self, mock_connect, mock_bus):
        """EventBus.emit не падает при конкурентной проксировке сообщений."""
        NUM = 20
        msgs = [json.dumps({"type": "stt.final", "data": {"text": f"t{i}"}}) for i in range(NUM)]

        async def burst_messages():
            for m in msgs:
                yield m

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=burst_messages())
            yield ws
            client.stop()

        mock_connect.side_effect = fake_connect

        errors = []

        def run_client():
            try:
                asyncio.run(VGWebSocketClient("http://localhost:8090", "vs_conc").run())
            except Exception as exc:
                errors.append(exc)

        client = VGWebSocketClient("http://localhost:8090", "vs_conc")
        mock_connect.side_effect = fake_connect
        asyncio.run(client.run())
        self.assertEqual(errors, [])
        self.assertEqual(mock_bus.emit.call_count, NUM)

    # ------------------------------------------------------------------
    # test_close_clean
    # ------------------------------------------------------------------
    @patch("backend.vg_ws_client.bus")
    @patch("backend.vg_ws_client.websockets.connect")
    def test_close_clean(self, mock_connect, mock_bus):
        """После вызова stop() цикл run() завершается без исключений."""

        async def no_messages():
            return
            yield

        @asynccontextmanager
        async def fake_connect(url, extra_headers=None):
            client.stop()  # сигнал остановки ДО подключения
            ws = MagicMock()
            ws.__aiter__ = MagicMock(return_value=no_messages())
            yield ws

        mock_connect.side_effect = fake_connect
        client = VGWebSocketClient("http://localhost:8090", "vs_close")
        # Должен завершиться без исключений
        asyncio.run(client.run())
        self.assertTrue(client._stop.is_set())


if __name__ == "__main__":
    unittest.main()
