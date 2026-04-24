"""Тесты TelnyxAdapter — Phase 3 REST adapter для исходящих звонков."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.telnyx_adapter import (  # noqa: E402
    TelnyxAdapter,
    _is_valid_phone,
)


def _make_adapter(
    api_key: str = "test_key_abc",
    connection_id: str = "conn_123",
    from_number: str = "+15550001111",
) -> TelnyxAdapter:
    return TelnyxAdapter(
        api_key=api_key,
        connection_id=connection_id,
        from_number=from_number,
    )


def _mock_response(
    status_code: int,
    json_data: object = None,
    headers: dict | None = None,
) -> MagicMock:
    """Создаёт мок requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = ""
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


class PhoneValidationTestCase(unittest.TestCase):
    """Тесты вспомогательной функции _is_valid_phone."""

    def test_valid_e164(self) -> None:
        self.assertTrue(_is_valid_phone("+15550001234"))
        self.assertTrue(_is_valid_phone("+79161234567"))
        self.assertTrue(_is_valid_phone("+1234"))

    def test_invalid_no_plus(self) -> None:
        self.assertFalse(_is_valid_phone("15550001234"))

    def test_invalid_letters(self) -> None:
        self.assertFalse(_is_valid_phone("+1abc"))

    def test_invalid_empty(self) -> None:
        self.assertFalse(_is_valid_phone(""))


class StubModeTestCase(unittest.TestCase):
    """Тесты stub-режима при пустом api_key."""

    def setUp(self) -> None:
        self.adapter = TelnyxAdapter(api_key="", connection_id="", from_number="")

    # 1 — dial в stub-режиме возвращает telnyx_not_configured
    def test_dial_stub_mode(self) -> None:
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "telnyx_not_configured")

    # 2 — hangup в stub-режиме возвращает telnyx_not_configured
    def test_hangup_stub_mode(self) -> None:
        result = self.adapter.hangup("ctrl_abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "telnyx_not_configured")

    # 3 — get_call_status в stub-режиме возвращает telnyx_not_configured
    def test_get_call_status_stub_mode(self) -> None:
        result = self.adapter.get_call_status("ctrl_abc")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "telnyx_not_configured")

    # 4 — list_active_calls в stub-режиме возвращает telnyx_not_configured
    def test_list_active_calls_stub_mode(self) -> None:
        result = self.adapter.list_active_calls()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "telnyx_not_configured")

    # 5 — api_key из пробелов также даёт stub-режим
    def test_whitespace_key_is_stub(self) -> None:
        a = TelnyxAdapter(api_key="   ")
        result = a.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "telnyx_not_configured")


class DialSuccessTestCase(unittest.TestCase):
    """Тесты успешного dial."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 6 — успешный dial возвращает call_id и call_control_id
    def test_dial_success(self) -> None:
        mock_resp = _mock_response(
            201,
            {
                "data": {
                    "call_leg_id": "leg_xyz",
                    "call_control_id": "ctrl_xyz",
                    "status": "ringing",
                }
            },
        )
        with patch.object(self.adapter._get_session(), "post", return_value=mock_resp):
            # Пересоздаём сессию, чтобы patch применился
            self.adapter._session = None
            sess_mock = MagicMock()
            sess_mock.post.return_value = mock_resp
            sess_mock.headers = {}
            self.adapter._session = sess_mock

            result = self.adapter.dial("+15550009999")

        self.assertTrue(result["ok"])
        self.assertEqual(result["call_id"], "leg_xyz")
        self.assertEqual(result["call_control_id"], "ctrl_xyz")
        self.assertEqual(result["to_number"], "+15550009999")

    # 7 — невалидный номер → invalid_phone_number (без HTTP-запроса)
    def test_dial_invalid_phone(self) -> None:
        result = self.adapter.dial("not_a_number")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_phone_number")


class DialErrorTestCase(unittest.TestCase):
    """Тесты обработки ошибок dial."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    def _set_mock_response(self, mock_resp: MagicMock) -> None:
        sess_mock = MagicMock()
        sess_mock.post.return_value = mock_resp
        sess_mock.headers = {}
        self.adapter._session = sess_mock

    # 8 — HTTP 402 insufficient balance
    def test_dial_insufficient_balance(self) -> None:
        self._set_mock_response(_mock_response(402, {}))
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_balance")

    # 9 — HTTP 429 rate limit
    def test_dial_rate_limit(self) -> None:
        self._set_mock_response(
            _mock_response(429, {}, headers={"Retry-After": "0"})
        )
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rate_limit")

    # 10 — HTTP 422 unreachable number
    def test_dial_unreachable_number(self) -> None:
        self._set_mock_response(_mock_response(422, {}))
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unreachable_number")


class HangupTestCase(unittest.TestCase):
    """Тесты hangup."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    def _set_mock_response(self, mock_resp: MagicMock) -> None:
        sess_mock = MagicMock()
        sess_mock.post.return_value = mock_resp
        sess_mock.headers = {}
        self.adapter._session = sess_mock

    # 11 — успешный hangup возвращает ok=True
    def test_hangup_success(self) -> None:
        self._set_mock_response(_mock_response(200, {"data": {}}))
        result = self.adapter.hangup("ctrl_123")
        self.assertTrue(result["ok"])

    # 12 — hangup с пустым call_control_id → ошибка без запроса
    def test_hangup_missing_id(self) -> None:
        result = self.adapter.hangup("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_call_control_id")


class ListActiveCallsTestCase(unittest.TestCase):
    """Тесты list_active_calls."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 13 — list_active_calls корректно парсит ответ API
    def test_list_active_calls_parsing(self) -> None:
        mock_resp = _mock_response(
            200,
            {
                "data": [
                    {
                        "call_leg_id": "leg_1",
                        "call_control_id": "ctrl_1",
                        "to": "+15550001111",
                        "from": "+15550002222",
                        "status": "answered",
                        "start_time": None,
                    },
                    {
                        "call_leg_id": "leg_2",
                        "call_control_id": "ctrl_2",
                        "to": "+17890001111",
                        "from": "+15550002222",
                        "status": "ringing",
                        "start_time": None,
                    },
                ]
            },
        )
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        self.adapter._session = sess_mock

        result = self.adapter.list_active_calls()
        self.assertTrue(result["ok"])
        calls = result["calls"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["id"], "leg_1")
        self.assertEqual(calls[0]["to_number"], "+15550001111")
        self.assertEqual(calls[0]["status"], "answered")
        self.assertEqual(calls[1]["status"], "ringing")

    # 14 — list_active_calls с пустым data возвращает пустой список
    def test_list_active_calls_empty(self) -> None:
        mock_resp = _mock_response(200, {"data": []})
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        self.adapter._session = sess_mock

        result = self.adapter.list_active_calls()
        self.assertTrue(result["ok"])
        self.assertEqual(result["calls"], [])

    # 15 — get_call_status возвращает статус из поля data.status
    def test_get_call_status_returns_status(self) -> None:
        mock_resp = _mock_response(
            200,
            {"data": {"call_control_id": "ctrl_abc", "status": "bridged"}},
        )
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        self.adapter._session = sess_mock

        result = self.adapter.get_call_status("ctrl_abc")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "bridged")


if __name__ == "__main__":
    unittest.main()
