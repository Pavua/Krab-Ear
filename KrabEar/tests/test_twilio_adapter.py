"""Тесты TwilioAdapter — Phase 3 Twilio REST adapter для исходящих звонков."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.twilio_adapter import (  # noqa: E402
    TwilioAdapter,
    _is_valid_phone,
)


def _make_adapter(
    account_sid: str = "ACtest123",
    auth_token: str = "tokentest456",
    from_number: str = "+15550001111",
) -> TwilioAdapter:
    return TwilioAdapter(
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
    )


def _mock_response(
    status_code: int,
    json_data: object = None,
    headers: dict | None = None,
    content: bytes = b"{}",
) -> MagicMock:
    """Создаёт мок requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = ""
    resp.content = content
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


def _set_mock_post(adapter: TwilioAdapter, mock_resp: MagicMock) -> None:
    sess_mock = MagicMock()
    sess_mock.post.return_value = mock_resp
    sess_mock.headers = {}
    adapter._session = sess_mock


def _set_mock_get(adapter: TwilioAdapter, mock_resp: MagicMock) -> None:
    sess_mock = MagicMock()
    sess_mock.get.return_value = mock_resp
    sess_mock.headers = {}
    adapter._session = sess_mock


class PhoneValidationTestCase(unittest.TestCase):
    """Тесты вспомогательной функции _is_valid_phone."""

    # 1 — валидные E.164 номера
    def test_valid_e164_us(self) -> None:
        self.assertTrue(_is_valid_phone("+15550001234"))

    # 2 — российский номер
    def test_valid_e164_ru(self) -> None:
        self.assertTrue(_is_valid_phone("+79161234567"))

    # 3 — без плюса — невалидный
    def test_invalid_no_plus(self) -> None:
        self.assertFalse(_is_valid_phone("15550001234"))

    # 4 — пустая строка — невалидная
    def test_invalid_empty(self) -> None:
        self.assertFalse(_is_valid_phone(""))

    # 5 — буквы в номере — невалидные
    def test_invalid_letters(self) -> None:
        self.assertFalse(_is_valid_phone("+1abc"))


class StubModeTestCase(unittest.TestCase):
    """Тесты stub-режима при пустых credentials."""

    def setUp(self) -> None:
        self.adapter = TwilioAdapter(account_sid="", auth_token="", from_number="")

    # 6 — dial в stub-режиме возвращает twilio_not_configured
    def test_dial_stub_mode(self) -> None:
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")

    # 7 — hangup в stub-режиме
    def test_hangup_stub_mode(self) -> None:
        result = self.adapter.hangup("CA123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")

    # 8 — get_call_status в stub-режиме
    def test_get_call_status_stub_mode(self) -> None:
        result = self.adapter.get_call_status("CA123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")

    # 9 — list_active_calls в stub-режиме
    def test_list_active_calls_stub_mode(self) -> None:
        result = self.adapter.list_active_calls()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")

    # 10 — is_configured возвращает False
    def test_is_configured_false(self) -> None:
        self.assertFalse(self.adapter.is_configured())

    # 11 — только sid без token — тоже stub
    def test_partial_credentials_is_stub(self) -> None:
        a = TwilioAdapter(account_sid="ACxxx", auth_token="")
        self.assertFalse(a.is_configured())
        result = a.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")


class IsConfiguredTestCase(unittest.TestCase):
    """Тесты is_configured."""

    # 12 — полные credentials → True
    def test_is_configured_true(self) -> None:
        adapter = _make_adapter()
        self.assertTrue(adapter.is_configured())

    # 13 — whitespace credentials — считаются пустыми
    def test_whitespace_credentials_stub(self) -> None:
        a = TwilioAdapter(account_sid="  ", auth_token="  ")
        self.assertFalse(a.is_configured())


