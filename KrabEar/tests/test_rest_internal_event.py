"""test_rest_internal_event.py — POST /internal/event контракт
(spec docs/superpowers/specs/2026-07-07-event-bridge-design.md §2.2).

Loopback-only (403) + bridge-токен (401), НЕЗАВИСИМО от
REST_API_AUTH_ENABLED/REST_API_KEY. Валидный батч -> EventBus.emit_envelope()
на каждый элемент; невалидный элемент — скип + WARN, не 500.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_internal_event.py -v
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
_rest_mod = None
try:
    import flask  # noqa: F401

    _import_engine = MagicMock()
    _import_store = MagicMock()
    _import_store.load_vocabulary.return_value = []
    _import_store.load_settings.return_value = {}
    _import_transcriber = MagicMock()

    with patch("core.engine.AudioEngine", return_value=_import_engine), \
            patch("backend.state_store.StateStore", return_value=_import_store), \
            patch("backend.transcriber.Transcriber", return_value=_import_transcriber):
        import backend.rest_server as _rest_mod

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


@unittest.skipUnless(_REST_AVAILABLE, "Flask/rest_server зависимости недоступны")
class InternalEventEndpointTestCase(unittest.TestCase):
    def setUp(self):
        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()
        _rest_mod._event_bridge_token_cache = None  # сброс module-level lazy cache между тестами
        self._token = "test-bridge-token-0123456789abcdef"

    def _post(self, events, token=None, remote_addr="127.0.0.1"):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post(
            "/internal/event",
            json={"events": events},
            headers=headers,
            environ_overrides={"REMOTE_ADDR": remote_addr},
        )

    def test_non_loopback_rejected_403(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token=self._token, remote_addr="8.8.8.8")
        self.assertEqual(resp.status_code, 403)

    def test_missing_token_file_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=None):
            resp = self._post([], token="whatever")
        self.assertEqual(resp.status_code, 401)

    def test_missing_authorization_header_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token=None)
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_rejected_401(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self._post([], token="wrong-token-value")
        self.assertEqual(resp.status_code, 401)

    def test_valid_batch_emits_envelope_per_item(self):
        captured = []
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token), \
                patch.object(_rest_mod.event_bus, "emit_envelope", side_effect=lambda e: captured.append(e)):
            resp = self._post(
                [{"type": "krab_error", "ts": "2026-07-07T00:00:00+00:00", "data": {"code": "x"}}],
                token=self._token,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["origin"], "ipc")
        self.assertEqual(captured[0]["type"], "krab_error")

    def test_malformed_item_skipped_not_500(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token), \
                patch.object(_rest_mod.event_bus, "emit_envelope") as mock_emit:
            resp = self._post(
                [{"type": "ok", "ts": "t", "data": {}}, {"type": 123, "ts": "t", "data": {}}],
                token=self._token,
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["accepted"], 1)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual(mock_emit.call_count, 1)

    def test_events_not_a_list_rejected_400(self):
        with patch("backend.rest_server._get_event_bridge_token", return_value=self._token):
            resp = self.client.post(
                "/internal/event",
                json={"events": "not-a-list"},
                headers={"Authorization": f"Bearer {self._token}"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(resp.status_code, 400)

    def test_stale_cache_rereads_file_token_and_accepts(self):
        """После dual-kickstart REST кэш может держать чужой токен; файл уже новый."""
        _rest_mod._event_bridge_token_cache = "stale-cached-token"
        with patch("backend.event_bridge.read_bridge_token", return_value=self._token), \
                patch.object(_rest_mod.event_bus, "emit_envelope"):
            resp = self._post([], token=self._token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_rest_mod._event_bridge_token_cache, self._token)

    def test_stale_cache_reread_still_rejects_wrong_bearer(self):
        _rest_mod._event_bridge_token_cache = "stale-cached-token"
        with patch("backend.event_bridge.read_bridge_token", return_value=self._token):
            resp = self._post([], token="attacker-token")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
