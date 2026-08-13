"""Unit tests: handle_list_quick_phrases gracefully handles VG offline.

Production log (2026-04-26): backend logged unhandled RuntimeError on every
panel open when Voice Gateway port 8090 was down (`Connection refused`).
Fix: handler returns `{"items": [], "status": "gateway_unavailable", ...}`
вместо raise — Swift caller остаётся работоспособным, log spam пропадает.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402


class FakeStore:
    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return {
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "",
        }


class FakeRecorder:
    is_recording = False


class FakeTranscriber:
    pass


class OfflineGateway(VoiceGatewayClient):
    """Gateway returns ok=False симулируя Connection refused."""

    def __init__(self, error_text: str = "<urlopen error [Errno 61] Connection refused>") -> None:
        self._error_text = error_text
        self.get_calls: list[dict[str, Any]] = []

    def get(self, *, voice_gateway_url: str, api_key: str, path: str) -> dict:
        self.get_calls.append({"url": voice_gateway_url, "path": path})
        return {"ok": False, "error": self._error_text}

    def post(self, *, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        return {"ok": False, "error": self._error_text}


class HealthyGateway(VoiceGatewayClient):
    """Gateway returns ok=True with a phrase list (success path)."""

    def get(self, *, voice_gateway_url: str, api_key: str, path: str) -> dict:
        return {
            "ok": True,
            "payload": {
                "items": [
                    {"id": "p1", "text": "Привет"},
                    {"id": "p2", "text": "Спасибо"},
                ]
            },
        }

    def post(self, *, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        return {"ok": True, "payload": {}}


class TestQuickPhrasesOffline(unittest.TestCase):

    def _make_service(self, gateway: VoiceGatewayClient) -> CallAssistService:
        return CallAssistService(
            store=FakeStore(),
            recorder=FakeRecorder(),
            transcriber=FakeTranscriber(),
            gateway=gateway,
        )

    # MARK: - Offline (Connection refused)

    def test_offline_returns_empty_items_no_raise(self) -> None:
        """VG offline → handler возвращает items:[] вместо raise."""
        gateway = OfflineGateway()
        svc = self._make_service(gateway)
        # Не должно raise
        result = svc.handle_list_quick_phrases({})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["status"], "gateway_unavailable")
        self.assertIn("Connection refused", result["error"])

    def test_offline_passes_default_lang_pair(self) -> None:
        """Default params — source_lang=ru, target_lang=es, category=all."""
        gateway = OfflineGateway()
        svc = self._make_service(gateway)
        svc.handle_list_quick_phrases({})
        self.assertEqual(len(gateway.get_calls), 1)
        path = gateway.get_calls[0]["path"]
        self.assertIn("source_lang=ru", path)
        self.assertIn("target_lang=es", path)
        self.assertIn("category=all", path)

    def test_offline_with_custom_params_propagates(self) -> None:
        """Кастомные params уходят в gateway path."""
        gateway = OfflineGateway()
        svc = self._make_service(gateway)
        svc.handle_list_quick_phrases({
            "source_lang": "RU",
            "target_lang": "ES",
            "category": "Greetings",
            "limit": 10,
        })
        path = gateway.get_calls[0]["path"]
        # source/target lower-cased, category lower-cased
        self.assertIn("source_lang=ru", path)
        self.assertIn("target_lang=es", path)
        self.assertIn("category=greetings", path)
        self.assertIn("limit=10", path)

    def test_offline_limit_clamped(self) -> None:
        """Limit clamped to [1, 200]; 0 фолбечится на default 30."""
        gateway = OfflineGateway()
        svc = self._make_service(gateway)
        # Слишком большой → 200 (max)
        svc.handle_list_quick_phrases({"limit": 9999})
        self.assertIn("limit=200", gateway.get_calls[-1]["path"])
        # Отрицательный → 1 (min)
        svc.handle_list_quick_phrases({"limit": -5})
        self.assertIn("limit=1", gateway.get_calls[-1]["path"])
        # 0 — falsy → fallback к default 30
        svc.handle_list_quick_phrases({"limit": 0})
        self.assertIn("limit=30", gateway.get_calls[-1]["path"])

    # MARK: - Healthy (success path не сломан)

    def test_healthy_returns_payload(self) -> None:
        """Success path: вернули payload как обычно."""
        gateway = HealthyGateway()
        svc = self._make_service(gateway)
        result = svc.handle_list_quick_phrases({})
        self.assertIn("items", result)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["id"], "p1")
        # Не должно быть status field на success path (только на error path)
        self.assertNotIn("status", result)


if __name__ == "__main__":
    unittest.main()