class DialTestCase(unittest.TestCase):
    """Тесты метода dial."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 14 — успешный dial возвращает call_id == call_control_id == sid
    def test_dial_success(self) -> None:
        mock_resp = _mock_response(
            201,
            {
                "sid": "CAabc123",
                "status": "queued",
                "to": "+15550009999",
                "from": "+15550001111",
            },
        )
        _set_mock_post(self.adapter, mock_resp)
        result = self.adapter.dial("+15550009999")
        self.assertTrue(result["ok"])
        self.assertEqual(result["call_id"], "CAabc123")
        self.assertEqual(result["call_control_id"], "CAabc123")
        self.assertEqual(result["to_number"], "+15550009999")

    # 15 — невалидный номер возвращает invalid_phone_number без HTTP-запроса
    def test_dial_invalid_phone(self) -> None:
        result = self.adapter.dial("not_a_number")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_phone_number")

    # 16 — HTTP 401 → unauthorized
    def test_dial_unauthorized(self) -> None:
        _set_mock_post(self.adapter, _mock_response(401, {"message": "Unauthorized"}))
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unauthorized")

    # 17 — HTTP 400 → validation_error
    def test_dial_validation_error(self) -> None:
        _set_mock_post(
            self.adapter,
            _mock_response(400, {"message": "The 'To' number is not valid", "code": 21211}),
        )
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "validation_error")

    # 18 — HTTP 429 rate limit
    def test_dial_rate_limit(self) -> None:
        _set_mock_post(
            self.adapter,
            _mock_response(429, {}, headers={"Retry-After": "0"}),
        )
        result = self.adapter.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rate_limit")

    # 19 — webhook_url передаётся в payload
    def test_dial_with_webhook_url(self) -> None:
        mock_resp = _mock_response(201, {"sid": "CA_wh", "status": "queued"})
        _set_mock_post(self.adapter, mock_resp)
        result = self.adapter.dial("+15550001234", webhook_url="https://example.com/hook")
        self.assertTrue(result["ok"])
        # Проверяем что StatusCallback попал в вызов post
        sess = self.adapter._session
        call_kwargs = sess.post.call_args
        data_payload = call_kwargs[1].get("data") or call_kwargs[0][1] if call_kwargs[0] else {}
        self.assertIn("StatusCallback", data_payload)


class HangupTestCase(unittest.TestCase):
    """Тесты метода hangup."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 20 — успешный hangup (200) возвращает ok=True
    def test_hangup_success(self) -> None:
        _set_mock_post(
            self.adapter,
            _mock_response(200, {"sid": "CA123", "status": "completed"}),
        )
        result = self.adapter.hangup("CA123")
        self.assertTrue(result["ok"])

    # 21 — hangup с пустым call_control_id → ошибка без запроса
    def test_hangup_missing_id(self) -> None:
        result = self.adapter.hangup("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_call_control_id")


class GetCallStatusTestCase(unittest.TestCase):
    """Тесты метода get_call_status."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 22 — возвращает статус из поля data.status
    def test_get_call_status_in_progress(self) -> None:
        _set_mock_get(
            self.adapter,
            _mock_response(200, {"sid": "CA123", "status": "in-progress"}),
        )
        result = self.adapter.get_call_status("CA123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "in-progress")

    # 23 — HTTP 404 → not_found
    def test_get_call_status_not_found(self) -> None:
        _set_mock_get(self.adapter, _mock_response(404, {"message": "Not found"}))
        result = self.adapter.get_call_status("CA_missing")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_found")

    # 24 — пустой call_control_id → ошибка без запроса
    def test_get_call_status_missing_id(self) -> None:
        result = self.adapter.get_call_status("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "missing_call_control_id")


class ListActiveCallsTestCase(unittest.TestCase):
    """Тесты метода list_active_calls."""

    def setUp(self) -> None:
        self.adapter = _make_adapter()

    # 25 — парсит список звонков из ответа Twilio
    def test_list_active_calls_parsing(self) -> None:
        _set_mock_get(
            self.adapter,
            _mock_response(
                200,
                {
                    "calls": [
                        {
                            "sid": "CA001",
                            "to": "+15550001111",
                            "from": "+15550002222",
                            "status": "in-progress",
                            "duration": "42",
                        }
                    ]
                },
            ),
        )
        result = self.adapter.list_active_calls()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["calls"]), 1)
        c = result["calls"][0]
        self.assertEqual(c["id"], "CA001")
        self.assertEqual(c["call_control_id"], "CA001")
        self.assertEqual(c["to_number"], "+15550001111")
        self.assertEqual(c["duration_sec"], 42)
        self.assertEqual(c["status"], "in-progress")

    # 26 — пустой ответ возвращает пустой список
    def test_list_active_calls_empty(self) -> None:
        _set_mock_get(self.adapter, _mock_response(200, {"calls": []}))
        result = self.adapter.list_active_calls()
        self.assertTrue(result["ok"])
        self.assertEqual(result["calls"], [])


if __name__ == "__main__":
    unittest.main()
