"""CallProvider Protocol — структурная типизация на ЖИВЫХ реализациях.

До 03.09.2026 файл проверял протокол на `TelnyxAdapter` и `TwilioAdapter`.
Волна консолидации отдала линию Voice Gateway (спека
`docs/superpowers/specs/2026-09-03-telephony-consolidation.md`), адаптеры
удалены — архив в теге `telephony-archive-2026-09-03`. Протокол остался: на нём
стоит `GatewayCallProvider`, и проверять его надо на том, что живо, иначе тест
охраняет пустоту.
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.call_provider import CallProvider  # noqa: E402
from backend.call_provider_factory import NullCallProvider  # noqa: E402
from backend.gateway_call_provider import GatewayCallProvider  # noqa: E402

_REQUIRED = ("dial", "hangup", "get_call_status", "list_active_calls", "is_configured")


class ProtocolTestCase(unittest.TestCase):
    def test_gateway_provider_satisfies_protocol(self) -> None:
        self.assertIsInstance(GatewayCallProvider(base_url="http://x", api_key="k"), CallProvider)

    def test_null_provider_satisfies_protocol(self) -> None:
        """Заглушка обязана оставаться взаимозаменяемой: её отдаёт фабрика,
        когда провайдер не настроен, и вызывающий код не должен об этом знать."""
        self.assertIsInstance(NullCallProvider(), CallProvider)

    def test_every_required_method_is_present(self) -> None:
        for impl in (GatewayCallProvider(base_url="http://x", api_key="k"), NullCallProvider()):
            for name in _REQUIRED:
                self.assertTrue(callable(getattr(impl, name, None)),
                                f"{type(impl).__name__} без метода {name}")

    def test_null_provider_refuses_instead_of_pretending(self) -> None:
        """Заглушка отвечает отказом, а не пустым успехом: молчаливое ok здесь
        означало бы «позвонили», хотя звонка не было."""
        out = NullCallProvider().dial("+34911234567")
        self.assertFalse(out.get("ok", False))
        self.assertTrue(out.get("error"), "отказ обязан называть причину")

    def test_dial_signature_accepts_protocol_arguments(self) -> None:
        sig = inspect.signature(GatewayCallProvider.dial)
        for arg in ("to_number", "call_control_id", "webhook_url"):
            self.assertIn(arg, sig.parameters, f"dial без параметра протокола {arg}")
