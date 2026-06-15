"""Tests for /v1/stream WebSocket endpoint."""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import json
import base64
import unittest
from unittest.mock import MagicMock, patch

# Patch Sock.route before importing rest_server
def dummy_route(self, path, **kwargs):
    def decorator(f):
        return f
    return decorator

patch("flask_sock.Sock.route", new=dummy_route).start()

from backend.rest_server import app, ws_stream

class MockWS:
    def __init__(self, receives):
        self.receives = receives
        self.sends = []
        self.closed = False

    def receive(self):
        if not self.receives:
            return None
        return self.receives.pop(0)

    def send(self, msg):
        self.sends.append(msg)

    def close(self, message=None):
        self.closed = True

class TestRestV1Stream(unittest.TestCase):
    def setUp(self):
        # 🔴 Chunk-pollution guard: another test file in the same pytest chunk can
        # reload `backend.rest_server` (swapping sys.modules), which strands the
        # module-level `app`/`ws_stream` captured at collection time on the OLD
        # module object while string-target patches land on the NEW one. Re-resolve
        # the module at run time and pin every patch + handler call to that SAME
        # object so the privacy gate reads our mock store, not a stranded leftover.
        import backend.rest_server as rs
        self.rs = rs

        self.mock_store = MagicMock()
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": False}
        self.store_patch = patch.object(rs, "store", self.mock_store)
        self.store_patch.start()

        self.mock_live_subs = MagicMock()
        self.live_subs_patch = patch.object(rs, "LiveSubsService", return_value=self.mock_live_subs)
        self.live_subs_patch.start()

        self.auth_patch = patch.object(rs, "_ws_check_auth", return_value=True)
        self.auth_patch.start()

        rs.app.config["TESTING"] = True

    def tearDown(self):
        self.store_patch.stop()
        self.live_subs_patch.stop()
        self.auth_patch.stop()

    def test_v1_stream_success(self):
        self.mock_live_subs.ingest.return_value = {
            "text": "Привет",
            "language_detected": "ru"
        }

        b64 = base64.b64encode(b"\x00\x00").decode('utf-8')
        ws = MockWS([
            json.dumps({"type": "config", "mode": "transcribe", "target_lang": "ru"}),
            json.dumps({"type": "audio", "data": b64, "sample_rate": 16000, "is_final": False}),
            json.dumps({"type": "end"})
        ])

        with self.rs.app.test_request_context('/v1/stream'):
            self.rs.ws_stream(ws)

        self.assertTrue(len(ws.sends) >= 2)
        resp1 = json.loads(ws.sends[0])
        self.assertEqual(resp1["type"], "final")
        self.assertEqual(resp1["text"], "Привет")

    def test_privacy_gate(self):
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": True}
        ws = MockWS([json.dumps({"type": "config"})])

        with self.rs.app.test_request_context('/v1/stream'):
            self.rs.ws_stream(ws)

        self.assertTrue(len(ws.sends) >= 1)
        resp = json.loads(ws.sends[0])
        self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["code"], "privacy_mode_active")

    def test_cloud_unknown_provider(self):
        # New contract (Stage 2): cloud backend with unknown provider → error on flush.
        b64 = base64.b64encode(b"\x00\x00").decode('utf-8')
        ws = MockWS([
            json.dumps({"type": "config", "backend": "cloud", "provider": "nonexistent"}),
            json.dumps({"type": "audio", "data": b64, "sample_rate": 16000, "is_final": False}),
            json.dumps({"type": "end"})
        ])

        with self.rs.app.test_request_context('/v1/stream'):
            self.rs.ws_stream(ws)

        self.assertTrue(len(ws.sends) >= 1)
        resp = json.loads(ws.sends[0])
        self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["code"], "invalid_cloud_provider")

    def test_cloud_audio_buffer_cap(self):
        # Oversized cloud-audio accumulation must be rejected (local memory-DoS guard).
        big = base64.b64encode(b"\x00" * 64).decode('utf-8')
        ws = MockWS([
            json.dumps({"type": "config", "backend": "cloud", "provider": "openai"}),
            json.dumps({"type": "audio", "data": big, "sample_rate": 16000, "is_final": False}),
            json.dumps({"type": "end"})
        ])

        with patch.object(self.rs, "MAX_CLOUD_AUDIO_BYTES", 8):
            with self.rs.app.test_request_context('/v1/stream'):
                self.rs.ws_stream(ws)

        self.assertTrue(len(ws.sends) >= 1)
        resp = json.loads(ws.sends[0])
        self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["code"], "audio_too_large")


if __name__ == "__main__":
    unittest.main()
