"""Тесты CallProvider Protocol — структурная типизация и interface parity.

Проверяет что TelnyxAdapter и TwilioAdapter реализуют Protocol CallProvider
(runtime_checkable), и что все адаптеры совместимы по сигнатурам методов.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_provider import (  # noqa: E402
    CallProvider,
    ERR_INSUFFICIENT_BALANCE,
    ERR_INVALID_PHONE,
    ERR_MISSING_CALL_ID,
    ERR_NETWORK,
    ERR_NOT_CONFIGURED,
    ERR_RATE_LIMIT,
    ERR_UNAUTHORIZED,
    ERR_UNREACHABLE_NUMBER,
    _not_configured_result,
)
from backend.call_provider_factory import NullCallProvider  # noqa: E402
from backend.telnyx_adapter import TelnyxAdapter  # noqa: E402
from backend.twilio_adapter import TwilioAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Protocol isinstance checks (runtime_checkable)
# ---------------------------------------------------------------------------

class ProtocolIsInstanceTestCase(unittest.TestCase):
    """Тесты проверки isinstance против @runtime_checkable Protocol."""

    # 1 — TelnyxAdapter реализует CallProvider Protocol
    def test_telnyx_adapter_implements_call_provider(self) -> None:
        adapter = TelnyxAdapter()
        self.assertIsInstance(adapter, CallProvider)

    # 2 — TwilioAdapter реализует CallProvider Protocol
    def test_twilio_adapter_implements_call_provider(self) -> None:
        adapter = TwilioAdapter()
        self.assertIsInstance(adapter, CallProvider)

    # 3 — NullCallProvider реализует CallProvider Protocol
    def test_null_provider_implements_call_provider(self) -> None:
        provider = NullCallProvider()
        self.assertIsInstance(provider, CallProvider)

    # 4 — Обычный dict НЕ реализует Protocol
    def test_plain_dict_does_not_implement_call_provider(self) -> None:
        self.assertNotIsInstance({}, CallProvider)

    # 5 — Класс без нужных методов НЕ реализует Protocol
    def test_incomplete_class_not_call_provider(self) -> None:
        class Incomplete:
            def dial(self, to_number: str) -> dict:
                return {}

        self.assertNotIsInstance(Incomplete(), CallProvider)


# ---------------------------------------------------------------------------
# Method signature parity
# ---------------------------------------------------------------------------

class MethodSignatureParityTestCase(unittest.TestCase):
    """Проверяет что TelnyxAdapter и TwilioAdapter имеют одинаковые методы."""

    _REQUIRED_METHODS = ("dial", "hangup", "get_call_status", "list_active_calls", "is_configured")

    def _check_has_methods(self, cls: type) -> None:
        for method_name in self._REQUIRED_METHODS:
            self.assertTrue(
                hasattr(cls, method_name),
                f"{cls.__name__} missing method '{method_name}'",
            )
            self.assertTrue(
                callable(getattr(cls, method_name)),
                f"{cls.__name__}.{method_name} is not callable",
            )

    # 6 — TelnyxAdapter имеет все методы CallProvider
    def test_telnyx_has_all_required_methods(self) -> None:
        self._check_has_methods(TelnyxAdapter)

    # 7 — TwilioAdapter имеет все методы CallProvider
    def test_twilio_has_all_required_methods(self) -> None:
        self._check_has_methods(TwilioAdapter)

    # 8 — NullCallProvider имеет все методы CallProvider
    def test_null_provider_has_all_required_methods(self) -> None:
        self._check_has_methods(NullCallProvider)

    # 9 — dial принимает to_number, call_control_id, webhook_url у TelnyxAdapter
    def test_telnyx_dial_signature(self) -> None:
        sig = inspect.signature(TelnyxAdapter.dial)
        params = list(sig.parameters.keys())
        self.assertIn("to_number", params)
        self.assertIn("call_control_id", params)
        self.assertIn("webhook_url", params)

    # 10 — dial принимает to_number, call_control_id, webhook_url у TwilioAdapter
    def test_twilio_dial_signature(self) -> None:
        sig = inspect.signature(TwilioAdapter.dial)
        params = list(sig.parameters.keys())
        self.assertIn("to_number", params)
        self.assertIn("call_control_id", params)
        self.assertIn("webhook_url", params)

    # 11 — hangup принимает call_control_id у обоих адаптеров
    def test_hangup_signature_parity(self) -> None:
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.hangup)
            params = list(sig.parameters.keys())
            self.assertIn("call_control_id", params, f"{cls.__name__}.hangup missing call_control_id")

    # 12 — get_call_status принимает call_control_id у обоих адаптеров
    def test_get_call_status_signature_parity(self) -> None:
        for cls in (TelnyxAdapter, TwilioAdapter):
            sig = inspect.signature(cls.get_call_status)
            params = list(sig.parameters.keys())
            self.assertIn(
                "call_control_id", params,
                f"{cls.__name__}.get_call_status missing call_control_id",
            )


# ---------------------------------------------------------------------------
# Return value shape parity
# ---------------------------------------------------------------------------

class ReturnValueParityTestCase(unittest.TestCase):
    """Проверяет что оба адаптера в stub-режиме возвращают совместимые dict."""

    def setUp(self) -> None:
        self.telnyx = TelnyxAdapter()   # stub: no api_key
        self.twilio = TwilioAdapter()   # stub: no credentials

    # 13 — stub dial у обоих возвращает {"ok": False, ...}
    def test_stub_dial_returns_ok_false(self) -> None:
        for adapter in (self.telnyx, self.twilio):
            result = adapter.dial("+15550001234")
            self.assertIn("ok", result)
            self.assertFalse(result["ok"])
            self.assertIn("error", result)

    # 14 — stub hangup у обоих возвращает {"ok": False, ...}
    def test_stub_hangup_returns_ok_false(self) -> None:
        for adapter in (self.telnyx, self.twilio):
            result = adapter.hangup("some_id")
            self.assertFalse(result["ok"])
            self.assertIn("error", result)

    # 15 — stub get_call_status у обоих возвращает {"ok": False, ...}
    def test_stub_get_call_status_returns_ok_false(self) -> None:
        for adapter in (self.telnyx, self.twilio):
            result = adapter.get_call_status("some_id")
            self.assertFalse(result["ok"])

    # 16 — stub list_active_calls у обоих возвращает {"ok": False, ...}
    def test_stub_list_active_calls_returns_ok_false(self) -> None:
        for adapter in (self.telnyx, self.twilio):
            result = adapter.list_active_calls()
            self.assertFalse(result["ok"])

    # 17 — stub is_configured у обоих возвращает False
    def test_stub_is_configured_false(self) -> None:
        for adapter in (self.telnyx, self.twilio):
            self.assertFalse(adapter.is_configured())


# ---------------------------------------------------------------------------
# Error constants
# ---------------------------------------------------------------------------

class ErrorConstantsTestCase(unittest.TestCase):
    """Проверяет что error-константы определены и являются строками."""

    # 18 — все константы являются строками
    def test_error_constants_are_strings(self) -> None:
        for const in (
            ERR_NOT_CONFIGURED,
            ERR_INVALID_PHONE,
            ERR_UNAUTHORIZED,
            ERR_INSUFFICIENT_BALANCE,
            ERR_UNREACHABLE_NUMBER,
            ERR_RATE_LIMIT,
            ERR_NETWORK,
            ERR_MISSING_CALL_ID,
        ):
            self.assertIsInstance(const, str, f"Constant {const!r} is not a string")
            self.assertTrue(len(const) > 0)

    # 19 — _not_configured_result возвращает ok=False и содержит provider в error
    def test_not_configured_result_shape(self) -> None:
        result = _not_configured_result("telnyx")
        self.assertFalse(result["ok"])
        self.assertIn("telnyx", result["error"])
        self.assertIn("message", result)

    # 20 — _not_configured_result различается по provider name
    def test_not_configured_result_provider_specific(self) -> None:
        r_telnyx = _not_configured_result("telnyx")
        r_twilio = _not_configured_result("twilio")
        self.assertNotEqual(r_telnyx["error"], r_twilio["error"])
        self.assertIn("telnyx", r_telnyx["error"])
        self.assertIn("twilio", r_twilio["error"])


if __name__ == "__main__":
    unittest.main()
