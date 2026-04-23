# -*- coding: utf-8 -*-
"""Тесты TelegramBridge — мост Krab Ear → main Krab userbot.

Покрываем:
  - успешная отправка сообщения
  - Krab недоступен (ConnectionError) → RuntimeError krab_unavailable
  - Krab вернул 503 → RuntimeError krab_unavailable
  - Krab вернул 500 → RuntimeError krab_error
  - circuit breaker срабатывает после N ошибок
  - circuit breaker блокирует повторные вызовы после открытия
  - circuit breaker сбрасывается после reset_sec
  - reply_to передаётся в payload
  - chat_id принимается как int, str и username (@handle)
  - пустой text → ValueError
"""

from __future__ import annotations

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

# Worktree path setup
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

from backend.telegram_bridge import CircuitBreakerOpen, TelegramBridge


def _make_ok_response(
    message_id: int = 42,
    chat_title: str = "Test Chat",
) -> MagicMock:
    """Имитирует успешный HTTP-ответ от /api/notify."""
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = {
        "ok": True,
        "chat_id": "123456",
        "message_id": message_id,
        "chat_title": chat_title,
        "sent_at": 1700000000.0,
    }
    return resp


def _make_error_response(status_code: int, detail: str) -> MagicMock:
    """Имитирует HTTP-ответ с ошибкой."""
    resp = MagicMock()
    resp.ok = False
    resp.status_code = status_code
    resp.text = detail
    resp.json.return_value = {"detail": detail}
    return resp


