# -*- coding: utf-8 -*-
"""Тесты IPC-методов send_to_telegram и list_telegram_chats.

Покрываем:
  - test_send_to_telegram_calls_bridge
  - test_send_to_telegram_when_bridge_offline_returns_error
  - test_list_chats_returns_chats
  - test_list_chats_when_offline_returns_error
  - test_invalid_chat_id_returns_error
  - test_empty_body_returns_error
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import requests

from backend.telegram_bridge import CircuitBreakerOpen, TelegramBridge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bridge_stub(
    send_result: dict | None = None,
    send_side_effect: Exception | None = None,
    chats_result: list | None = None,
    chats_side_effect: Exception | None = None,
) -> MagicMock:
    """Возвращает MagicMock с настроенными send_message и get_chats."""
    bridge = MagicMock(spec=TelegramBridge)
    if send_side_effect is not None:
        bridge.send_message.side_effect = send_side_effect
    else:
        bridge.send_message.return_value = send_result or {
            "message_id": 1,
            "sent_at": 1700000000.0,
            "chat_title": "Test",
        }

    if chats_side_effect is not None:
        bridge.get_chats.side_effect = chats_side_effect
    else:
        bridge.get_chats.return_value = chats_result or [
            {"id": 123, "title": "Saved Messages", "type": "saved"},
            {"id": -1001234567890, "title": "My Group", "type": "group"},
        ]

    return bridge


def _make_service_with_bridge(bridge: MagicMock) -> object:
    """Создаёт минимальный объект-заглушку BackendService для тестирования обработчиков."""
    from core.config import settings as real_settings

    class FakeService:
        _telegram_bridge = bridge
        _settings = real_settings

        def _handle_send_to_telegram(self, params):
            from backend.service import BackendService
            return BackendService._handle_send_to_telegram(self, params)

        def _handle_list_telegram_chats(self, params):
            from backend.service import BackendService
            return BackendService._handle_list_telegram_chats(self, params)

    svc = FakeService()
    # Inject a local settings object we can tweak
    import types
    svc_settings = types.SimpleNamespace(TELEGRAM_BRIDGE_ENABLED=True)
    return svc, svc_settings


# ---------------------------------------------------------------------------
# Tests: send_to_telegram
# ---------------------------------------------------------------------------

class TestSendToTelegramCallsBridge(unittest.TestCase):
    """send_to_telegram вызывает bridge.send_message с правильными параметрами."""

    def test_send_to_telegram_calls_bridge(self):
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            BackendService._handle_send_to_telegram(svc, {
                "chat_id": "123456",
                "text": "Тест транскрипции",
            })

        bridge.send_message.assert_called_once()
        call_kwargs = bridge.send_message.call_args
        sent_text = call_kwargs.kwargs.get("text") or (call_kwargs[0][0] if call_kwargs[0] else None)
        self.assertEqual(sent_text, "Тест транскрипции")

    def test_send_to_telegram_returns_message_id(self):
        bridge = _make_bridge_stub(send_result={"message_id": 42, "sent_at": 1700000000.0, "chat_title": "Saved"})

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            result = BackendService._handle_send_to_telegram(svc, {
                "chat_id": 123,
                "text": "Hello",
            })

        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["chat_title"], "Saved")


class TestSendToTelegramBridgeOffline(unittest.TestCase):
    """send_to_telegram возвращает ошибку, когда мост недоступен."""

    def test_send_to_telegram_when_bridge_offline_returns_error(self):
        bridge = _make_bridge_stub(
            send_side_effect=requests.ConnectionError("Connection refused")
        )

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_send_to_telegram(svc, {
                    "chat_id": 123,
                    "text": "Hello",
                })

        self.assertIn("krab_unavailable", str(ctx.exception))

    def test_send_to_telegram_circuit_open_returns_error(self):
        bridge = _make_bridge_stub(
            send_side_effect=CircuitBreakerOpen("CB открыт")
        )

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_send_to_telegram(svc, {
                    "chat_id": 123,
                    "text": "Hello",
                })

        self.assertIn("circuit_open", str(ctx.exception))


class TestSendToTelegramInvalidParams(unittest.TestCase):
    """send_to_telegram валидирует параметры до вызова bridge."""

    def test_invalid_chat_id_returns_error(self):
        """Пустой chat_id → ValueError до вызова bridge."""
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises((ValueError, RuntimeError)):
                BackendService._handle_send_to_telegram(svc, {
                    "chat_id": "",
                    "text": "Привет",
                })

        bridge.send_message.assert_not_called()

    def test_empty_body_returns_error(self):
        """Пустой text → ValueError до вызова bridge."""
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises((ValueError, RuntimeError)):
                BackendService._handle_send_to_telegram(svc, {
                    "chat_id": 123,
                    "text": "   ",
                })

        bridge.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: list_telegram_chats
# ---------------------------------------------------------------------------

class TestListTelegramChatsReturnsChats(unittest.TestCase):
    """list_telegram_chats возвращает список чатов от bridge."""

    def test_list_chats_returns_chats(self):
        chats = [
            {"id": 1, "title": "Saved Messages", "type": "saved"},
            {"id": -100123, "title": "Work Group", "type": "group"},
        ]
        bridge = _make_bridge_stub(chats_result=chats)

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            result = BackendService._handle_list_telegram_chats(svc, {})

        self.assertIn("chats", result)
        self.assertEqual(len(result["chats"]), 2)
        self.assertEqual(result["chats"][0]["title"], "Saved Messages")
        self.assertEqual(result["chats"][1]["type"], "group")

    def test_list_chats_calls_bridge_get_chats(self):
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            BackendService._handle_list_telegram_chats(svc, {})

        bridge.get_chats.assert_called_once()


class TestListTelegramChatsOffline(unittest.TestCase):
    """list_telegram_chats возвращает ошибку, когда bridge недоступен."""

    def test_list_chats_when_offline_returns_error(self):
        bridge = _make_bridge_stub(
            chats_side_effect=requests.ConnectionError("offline")
        )

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_list_telegram_chats(svc, {})

        self.assertIn("krab_unavailable", str(ctx.exception))

    def test_list_chats_circuit_open_returns_error(self):
        bridge = _make_bridge_stub(
            chats_side_effect=CircuitBreakerOpen("CB открыт")
        )

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_list_telegram_chats(svc, {})

        self.assertIn("circuit_open", str(ctx.exception))


class TestBridgeDisabled(unittest.TestCase):
    """Оба метода возвращают ошибку bridge_disabled когда TELEGRAM_BRIDGE_ENABLED=False."""

    def test_send_disabled_raises(self):
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = False
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_send_to_telegram(svc, {"chat_id": 1, "text": "Hi"})

        self.assertIn("bridge_disabled", str(ctx.exception))
        bridge.send_message.assert_not_called()

    def test_list_disabled_raises(self):
        bridge = _make_bridge_stub()

        from backend.service import BackendService
        svc = MagicMock()
        svc._telegram_bridge = bridge

        with patch("backend.service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = False
            with self.assertRaises(RuntimeError) as ctx:
                BackendService._handle_list_telegram_chats(svc, {})

        self.assertIn("bridge_disabled", str(ctx.exception))
        bridge.get_chats.assert_not_called()


if __name__ == "__main__":
    unittest.main()
