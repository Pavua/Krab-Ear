"""Тесты CallProviderFactory — выбор провайдера по настройкам."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_provider_factory import (  # noqa: E402
    NullCallProvider,
    get_provider,
)
from backend.telnyx_adapter import TelnyxAdapter  # noqa: E402
from backend.twilio_adapter import TwilioAdapter  # noqa: E402


def _settings(**kwargs) -> SimpleNamespace:
    """Создаёт объект настроек с разумными дефолтами."""
    defaults = {
        "CALL_PROVIDER": "telnyx",
        "TELNYX_API_KEY": "",
        "TELNYX_CONNECTION_ID": "",
        "TELNYX_FROM_NUMBER": "",
        "TWILIO_ACCOUNT_SID": "",
        "TWILIO_AUTH_TOKEN": "",
        "TWILIO_FROM_NUMBER": "",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TelnyxProviderTestCase(unittest.TestCase):
    """Тесты выбора TelnyxAdapter."""

    # 1 — CALL_PROVIDER=telnyx возвращает TelnyxAdapter
    def test_telnyx_provider_type(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="telnyx"))
        self.assertIsInstance(provider, TelnyxAdapter)

    # 2 — CALL_PROVIDER=telnyx с api_key — adapter сконфигурирован
    def test_telnyx_configured(self) -> None:
        provider = get_provider(
            _settings(
                CALL_PROVIDER="telnyx",
                TELNYX_API_KEY="real_key",
                TELNYX_FROM_NUMBER="+15550001111",
            )
        )
        self.assertIsInstance(provider, TelnyxAdapter)
        self.assertTrue(provider._configured)  # noqa: SLF001

    # 3 — дефолт (без CALL_PROVIDER) → TelnyxAdapter
    def test_default_is_telnyx(self) -> None:
        s = SimpleNamespace()  # нет атрибутов вообще
        provider = get_provider(s)
        self.assertIsInstance(provider, TelnyxAdapter)


class TwilioProviderTestCase(unittest.TestCase):
    """Тесты выбора TwilioAdapter."""

    # 4 — CALL_PROVIDER=twilio возвращает TwilioAdapter
    def test_twilio_provider_type(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="twilio"))
        self.assertIsInstance(provider, TwilioAdapter)

    # 5 — CALL_PROVIDER=twilio с credentials — adapter сконфигурирован
    def test_twilio_configured(self) -> None:
        provider = get_provider(
            _settings(
                CALL_PROVIDER="twilio",
                TWILIO_ACCOUNT_SID="ACDEADBEEF1234567890ABCDEF0000FFFF",
                TWILIO_AUTH_TOKEN="authtest456",
                TWILIO_FROM_NUMBER="+15550009999",
            )
        )
        self.assertIsInstance(provider, TwilioAdapter)
        self.assertTrue(provider.is_configured())

    # 6 — twilio без credentials → TwilioAdapter в stub-режиме
    def test_twilio_stub_mode(self) -> None:
        provider = get_provider(
            _settings(CALL_PROVIDER="twilio", TWILIO_ACCOUNT_SID="", TWILIO_AUTH_TOKEN="")
        )
        self.assertIsInstance(provider, TwilioAdapter)
        self.assertFalse(provider.is_configured())
        result = provider.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "twilio_not_configured")


class NullProviderTestCase(unittest.TestCase):
    """Тесты NullCallProvider."""

    # 7 — CALL_PROVIDER=none → NullCallProvider
    def test_none_provider(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="none"))
        self.assertIsInstance(provider, NullCallProvider)

    # 8 — неизвестный провайдер → NullCallProvider
    def test_unknown_provider(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="vonage"))
        self.assertIsInstance(provider, NullCallProvider)

    # 9 — NullCallProvider.dial возвращает no_provider
    def test_null_dial_returns_error(self) -> None:
        provider = NullCallProvider()
        result = provider.dial("+15550001234")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_provider")

    # 10 — NullCallProvider.hangup возвращает no_provider
    def test_null_hangup_returns_error(self) -> None:
        provider = NullCallProvider()
        result = provider.hangup("CA123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_provider")

    # 11 — NullCallProvider.get_call_status возвращает no_provider
    def test_null_get_status_returns_error(self) -> None:
        provider = NullCallProvider()
        result = provider.get_call_status("CA123")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_provider")

    # 12 — NullCallProvider.list_active_calls возвращает no_provider
    def test_null_list_active_calls_returns_error(self) -> None:
        provider = NullCallProvider()
        result = provider.list_active_calls()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_provider")

    # 13 — NullCallProvider.is_configured возвращает False
    def test_null_is_configured_false(self) -> None:
        provider = NullCallProvider()
        self.assertFalse(provider.is_configured())


class ProviderCaseInsensitiveTestCase(unittest.TestCase):
    """Тесты нечувствительности CALL_PROVIDER к регистру."""

    # 14 — CALL_PROVIDER="TELNYX" (uppercase) → TelnyxAdapter
    def test_telnyx_uppercase_still_works(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="TELNYX"))
        self.assertIsInstance(provider, TelnyxAdapter)

    # 15 — CALL_PROVIDER="Twilio" (mixed case) → TwilioAdapter
    def test_twilio_mixed_case_still_works(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="Twilio"))
        self.assertIsInstance(provider, TwilioAdapter)

    # 16 — CALL_PROVIDER=" telnyx " (whitespace) → TelnyxAdapter
    def test_telnyx_with_whitespace_still_works(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER=" telnyx "))
        self.assertIsInstance(provider, TelnyxAdapter)


class ProviderCredentialsTestCase(unittest.TestCase):
    """Тесты передачи credentials в адаптер."""

    # 17 — Telnyx credentials передаются в адаптер корректно
    def test_telnyx_credentials_passed_correctly(self) -> None:
        provider = get_provider(
            _settings(
                CALL_PROVIDER="telnyx",
                TELNYX_API_KEY="real_api_key",
                TELNYX_CONNECTION_ID="conn_abc",
                TELNYX_FROM_NUMBER="+15550001111",
            )
        )
        self.assertIsInstance(provider, TelnyxAdapter)
        self.assertEqual(provider._api_key, "real_api_key")
        self.assertEqual(provider._connection_id, "conn_abc")
        self.assertEqual(provider._from_number, "+15550001111")

    # 18 — Twilio credentials передаются в адаптер корректно
    def test_twilio_credentials_passed_correctly(self) -> None:
        provider = get_provider(
            _settings(
                CALL_PROVIDER="twilio",
                TWILIO_ACCOUNT_SID="ACDEADBEEF1234567890ABCDEF0000FFFF",
                TWILIO_AUTH_TOKEN="authtest456",
                TWILIO_FROM_NUMBER="+15550009999",
            )
        )
        self.assertIsInstance(provider, TwilioAdapter)
        self.assertEqual(provider._account_sid, "ACDEADBEEF1234567890ABCDEF0000FFFF")
        self.assertEqual(provider._auth_token, "authtest456")
        self.assertEqual(provider._from_number, "+15550009999")

    # 19 — NullCallProvider list_active_calls возвращает no_provider
    def test_null_list_active_calls_no_provider(self) -> None:
        provider = get_provider(_settings(CALL_PROVIDER="none"))
        result = provider.list_active_calls()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_provider")

    # 20 — CALL_PROVIDER=sip_local возвращает LocalSIPAdapter
    def test_sip_local_provider_selection(self) -> None:
        from backend.sip_local_adapter import LocalSIPAdapter

        provider = get_provider(
            _settings(
                CALL_PROVIDER="sip_local",
                SIP_SERVER="sip.example.com",
                SIP_PORT=5060,
                SIP_USER="user1",
                SIP_PASSWORD="pwd",
                SIP_FROM_NUMBER="+15551112233",
            )
        )
        self.assertIsInstance(provider, LocalSIPAdapter)
        self.assertTrue(provider.is_configured())
        self.assertEqual(provider._server, "sip.example.com")
        self.assertEqual(provider._port, 5060)
        self.assertEqual(provider._user, "user1")
        self.assertEqual(provider._from_number, "+15551112233")


if __name__ == "__main__":
    unittest.main()