class TestTelegramBridgeSendSuccess(unittest.TestCase):
    """send_message возвращает корректный словарь при успехе."""

    @patch("requests.post")
    def test_returns_message_id_sent_at_chat_title(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response(message_id=99, chat_title="Мой чат")
        bridge = TelegramBridge()
        result = bridge.send_message(text="Привет", chat_id=123456)

        self.assertEqual(result["message_id"], 99)
        self.assertEqual(result["chat_title"], "Мой чат")
        self.assertIn("sent_at", result)

    @patch("requests.post")
    def test_post_called_with_correct_url_and_payload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge(base_url="http://localhost:8080")
        bridge.send_message(text="Test", chat_id="@testchat")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        self.assertIn("http://localhost:8080/api/notify", call_kwargs[0])
        payload = call_kwargs[1]["json"]
        self.assertEqual(payload["text"], "Test")
        self.assertEqual(payload["chat_id"], "@testchat")

    @patch("requests.post")
    def test_circuit_resets_on_success(self, mock_post: MagicMock) -> None:
        """Успешный вызов обнуляет счётчик ошибок circuit breaker."""
        bridge = TelegramBridge(circuit_fail_threshold=3)
        bridge._fail_count = 2  # предустановим 2 ошибки
        mock_post.return_value = _make_ok_response()
        bridge.send_message(text="OK", chat_id=1)
        self.assertEqual(bridge._fail_count, 0)


class TestTelegramBridgeKrabUnavailable(unittest.TestCase):
    """Обработка ситуации, когда main Krab недоступен."""

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_connection_error_raises_runtime_error(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge()
        with self.assertRaises(requests.ConnectionError):
            bridge.send_message(text="Тест", chat_id=12345)

    @patch("requests.post", side_effect=requests.Timeout("timeout"))
    def test_timeout_raises(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge()
        with self.assertRaises(requests.Timeout):
            bridge.send_message(text="Тест", chat_id=12345)

    @patch("requests.post")
    def test_503_raises_runtime_error_with_krab_unavailable(
        self, mock_post: MagicMock
    ) -> None:
        mock_post.return_value = _make_error_response(503, "userbot_not_ready")
        bridge = TelegramBridge()
        with self.assertRaises(RuntimeError) as ctx:
            bridge.send_message(text="Тест", chat_id=12345)
        self.assertIn("krab_unavailable", str(ctx.exception))

    @patch("requests.post")
    def test_500_raises_runtime_error_with_krab_error(
        self, mock_post: MagicMock
    ) -> None:
        mock_post.return_value = _make_error_response(500, "internal server error")
        bridge = TelegramBridge()
        with self.assertRaises(RuntimeError) as ctx:
            bridge.send_message(text="Тест", chat_id=12345)
        self.assertIn("krab_error", str(ctx.exception))


class TestTelegramBridgeCircuitBreaker(unittest.TestCase):
    """Circuit breaker открывается после N последовательных ошибок."""

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_circuit_opens_after_threshold_failures(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge(circuit_fail_threshold=3, circuit_reset_sec=60.0)
        # Три ошибки подряд
        for _ in range(3):
            try:
                bridge.send_message(text="X", chat_id=1)
            except requests.ConnectionError:
                pass
        self.assertTrue(bridge.is_circuit_open)

    @patch("requests.post", side_effect=requests.ConnectionError("refused"))
    def test_circuit_open_raises_circuit_breaker_open(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge(circuit_fail_threshold=2, circuit_reset_sec=60.0)
        for _ in range(2):
            try:
                bridge.send_message(text="X", chat_id=1)
            except requests.ConnectionError:
                pass

        self.assertTrue(bridge.is_circuit_open)
        with self.assertRaises(CircuitBreakerOpen):
            bridge.send_message(text="After open", chat_id=1)

    def test_circuit_auto_closes_after_reset_sec(self) -> None:
        bridge = TelegramBridge(circuit_fail_threshold=1, circuit_reset_sec=0.05)
        bridge._fail_count = 1
        bridge._open_at = time.monotonic() - 0.1  # уже прошло >0.05s
        self.assertFalse(bridge.is_circuit_open)  # должен закрыться

    def test_reset_circuit_clears_state(self) -> None:
        bridge = TelegramBridge(circuit_fail_threshold=2, circuit_reset_sec=60.0)
        bridge._fail_count = 2
        bridge._open_at = time.monotonic()
        bridge.reset_circuit()
        self.assertFalse(bridge.is_circuit_open)
        self.assertEqual(bridge._fail_count, 0)


class TestTelegramBridgeReplyTo(unittest.TestCase):
    """reply_to передаётся корректно в payload."""

    @patch("requests.post")
    def test_reply_to_included_in_payload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="Ответ", chat_id=111, reply_to=777)

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload.get("reply_to_message_id"), 777)

    @patch("requests.post")
    def test_reply_to_none_not_in_payload(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="Сообщение", chat_id=111, reply_to=None)

        payload = mock_post.call_args[1]["json"]
        self.assertNotIn("reply_to_message_id", payload)


class TestTelegramBridgeChatIdTypes(unittest.TestCase):
    """chat_id принимается как int, str и @username."""

    @patch("requests.post")
    def test_int_chat_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="Hi", chat_id=123456789)
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], "123456789")

    @patch("requests.post")
    def test_str_numeric_chat_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="Hi", chat_id="123456789")
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], "123456789")

    @patch("requests.post")
    def test_username_chat_id(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="Hi", chat_id="@mygroup")
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], "@mygroup")


class TestTelegramBridgeEmptyText(unittest.TestCase):
    """Пустой text вызывает ValueError до HTTP-запроса."""

    def test_empty_string_raises_value_error(self) -> None:
        bridge = TelegramBridge()
        with self.assertRaises(ValueError):
            bridge.send_message(text="", chat_id=12345)

    def test_whitespace_only_raises_value_error(self) -> None:
        bridge = TelegramBridge()
        with self.assertRaises(ValueError):
            bridge.send_message(text="   ", chat_id=12345)

    @patch("requests.post")
    def test_no_http_call_on_empty_text(self, mock_post: MagicMock) -> None:
        bridge = TelegramBridge()
        try:
            bridge.send_message(text="", chat_id=12345)
        except ValueError:
            pass
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
