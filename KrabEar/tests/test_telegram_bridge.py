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


class TestTelegramBridgeSendNotifyBasic(unittest.TestCase):
    """test_send_notify_basic — успешный POST /api/notify mock → 200."""

    @patch("backend.telegram_bridge.requests.post")
    def test_send_notify_basic(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response(message_id=1, chat_title="Basic")
        bridge = TelegramBridge()
        result = bridge.send_message(text="hello", chat_id=100)
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        self.assertIn("/api/notify", call_url)
        self.assertEqual(result["message_id"], 1)


class TestTelegramBridgeSendHandles404(unittest.TestCase):
    """test_send_handles_404 — web-panel отвечает 404 → RuntimeError krab_error."""

    @patch("backend.telegram_bridge.requests.post")
    def test_send_handles_404(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_error_response(404, "Not Found")
        bridge = TelegramBridge()
        with self.assertRaises(RuntimeError) as ctx:
            bridge.send_message(text="hi", chat_id=1)
        self.assertIn("krab_error", str(ctx.exception))


class TestTelegramBridgeSendConnectionRefused(unittest.TestCase):
    """test_send_handles_connection_refused — ConnectionError пробрасывается выше."""

    @patch("backend.telegram_bridge.requests.post",
           side_effect=requests.ConnectionError("Connection refused"))
    def test_send_handles_connection_refused(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge()
        with self.assertRaises(requests.ConnectionError):
            bridge.send_message(text="ping", chat_id=1)


class TestTelegramBridgeSendTimeoutHandled(unittest.TestCase):
    """test_send_timeout_handled — Timeout пробрасывается, circuit breaker считает ошибку."""

    @patch("backend.telegram_bridge.requests.post",
           side_effect=requests.Timeout("timed out"))
    def test_send_timeout_handled(self, _mock: MagicMock) -> None:
        bridge = TelegramBridge(circuit_fail_threshold=5)
        with self.assertRaises(requests.Timeout):
            bridge.send_message(text="ping", chat_id=2)
        self.assertEqual(bridge._fail_count, 1)


class TestTelegramBridgeUnicodeMessageBody(unittest.TestCase):
    """test_unicode_message_body — кириллица, эмодзи и CJK проходят без искажений."""

    @patch("backend.telegram_bridge.requests.post")
    def test_unicode_message_body(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        unicode_text = "Привет! 你好 🦀🎤"
        bridge.send_message(text=unicode_text, chat_id=42)
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["text"], unicode_text)


class TestTelegramBridgeConcurrentSend(unittest.TestCase):
    """test_concurrent_send — несколько потоков шлют сообщения одновременно; все succeeds."""

    @patch("backend.telegram_bridge.requests.post")
    def test_concurrent_send(self, mock_post: MagicMock) -> None:
        import threading

        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        errors: list[Exception] = []

        def _send(idx: int) -> None:
            try:
                bridge.send_message(text=f"msg {idx}", chat_id=idx)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_send, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Errors in threads: {errors}")
        self.assertEqual(mock_post.call_count, 10)


class TestTelegramBridgeDisabledViaSetting(unittest.TestCase):
    """test_disabled_via_setting — non-localhost base_url raises ValueError (W945 allowlist).

    Архитектурное примечание: TelegramBridge не имеет явного 'enabled' флага.
    Любой non-localhost base_url теперь отклоняется при конструировании (SSRF guard).
    """

    def test_disabled_via_setting(self) -> None:
        """Non-localhost base_url → ValueError при конструировании (W945 hostname allowlist)."""
        with self.assertRaises(ValueError, msg="non-localhost host should be rejected"):
            TelegramBridge(
                base_url="http://disabled-host:0",
                circuit_fail_threshold=1,
            )


class TestTelegramBridgeIncludesPriorityField(unittest.TestCase):
    """test_includes_priority_field — reply_to передаётся как reply_to_message_id в payload."""

    @patch("backend.telegram_bridge.requests.post")
    def test_includes_priority_field(self, mock_post: MagicMock) -> None:
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        bridge.send_message(text="важное", chat_id=999, reply_to=42)
        payload = mock_post.call_args[1]["json"]
        self.assertIn("reply_to_message_id", payload)
        self.assertEqual(payload["reply_to_message_id"], 42)


class TestTelegramBridgeHandlesInvalidUrlSetting(unittest.TestCase):
    """test_handles_invalid_url_setting — non-localhost URL → ValueError at construction."""

    def test_handles_invalid_url_setting(self) -> None:
        # Since W945 hostname allowlist, non-localhost hosts raise ValueError at __init__
        with self.assertRaises(ValueError):
            TelegramBridge(base_url="http://INVALID_HOST_@@@:99999")


class TestTelegramBridgeDoesNotRetry4xx(unittest.TestCase):
    """test_does_not_retry_4xx — 4xx не вызывает повторных запросов (только 5xx учитываем)."""

    @patch("backend.telegram_bridge.requests.post")
    def test_does_not_retry_4xx(self, mock_post: MagicMock) -> None:
        """Bridge не выполняет retry при любом статус-коде — один запрос и RuntimeError."""
        mock_post.return_value = _make_error_response(400, "bad request")
        bridge = TelegramBridge(circuit_fail_threshold=10)
        with self.assertRaises(RuntimeError):
            bridge.send_message(text="test", chat_id=1)
        # ровно один вызов — нет retry логики
        self.assertEqual(mock_post.call_count, 1)

    @patch("backend.telegram_bridge.requests.post")
    def test_5xx_also_no_retry_but_increments_circuit(self, mock_post: MagicMock) -> None:
        """5xx тоже не retry, но инкрементирует circuit breaker."""
        mock_post.return_value = _make_error_response(503, "unavailable")
        bridge = TelegramBridge(circuit_fail_threshold=10)
        with self.assertRaises(RuntimeError):
            bridge.send_message(text="test", chat_id=1)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(bridge._fail_count, 1)


class TestTelegramBridgeGetChats(unittest.TestCase):
    """get_chats возвращает список чатов от /api/chats."""

    @patch("backend.telegram_bridge.requests.get")
    def test_get_chats_success(self, mock_get: MagicMock) -> None:
        resp = MagicMock()
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {
            "chats": [
                {"id": 1, "title": "Чат 1", "type": "group"},
                {"id": 2, "title": None, "type": "private"},
            ]
        }
        mock_get.return_value = resp
        bridge = TelegramBridge()
        chats = bridge.get_chats()
        self.assertEqual(len(chats), 2)
        self.assertEqual(chats[0]["title"], "Чат 1")
        self.assertEqual(chats[1]["title"], "2")  # fallback to str(id)

    @patch("backend.telegram_bridge.requests.get")
    def test_get_chats_503_raises_runtime_error(self, mock_get: MagicMock) -> None:
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 503
        resp.text = "unavailable"
        resp.json.return_value = {"detail": "unavailable"}
        mock_get.return_value = resp
        bridge = TelegramBridge()
        with self.assertRaises(RuntimeError) as ctx:
            bridge.get_chats()
        self.assertIn("krab_unavailable", str(ctx.exception))


class TestTelegramBridgeHostnameAllowlist(unittest.TestCase):
    """W945 / W943 HIGH-1: hostname allowlist rejects non-localhost base_url at construction.

    Ensures that KRAB_EAR_TELEGRAM_BRIDGE_URL cannot be used to redirect bridge
    traffic to SSRF targets such as 0.0.0.0, link-local (169.254.x.x), or
    arbitrary LAN IPs.
    """

    def test_init_rejects_non_localhost_zero_addr(self) -> None:
        """0.0.0.0 должен быть отклонён с ValueError."""
        with self.assertRaises(ValueError) as ctx:
            TelegramBridge(base_url="http://0.0.0.0:5000")
        self.assertIn("refusing non-localhost", str(ctx.exception))

    def test_init_rejects_link_local(self) -> None:
        """169.254.169.254 (AWS IMDS / link-local) должен быть отклонён с ValueError."""
        with self.assertRaises(ValueError) as ctx:
            TelegramBridge(base_url="http://169.254.169.254")
        self.assertIn("refusing non-localhost", str(ctx.exception))

    def test_init_rejects_private_lan(self) -> None:
        """Приватный IP (10.0.0.1) должен быть отклонён с ValueError."""
        with self.assertRaises(ValueError) as ctx:
            TelegramBridge(base_url="http://10.0.0.1:8080")
        self.assertIn("refusing non-localhost", str(ctx.exception))

    def test_init_rejects_external_host(self) -> None:
        """Внешний хост должен быть отклонён с ValueError."""
        with self.assertRaises(ValueError) as ctx:
            TelegramBridge(base_url="http://evil.example.com:8080")
        self.assertIn("refusing non-localhost", str(ctx.exception))

    @patch("requests.post")
    def test_init_allows_localhost(self, mock_post: MagicMock) -> None:
        """http://localhost:5000 должен быть разрешён."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge(base_url="http://localhost:5000")
        self.assertEqual(bridge._base_url, "http://localhost:5000")

    @patch("requests.post")
    def test_init_allows_127_0_0_1(self, mock_post: MagicMock) -> None:
        """http://127.0.0.1:8080 должен быть разрешён."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge(base_url="http://127.0.0.1:8080")
        self.assertEqual(bridge._base_url, "http://127.0.0.1:8080")

    @patch("requests.post")
    def test_init_allows_default_url(self, mock_post: MagicMock) -> None:
        """Дефолтный URL (http://localhost:8080) должен быть разрешён."""
        mock_post.return_value = _make_ok_response()
        bridge = TelegramBridge()
        self.assertIn("localhost", bridge._base_url)


if __name__ == "__main__":
    unittest.main()
