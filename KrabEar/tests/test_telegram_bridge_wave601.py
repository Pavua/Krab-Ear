"""Wave 601 — TelegramBridge unit tests.

Покрывает: happy path, connection refused retry, graceful skip,
malformed response, timeout.
"""

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

from backend.telegram_bridge import TelegramBridge, CircuitBreakerOpen


class TestTelegramBridgeHappyPath(unittest.TestCase):
    """POST /api/notify → 200, результат нормализован."""

    def test_send_message_returns_normalised_dict(self):
        bridge = TelegramBridge(base_url="http://localhost:8080")
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "message_id": 42,
            "sent_at": 1_700_000_000.0,
            "chat_title": "Test Chat",
        }
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = bridge.send_message(text="Hello", chat_id=123)

        mock_post.assert_called_once()
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["chat_title"], "Test Chat")
        self.assertAlmostEqual(result["sent_at"], 1_700_000_000.0, places=0)


class TestTelegramBridgeRetryOnConnectionRefused(unittest.TestCase):
    """Connection refused → circuit breaker открывается после порога."""

    def test_circuit_opens_after_threshold_failures(self):
        # threshold=3, reset_sec большой чтобы не авто-закрылся
        bridge = TelegramBridge(
            base_url="http://localhost:8080",
            circuit_fail_threshold=3,
            circuit_reset_sec=3600.0,
        )
        exc = requests.ConnectionError("Connection refused")

        with patch("requests.post", side_effect=exc):
            for attempt in range(1, 4):
                with self.assertRaises(requests.ConnectionError):
                    bridge.send_message(text=f"attempt {attempt}", chat_id=1)

        # после 3 ошибок circuit breaker должен быть открыт
        self.assertTrue(bridge.is_circuit_open)

        # следующий вызов → CircuitBreakerOpen без HTTP запроса
        with patch("requests.post") as mock_post:
            with self.assertRaises(CircuitBreakerOpen):
                bridge.send_message(text="should not go through", chat_id=1)
            mock_post.assert_not_called()


class TestTelegramBridgeKrabNotRunning(unittest.TestCase):
    """Krab не запущен → ConnectionError пойман и логируется, не падает."""

    def test_graceful_skip_when_krab_not_running(self):
        bridge = TelegramBridge(
            base_url="http://localhost:8080",
            circuit_fail_threshold=99,  # CB не откроется
        )
        exc = requests.ConnectionError("Connection refused")

        sent_messages = []

        def safe_send(text: str, chat_id: int) -> None:
            """Имитирует caller-side graceful handling."""
            try:
                result = bridge.send_message(text=text, chat_id=chat_id)
                sent_messages.append(result)
            except (requests.ConnectionError, requests.Timeout):
                # graceful skip — caller не крашится
                pass

        with patch("requests.post", side_effect=exc):
            safe_send("hello", chat_id=1)

        # сообщение не было отправлено, но исключение не всплыло выше
        self.assertEqual(sent_messages, [])


class TestTelegramBridgeMalformedResponse(unittest.TestCase):
    """Ответ без ожидаемых полей → fallback на разумные умолчания."""

    def test_missing_fields_use_fallback_values(self):
        bridge = TelegramBridge(base_url="http://localhost:8080")
        mock_resp = MagicMock()
        mock_resp.ok = True
        # ответ не содержит message_id / sent_at / chat_title
        mock_resp.json.return_value = {}

        with patch("requests.post", return_value=mock_resp):
            result = bridge.send_message(text="test", chat_id=777)

        self.assertIsNone(result["message_id"])
        # sent_at должен быть заполнен текущим временем (fallback)
        self.assertAlmostEqual(result["sent_at"], time.time(), delta=5.0)
        # chat_title → str(chat_id)
        self.assertEqual(result["chat_title"], "777")


class TestTelegramBridgeTimeout(unittest.TestCase):
    """Таймаут → requests.Timeout, circuit breaker фиксирует ошибку."""

    def test_timeout_increments_failure_count(self):
        bridge = TelegramBridge(
            base_url="http://localhost:8080",
            timeout_sec=1.0,
            circuit_fail_threshold=5,
        )
        with patch("requests.post", side_effect=requests.Timeout("timed out")):
            with self.assertRaises(requests.Timeout):
                bridge.send_message(text="urgent", chat_id=999)

        # fail_count должен вырасти
        with bridge._lock:
            self.assertEqual(bridge._fail_count, 1)

        # circuit breaker ещё закрыт (порог 5)
        self.assertFalse(bridge.is_circuit_open)


if __name__ == "__main__":
    unittest.main()
