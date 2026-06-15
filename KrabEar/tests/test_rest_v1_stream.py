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
        self.mock_store = MagicMock()
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": False}
        self.store_patch = patch("backend.rest_server.store", self.mock_store)
        self.store_patch.start()
        
        self.mock_live_subs = MagicMock()
        self.live_subs_patch = patch("backend.rest_server.LiveSubsService", return_value=self.mock_live_subs)
        self.live_subs_patch.start()
        
        self.auth_patch = patch("backend.rest_server._ws_check_auth", return_value=True)
        self.auth_patch.start()
        
        app.config["TESTING"] = True

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
        
        with app.test_request_context('/v1/stream'):
            ws_stream(ws)
            
        self.assertTrue(len(ws.sends) >= 2)
        resp1 = json.loads(ws.sends[0])
        self.assertEqual(resp1["type"], "final")
        self.assertEqual(resp1["text"], "Привет")

    def test_privacy_gate(self):
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": True}
        ws = MockWS([json.dumps({"type": "config"})])
        
        with app.test_request_context('/v1/stream'):
            ws_stream(ws)
            
        self.assertTrue(len(ws.sends) >= 1)
        resp = json.loads(ws.sends[0])
        self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["code"], "privacy_mode_active")

    def test_cloud_stub(self):
        ws = MockWS([json.dumps({"type": "config", "backend": "cloud"})])
        
        with app.test_request_context('/v1/stream'):
            ws_stream(ws)
            
        self.assertTrue(len(ws.sends) >= 1)
        resp = json.loads(ws.sends[0])
        self.assertEqual(resp["type"], "error")
        self.assertEqual(resp["code"], "cloud_not_implemented")

if __name__ == "__main__":
    unittest.main()
