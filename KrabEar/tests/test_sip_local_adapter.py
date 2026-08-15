"""Тесты для LocalSIPAdapter (Krab Ear On-Device Telephony).

Проверяет:
1. Соответствие протоколу CallProvider.
2. Stub-режим при неполных credentials (is_configured == False).
3. Исходящий вызов (dial) с валидным и невалидным номером.
4. Завершение вызова (hangup).
5. Получение статуса звонка (get_call_status).
6. Список активных звонков (list_active_calls).
7. Интеграция с CallProviderFactory (get_provider).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_provider import (
    CallProvider,
    ERR_INVALID_PHONE,
    ERR_MISSING_CALL_ID,
    ERR_UNREACHABLE_NUMBER,
)
from backend.call_provider_factory import (
    PROVIDER_SIP_LOCAL,
    get_provider,
)
from backend.sip_local_adapter import LocalSIPAdapter


class TestLocalSIPAdapter(unittest.TestCase):
    """Набор тестов для LocalSIPAdapter."""

    def test_implements_call_provider_protocol(self) -> None:
        """LocalSIPAdapter обязан реализовывать runtime-checkable Protocol CallProvider."""
        adapter = LocalSIPAdapter(
            server="sip.local.domain",
            port=5060,
            user="101",
            password="secret_password",
        )
        self.assertIsInstance(adapter, CallProvider)

    def test_unconfigured_when_missing_server_or_user(self) -> None:
        """Без server или user адаптер работает в stub-режиме."""
        unconfigured_cases = [
            LocalSIPAdapter(),
            LocalSIPAdapter(server="sip.local.domain", user=""),
            LocalSIPAdapter(server="", user="101"),
        ]
        for adapter in unconfigured_cases:
            self.assertFalse(adapter.is_configured())
            dial_res = adapter.dial("+79001234567")
            self.assertFalse(dial_res["ok"])
            self.assertEqual(dial_res["error"], "sip_local_not_configured")

            hangup_res = adapter.hangup("any_id")
            self.assertFalse(hangup_res["ok"])
            self.assertEqual(hangup_res["error"], "sip_local_not_configured")

            status_res = adapter.get_call_status("any_id")
            self.assertFalse(status_res["ok"])
            self.assertEqual(status_res["error"], "sip_local_not_configured")

            list_res = adapter.list_active_calls()
            self.assertFalse(list_res["ok"])
            self.assertEqual(list_res["error"], "sip_local_not_configured")

    def test_configured_when_server_and_user_present(self) -> None:
        """При наличии server и user адаптер считается сконфигурированным."""
        adapter = LocalSIPAdapter(
            server="192.168.1.100",
            port=5060,
            user="200",
            password="secret_password",
            from_number="+15551234567",
        )
        self.assertTrue(adapter.is_configured())

    def test_dial_invalid_phone_number(self) -> None:
        """Невалидный номер назначения возвращает ошибку ERR_INVALID_PHONE."""
        adapter = LocalSIPAdapter(
            server="sip.local",
            user="100",
            password="pwd",
        )
        invalid_numbers = ["abc", "12", "+0123", "", "   "]
        for num in invalid_numbers:
            res = adapter.dial(num)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], ERR_INVALID_PHONE)

    def test_dial_and_lifecycle(self) -> None:
        """Успешный исходящий вызов, проверка статуса, листинга и завершения."""
        adapter = LocalSIPAdapter(
            server="sip.local",
            port=5060,
            user="100",
            password="pwd",
            from_number="+15551000000",
        )
        # 1. Dial E.164
        dial_res = adapter.dial("+79001234567", call_control_id="custom_call_1")
        self.assertTrue(dial_res["ok"])
        self.assertEqual(dial_res["call_control_id"], "custom_call_1")
        self.assertEqual(dial_res["to"], "+79001234567")
        self.assertEqual(dial_res["status"], "initiated")

        # 2. Status
        status_res = adapter.get_call_status("custom_call_1")
        self.assertTrue(status_res["ok"])
        self.assertEqual(status_res["call_control_id"], "custom_call_1")
        self.assertEqual(status_res["status"], "initiated")
        self.assertGreaterEqual(status_res["duration_sec"], 0.0)

        # 3. List
        list_res = adapter.list_active_calls()
        self.assertTrue(list_res["ok"])
        self.assertEqual(list_res["count"], 1)
        self.assertEqual(list_res["calls"][0]["call_control_id"], "custom_call_1")

        # 4. Hangup
        hangup_res = adapter.hangup("custom_call_1")
        self.assertTrue(hangup_res["ok"])
        self.assertEqual(hangup_res["status"], "completed")

        # 5. List after hangup should be empty
        list_after = adapter.list_active_calls()
        self.assertEqual(list_after["count"], 0)

        # 6. Status of finished call
        status_after = adapter.get_call_status("custom_call_1")
        self.assertFalse(status_after["ok"])
        self.assertEqual(status_after["error"], ERR_UNREACHABLE_NUMBER)

    def test_hangup_missing_id(self) -> None:
        """Hangup без ID возвращает ERR_MISSING_CALL_ID."""
        adapter = LocalSIPAdapter(server="sip.local", user="100")
        res = adapter.hangup("")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], ERR_MISSING_CALL_ID)

    def test_factory_integration(self) -> None:
        """CallProviderFactory корректно отдаёт LocalSIPAdapter при CALL_PROVIDER=sip_local."""
        mock_settings = MagicMock()
        mock_settings.CALL_PROVIDER = "sip_local"
        mock_settings.SIP_SERVER = "pbx.office.lan"
        mock_settings.SIP_PORT = 5060
        mock_settings.SIP_USER = "agent_01"
        mock_settings.SIP_PASSWORD = "pass"
        mock_settings.SIP_FROM_NUMBER = "+15559998877"
        mock_settings.SIP_PROXY = ""

        provider = get_provider(mock_settings)
        self.assertIsInstance(provider, LocalSIPAdapter)
        self.assertTrue(provider.is_configured())


if __name__ == "__main__":
    unittest.main()
