import json
import base64
import unittest
from unittest.mock import patch, MagicMock

import pytest

from backend.cloud_stt import get_cloud_stt_provider
from backend.rest_server import create_app, ws_stream


class TestCloudSTTProviders(unittest.TestCase):
    @patch("backend.cloud_stt.store.load_settings")
    def test_openai_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("openai")
        self.assertIsNotNone(provider)
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")
        self.assertEqual(res.get("provider"), "openai")

    @patch("backend.cloud_stt.store.load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    def test_openai_transcribe(self, mock_urlopen, mock_load_settings):
        mock_load_settings.return_value = {"openai_api_key": "test_openai_key"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"text": "hello", "language": "en"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        provider = get_cloud_stt_provider("openai")
        res = provider.transcribe(b"dummy_pcm", 16000, "auto")

        self.assertEqual(res.get("text"), "hello")
        self.assertEqual(res.get("lang"), "en")

        # Check HTTP request
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/audio/transcriptions")
        self.assertEqual(req.headers.get("Authorization"), "Bearer test_openai_key")
        self.assertIn("multipart/form-data", req.headers.get("Content-type"))

    @patch("backend.cloud_stt.store.load_settings")
    def test_deepgram_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("deepgram")
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")

    @patch("backend.cloud_stt.store.load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    def test_deepgram_transcribe(self, mock_urlopen, mock_load_settings):
        mock_load_settings.return_value = {"deepgram_api_key": "test_dg_key"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"results": {"channels": [{"alternatives": [{"transcript": "test dg", "confidence": 0.99}]}]}}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        provider = get_cloud_stt_provider("deepgram")
        res = provider.transcribe(b"dummy", 16000, "ru")

        self.assertEqual(res.get("text"), "test dg")
        self.assertEqual(res.get("confidence"), 0.99)

        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.startswith("https://api.deepgram.com/v1/listen"))
        self.assertEqual(req.headers.get("Authorization"), "Token test_dg_key")

    @patch("backend.cloud_stt.store.load_settings")
    def test_assemblyai_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")

    @patch("backend.cloud_stt.store.load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    @patch("backend.cloud_stt.time.sleep", return_value=None)
    def test_assemblyai_transcribe(self, mock_sleep, mock_urlopen, mock_load_settings):
        mock_load_settings.return_value = {"assemblyai_api_key": "test_aai_key"}

        # mock responses for upload, transcribe, poll
        mock_upload_resp = MagicMock()
        mock_upload_resp.read.return_value = b'{"upload_url": "http://test/upload"}'

        mock_transcribe_resp = MagicMock()
        mock_transcribe_resp.read.return_value = b'{"id": "test_tx_id"}'

        mock_poll_resp = MagicMock()
        mock_poll_resp.read.return_value = b'{"status": "completed", "text": "test aai", "language_code": "ru", "confidence": 0.95}'

        # urlopen is called 3 times in AssemblyAI logic
        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=mock_upload_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_transcribe_resp)),
            MagicMock(__enter__=MagicMock(return_value=mock_poll_resp)),
        ]

        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"dummy", 16000, "ru")

        self.assertEqual(res.get("text"), "test aai")
        self.assertEqual(res.get("confidence"), 0.95)


class TestV1StreamCloudIntegration(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def setup_app(self):
        self.app = create_app()
        self.app.config["TESTING"] = True

    @patch("backend.rest_server.get_cloud_stt_provider")
    @patch("backend.rest_server.store.load_settings")
    def test_v1_stream_cloud_stub_mode(self, mock_load_settings, mock_get_provider):
        mock_load_settings.return_value = {}
        mock_provider = MagicMock()
        mock_provider.transcribe.return_value = {"error": "no_api_key", "message": "missing key"}
        mock_get_provider.return_value = mock_provider

        mock_ws = MagicMock()
        config = {"type": "config", "backend": "cloud", "provider": "openai"}
        audio_msg = {
            "type": "audio",
            "data": base64.b64encode(b"dummy").decode("utf-8"),
            "sample_rate": 16000,
            "is_final": True
        }
        mock_ws.receive.side_effect = [json.dumps(config), json.dumps(audio_msg), None]

        with self.app.test_request_context('/v1/stream'):
            ws_stream(mock_ws)

        calls = mock_ws.send.call_args_list
        self.assertTrue(len(calls) > 0)
        resp = json.loads(calls[-1][0][0])
        self.assertEqual(resp.get("type"), "error")
        self.assertEqual(resp.get("code"), "cloud_no_api_key")

    @patch("backend.rest_server.get_cloud_stt_provider")
    @patch("backend.rest_server.store.load_settings")
    def test_v1_stream_cloud_success(self, mock_load_settings, mock_get_provider):
        mock_load_settings.return_value = {}
        mock_provider = MagicMock()
        mock_provider.transcribe.return_value = {"text": "hello cloud", "lang": "ru", "confidence": 0.9}
        mock_get_provider.return_value = mock_provider

        mock_ws = MagicMock()
        config = {"type": "config", "backend": "cloud", "provider": "openai"}
        audio_msg = {
            "type": "audio",
            "data": base64.b64encode(b"dummy").decode("utf-8"),
            "sample_rate": 16000,
            "is_final": False
        }
        end_msg = {"type": "end"}
        mock_ws.receive.side_effect = [json.dumps(config), json.dumps(audio_msg), json.dumps(end_msg), None]

        with self.app.test_request_context('/v1/stream'):
            ws_stream(mock_ws)

        calls = mock_ws.send.call_args_list
        self.assertTrue(len(calls) > 0)
        resp = json.loads(calls[-1][0][0])
        self.assertEqual(resp.get("type"), "final")
        self.assertEqual(resp.get("text"), "hello cloud")
        self.assertEqual(resp.get("lang"), "ru")
