"""Звонки Krab Ear уходят в Voice Gateway, а не в собственные адаптеры.

Волна консолидации 03.09.2026 (спека
`docs/superpowers/specs/2026-09-03-telephony-consolidation.md`). У Krab Ear своя
телефония с 24.04.2026 — 3049 строк, ни одного живого звонка; у шлюза она
полнее и работает ежедневно. Контракт согласован обеими сессиями шлюза.

`GatewayCallProvider` реализует существующий `CallProvider`, поэтому
`CallSessionService` и вызывающий код не меняются вовсе — подменяется только
то, ЧЕМ звоним.

🔴 Идентификаторы: наружу как `call_control_id` отдаётся **session_id шлюза** —
именно он адресует звонок в его REST (`/v1/telephony/calls/{session_id}/hangup`).
`call_sid` провайдера тоже возвращается, но управлять по нему нельзя.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.call_provider import CallProvider  # noqa: E402
from backend.gateway_call_provider import GatewayCallProvider  # noqa: E402


class _FakeHTTP:
    """Двойник VoiceGatewayClient: пишет вызовы, отдаёт заготовленные ответы."""

    def __init__(self, responses: dict | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._responses = responses or {}

    def post(self, voice_gateway_url, api_key, path, payload=None, timeout=None):
        self.calls.append(("POST", path, payload))
        return self._responses.get(("POST", path), {"ok": True})

    def get(self, voice_gateway_url, api_key, path, timeout=None):
        self.calls.append(("GET", path, None))
        return self._responses.get(("GET", path), {"ok": True})


def _provider(http: _FakeHTTP, url="http://127.0.0.1:8090", key="k") -> GatewayCallProvider:
    return GatewayCallProvider(base_url=url, api_key=key, http=http)


class ProtocolComplianceTest(unittest.TestCase):
    def test_satisfies_call_provider_protocol(self) -> None:
        self.assertIsInstance(_provider(_FakeHTTP()), CallProvider)


class DialTest(unittest.TestCase):
    def test_dial_calls_gateway_outbound_endpoint(self) -> None:
        http = _FakeHTTP({("POST", "/v1/telephony/calls/outbound"): {
            "ok": True, "session_id": "vs_1", "call_sid": "CA1", "status": "dialing"}})
        out = _provider(http).dial("+34911234567")
        self.assertTrue(out["ok"])
        self.assertEqual(out["call_control_id"], "vs_1", "управлять звонком можно только по session_id")
        self.assertEqual(out["call_id"], "CA1")
        method, path, payload = http.calls[0]
        self.assertEqual((method, path), ("POST", "/v1/telephony/calls/outbound"))
        self.assertEqual(payload["to"], "+34911234567")

    def test_dial_with_prompt_uses_prompt_call_endpoint(self) -> None:
        http = _FakeHTTP({("POST", "/v1/telephony/calls/prompt-call"): {
            "ok": True, "session_id": "vs_2", "call_sid": "CA2"}})
        out = _provider(http).dial("+34911234567", prompt="Уточни часы работы")
        self.assertTrue(out["ok"])
        _, path, payload = http.calls[0]
        self.assertEqual(path, "/v1/telephony/calls/prompt-call")
        self.assertEqual(payload["prompt"], "Уточни часы работы")

    def test_rejects_non_e164(self) -> None:
        http = _FakeHTTP()
        out = _provider(http).dial("8 916 000 00 00")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "invalid_phone_number")
        self.assertEqual(http.calls, [], "негодный номер не должен уходить в шлюз")

    def test_gateway_error_is_surfaced_not_swallowed(self) -> None:
        http = _FakeHTTP({("POST", "/v1/telephony/calls/outbound"): {
            "ok": False, "error": "twilio_rejected"}})
        out = _provider(http).dial("+34911234567")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "twilio_rejected")


class ControlTest(unittest.TestCase):
    def test_hangup_addresses_session(self) -> None:
        http = _FakeHTTP()
        out = _provider(http).hangup("vs_1")
        self.assertTrue(out["ok"])
        self.assertEqual(http.calls[0][1], "/v1/telephony/calls/vs_1/hangup")

    def test_hangup_requires_id(self) -> None:
        http = _FakeHTTP()
        out = _provider(http).hangup("")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "missing_call_control_id")
        self.assertEqual(http.calls, [])

    def test_status_reads_session(self) -> None:
        http = _FakeHTTP({("GET", "/v1/sessions/vs_1"): {"ok": True, "status": "running"}})
        out = _provider(http).get_call_status("vs_1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "running")

    def test_list_active_filters_finished(self) -> None:
        http = _FakeHTTP({("GET", "/v1/sessions"): {"ok": True, "items": [
            {"id": "vs_1", "status": "running"},
            {"id": "vs_2", "status": "stopped"},
        ]}})
        out = _provider(http).list_active_calls()
        self.assertTrue(out["ok"])
        self.assertEqual([c["id"] for c in out["calls"]], ["vs_1"])


class ConfigurationTest(unittest.TestCase):
    def test_not_configured_without_key(self) -> None:
        self.assertFalse(_provider(_FakeHTTP(), key="").is_configured())

    def test_dial_without_config_does_not_touch_network(self) -> None:
        http = _FakeHTTP()
        out = _provider(http, key="").dial("+34911234567")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "gateway_not_configured")
        self.assertEqual(http.calls, [])
