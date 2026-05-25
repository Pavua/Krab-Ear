"""Wave 622: TelegramBridge unit tests.

Проверяет send_message happy path, retry-поведение circuit breaker на
ConnectionError, graceful-обработку недоступного main Krab (503),
malformed JSON-ответ и timeout.
"""
from __future__ import annotations

import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRABEAR_ROOT not in sys.path:
    sys.path.insert(0, KRABEAR_ROOT)

import requests

from backend.telegram_bridge import TelegramBridge, CircuitBreakerOpen


def _make_response(status_code: int, json_body=None, text: str = "") -> MagicMock:
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = (200 <= status_code < 300)
    resp.text = text
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


class TestTelegramBridgeWave622(unittest.TestCase):

    def setUp(self):
        self.bridge = TelegramBridge(
            base_url="http://localhost:8080",
            timeout_sec=2.0,
            circuit_fail_threshold=3,
            circuit_reset_sec=60.0,
        )

    # ------------------------------------------------------------------
    # Test 1: happy path — 200 OK
    # ------------------------------------------------------------------
    def test_happy_path_200(self):
        payload_in = {"message_id": 42, "sent_at": 1716000000.0, "chat_title": "Test Chat"}
        mock_resp = _make_response(200, json_body=payload_in)

        with patch("backend.telegram_bridge.requests.post", return_value=mock_resp) as mock_post:
            result = self.bridge.send_message("Привет", chat_id=123456)

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        self.assertIn("/api/notify", call_kwargs.args[0])
        self.assertEqual(result["message_id"], 42)
        self.assertEqual(result["chat_title"], "Test Chat")
        self.assertFalse(self.bridge.is_circuit_open)

    # ------------------------------------------------------------------
    # Test 2: retry on ConnectionError opens circuit breaker
    # ------------------------------------------------------------------
    def test_connection_refused_increments_circuit(self):
        with patch(
            "backend.telegram_bridge.requests.post",
            side_effect=requests.ConnectionError("Connection refused"),
        ):
            for _ in range(self.bridge._circuit_fail_threshold):
                with self.assertRaises(requests.ConnectionError):
                    self.bridge.send_message("msg", chat_id=1)

        # After threshold failures the circuit should be open
        self.assertTrue(self.bridge.is_circuit_open)
        # Next call raises CircuitBreakerOpen without hitting HTTP
        with self.assertRaises(CircuitBreakerOpen):
            self.bridge.send_message("msg", chat_id=1)

    # ------------------------------------------------------------------
    # Test 3: main Krab not running — 503 graceful error
    # ------------------------------------------------------------------
    def test_krab_not_running_503_raises_runtime_error(self):
        mock_resp = _make_response(
            503,
            json_body={"detail": "userbot_not_ready"},
            text='{"detail":"userbot_not_ready"}',
        )

        with patch("backend.telegram_bridge.requests.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError) as ctx:
                self.bridge.send_message("hello", chat_id=999)

        self.assertIn("krab_unavailable", str(ctx.exception))
        self.assertIn("userbot_not_ready", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 4: malformed / non-JSON response
    # ------------------------------------------------------------------
    def test_malformed_response_falls_back_to_text(self):
        # Server returns 500 with non-JSON body
        mock_resp = _make_response(500, text="Internal Server Error")

        with patch("backend.telegram_bridge.requests.post", return_value=mock_resp):
            with self.assertRaises(RuntimeError) as ctx:
                self.bridge.send_message("test", chat_id=1)

        # RuntimeError message should include the raw text fragment
        self.assertIn("krab_error", str(ctx.exception))

    # ------------------------------------------------------------------
    # Test 5: timeout raises requests.Timeout
    # ------------------------------------------------------------------
    def test_timeout_raises_and_records_failure(self):
        with patch(
            "backend.telegram_bridge.requests.post",
            side_effect=requests.Timeout("timed out"),
        ):
            with self.assertRaises(requests.Timeout):
                self.bridge.send_message("slow", chat_id=7)

        # One failure recorded but circuit not yet open (threshold=3)
        self.assertEqual(self.bridge._fail_count, 1)
        self.assertFalse(self.bridge.is_circuit_open)


if __name__ == "__main__":
    unittest.main()
