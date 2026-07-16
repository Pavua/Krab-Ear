"""Контракт Voice Gateway ↔ Krab Ear REST (спека M-серии §1a, §4.5).

VG зовёт ровно: GET /health, POST /v1/stt/transcribe, POST /v1/tts/synthesize.
Эти тесты — стражи схемы/семантики, обязаны пережить M2/S3 без правок.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.load_settings.return_value = {}

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=MagicMock()):
        import backend.rest_server as rs
    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    rs = None


def _deps_with(privacy: bool = False, auth_enabled: bool = False):
    st = MagicMock()
    st.load_vocabulary.return_value = []
    st.load_settings.return_value = {
        "privacy_mode_enabled": privacy,
        "REST_API_AUTH_ENABLED": auth_enabled,
    }
    eng = MagicMock()
    eng.quality_profile = "balanced"
    tts = MagicMock()
    tts.handle_synthesize_speech.return_value = {
        "ok": True, "wav_bytes_b64": "UklGRg==", "language": "ru",
        "engine": "say", "byte_count": 8,
    }
    m = MagicMock()
    m.get_summary.return_value = {"total_requests": 0, "error_rate": 0, "status": "waiting_data"}
    return rs.StaticDeps(
        engine=eng, store=st, transcriber=MagicMock(), translator=MagicMock(),
        tts_service=tts, metrics=m, event_bus=MagicMock(), sse_stream=MagicMock(),
    )


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class VGHealthContractTest(unittest.TestCase):
    """preflight_call.py у VG парсит поля status и profile; httpx timeout=1s."""

    def test_health_schema(self):
        client = rs.create_app(_deps_with()).test_client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("status", body)
        self.assertIn("profile", body)
        self.assertEqual(body["status"], "ok")


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class VGPrivacyContractTest(unittest.TestCase):
    """403 + skipped:privacy_mode — VG останавливается БЕЗ fallback (их семантика)."""

    def test_tts_privacy_403(self):
        client = rs.create_app(_deps_with(privacy=True)).test_client()
        resp = client.post("/v1/tts/synthesize", json={"text": "привет"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json().get("skipped"), "privacy_mode")

    def test_stt_privacy_403(self):
        client = rs.create_app(_deps_with(privacy=True)).test_client()
        resp = client.post("/v1/stt/transcribe", data={})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json().get("skipped"), "privacy_mode")

    def test_tts_ok_without_privacy(self):
        client = rs.create_app(_deps_with()).test_client()
        resp = client.post("/v1/tts/synthesize", json={"text": "привет"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("wav_bytes_b64", resp.get_json())


@unittest.skipUnless(_REST_AVAILABLE, "REST-зависимости недоступны")
class VGAuthContractTest(unittest.TestCase):
    """401 без Authorization header, когда REST_API_AUTH_ENABLED включён.

    NOTE (расхождение с планом, проверено чтением require_api_key в
    rest_server.py): auth-гейт читает НЕ _deps()/StaticDeps, а module-level
    `settings` — синглтон `core.config.settings`, импортированный в
    rest_server.py строкой `from core.config import settings` (см. строку 30
    и `require_api_key`: `getattr(settings, "REST_API_AUTH_ENABLED", False)`).
    Поле `REST_API_AUTH_ENABLED` внутри `store.load_settings()` (которое
    заполняет `_deps_with(auth_enabled=...)` по образцу плана) auth вообще
    не проверяет — оно живёт только в privacy-related `_load_settings_field`
    путях. Реальный per-app DI (`StaticDeps`) на auth не влияет: это process-
    wide синглтон, общий для всех `create_app()`-инстансов. План допускал
    пропустить auth-пару, если нужен реальный token-store, но проверка
    "нет Authorization header" (Mode 1 в `require_api_key`) возвращает 401
    ДО обращения к `_get_rest_auth()`/token-store — токен-стор не нужен,
    поэтому тест написан через прямой `patch.object(rs.settings, ...)`
    вместо `_deps_with(auth_enabled=True)`.
    """

    def test_tts_missing_auth_header_401(self):
        client = rs.create_app(_deps_with()).test_client()
        with patch.object(rs.settings, "REST_API_AUTH_ENABLED", True):
            resp = client.post("/v1/tts/synthesize", json={"text": "привет"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
