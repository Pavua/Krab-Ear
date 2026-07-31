import json
import base64
import unittest
from unittest.mock import patch, MagicMock

import pytest

from backend.cloud_stt import get_cloud_stt_provider, _safe_confidence, pcm16_to_wav
from backend.rest_server import create_app, ws_stream


class TestCloudSTTProviders(unittest.TestCase):
    @patch("backend.cloud_stt._load_settings")
    def test_openai_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("openai")
        self.assertIsNotNone(provider)
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")
        self.assertEqual(res.get("provider"), "openai")

    @patch("backend.cloud_stt._load_settings")
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

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    def test_openai_language_normalised_to_iso(self, mock_urlopen, mock_load_settings):
        # OpenAI verbose_json returns "language" as a full English WORD ("russian"),
        # not an ISO-639-1 code. Downstream consumers compare lang == "ru", so the
        # provider must normalise. (External-seam contract-drift fix, 2026-06-20.)
        mock_load_settings.return_value = {"openai_api_key": "test_openai_key"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"text": "\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82", "language": "russian"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        provider = get_cloud_stt_provider("openai")
        res = provider.transcribe(b"dummy_pcm", 16000, "auto")
        self.assertEqual(res.get("lang"), "ru")  # "russian" -> "ru", not the raw word

        # Unlisted language falls back to the first 2 chars (lowercased), never the full word.
        mock_resp.read.return_value = b'{"text": "ola", "language": "Galician"}'
        res2 = provider.transcribe(b"dummy_pcm", 16000, "auto")
        self.assertEqual(res2.get("lang"), "ga")

    @patch("backend.cloud_stt._load_settings")
    def test_deepgram_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("deepgram")
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")

    @patch("backend.cloud_stt._load_settings")
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

    @patch("backend.cloud_stt._load_settings")
    def test_assemblyai_stub_mode(self, mock_load_settings):
        mock_load_settings.return_value = {}
        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"dummy", 16000, "ru")
        self.assertEqual(res.get("error"), "no_api_key")

    @patch("backend.cloud_stt._load_settings")
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


class TestCloudSTTHardening(unittest.TestCase):
    """2026-06-16 audit hardening: lang sanitization, capped reads, id validation."""

    def test_sanitize_lang_strips_injection(self):
        from backend.cloud_stt import _sanitize_lang
        evil = ('ru\r\n------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n'
                'Content-Disposition: form-data; name="prompt"\r\n\r\nx')
        self.assertEqual(_sanitize_lang(evil), "ru")        # CRLF-injection collapses to default
        self.assertEqual(_sanitize_lang("auto"), "ru")
        self.assertEqual(_sanitize_lang(""), "ru")
        self.assertEqual(_sanitize_lang("en"), "en")
        self.assertEqual(_sanitize_lang("EN"), "en")
        self.assertEqual(_sanitize_lang("en-US"), "en-us")
        self.assertEqual(_sanitize_lang("../etc/passwd"), "ru")  # junk → default

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    def test_openai_multipart_injection_blocked(self, mock_urlopen, mock_load_settings):
        mock_load_settings.return_value = {"openai_api_key": "k"}
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"text": "x", "language": "ru"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        provider = get_cloud_stt_provider("openai")
        evil = ('ru\r\n------WebKitFormBoundary7MA4YWxkTrZu0gW\r\n'
                'Content-Disposition: form-data; name="prompt"\r\n\r\nIGNORE ALL')
        provider.transcribe(b"pcm", 16000, evil)

        body = mock_urlopen.call_args[0][0].data
        # The injected field must NOT reach the multipart body; sanitizer → "ru".
        self.assertNotIn(b'name="prompt"', body)
        self.assertIn(b'name="language"\r\n\r\nru\r\n', body)

    def test_read_capped_bounds_body(self):
        from backend.cloud_stt import _read_capped

        class _FakeResp:
            def __init__(self, payload):
                self._p = payload

            def read(self, n=-1):
                return self._p[:n] if (n and n > 0) else self._p

        self.assertEqual(_read_capped(_FakeResp(b"x" * 100), limit=10), b"x" * 10)
        self.assertEqual(_read_capped(_FakeResp(b"abc"), limit=4096), b"abc")

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    @patch("backend.cloud_stt.time.sleep", return_value=None)
    def test_assemblyai_rejects_malicious_transcript_id(self, mock_sleep, mock_urlopen, mock_load_settings):
        mock_load_settings.return_value = {"assemblyai_api_key": "k"}
        up = MagicMock()
        up.read.return_value = b'{"upload_url": "http://t/u"}'
        tx = MagicMock()
        tx.read.return_value = b'{"id": "../../v2/transcript/evil?x=1"}'
        mock_urlopen.side_effect = [
            MagicMock(__enter__=MagicMock(return_value=up)),
            MagicMock(__enter__=MagicMock(return_value=tx)),
        ]
        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"pcm", 16000, "ru")
        self.assertEqual(res.get("error"), "api_error")
        self.assertIn("Invalid transcript id", res.get("message", ""))
        # Must NOT issue a 3rd (poll) request with the tampered id.
        self.assertEqual(mock_urlopen.call_count, 2)


class TestAssemblyAIPollRetry(unittest.TestCase):
    """Regression tests for the transient-poll-error retry fix.

    Before the fix the except-block inside the poll loop did
    ``return {"error": "network_error", ...}`` on the FIRST exception,
    discarding all remaining poll budget.  After the fix a single transient
    error causes ``continue`` (retry); only 3 *consecutive* failures abort.
    """

    def _make_upload_ctx(self):
        up = MagicMock()
        up.read.return_value = b'{"upload_url": "http://test/upload"}'
        return MagicMock(__enter__=MagicMock(return_value=up))

    def _make_transcribe_ctx(self, tx_id="abc123_valid-id"):
        tx = MagicMock()
        tx.read.return_value = json.dumps({"id": tx_id}).encode()
        return MagicMock(__enter__=MagicMock(return_value=tx))

    def _make_poll_ctx(self, text="hello from cloud", lang="ru", confidence=0.9):
        poll = MagicMock()
        poll.read.return_value = json.dumps({
            "status": "completed",
            "text": text,
            "language_code": lang,
            "confidence": confidence,
        }).encode()
        return MagicMock(__enter__=MagicMock(return_value=poll))

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    @patch("backend.cloud_stt.time.sleep", return_value=None)
    def test_single_transient_poll_error_retries_and_succeeds(
        self, mock_sleep, mock_urlopen, mock_load_settings
    ):
        """upload OK → transcribe OK → poll iter1 raises transient Exception
        → poll iter2 returns completed → result must contain transcript text."""
        mock_load_settings.return_value = {"assemblyai_api_key": "test-key-abc"}

        mock_urlopen.side_effect = [
            self._make_upload_ctx(),
            self._make_transcribe_ctx("txid-retry-test"),
            ConnectionResetError("TCP reset"),   # iter1 — transient, must NOT abort
            self._make_poll_ctx("retry worked"),  # iter2 — succeeds
        ]

        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"pcm_bytes", 16000, "ru")

        # The transcription must succeed — the single blip was retried.
        self.assertEqual(res.get("text"), "retry worked",
                         "Expected transcript text after transient poll error retry")
        self.assertNotIn("error", res,
                         "Result must not be an error dict after single transient poll failure")
        # Verify urlopen was called 4 times: upload + transcribe + (error) + poll-success
        self.assertEqual(mock_urlopen.call_count, 4)

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    @patch("backend.cloud_stt.time.sleep", return_value=None)
    def test_three_consecutive_poll_errors_returns_network_error(
        self, mock_sleep, mock_urlopen, mock_load_settings
    ):
        """3 consecutive poll exceptions must abort and return network_error."""
        mock_load_settings.return_value = {"assemblyai_api_key": "test-key-xyz"}

        mock_urlopen.side_effect = [
            self._make_upload_ctx(),
            self._make_transcribe_ctx("txid-cap-test"),
            OSError("DNS lookup failed"),    # consecutive error 1
            OSError("DNS lookup failed"),    # consecutive error 2
            OSError("DNS lookup failed"),    # consecutive error 3 — cap reached
        ]

        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"pcm_bytes", 16000, "ru")

        self.assertEqual(res.get("error"), "network_error",
                         "Expected network_error after 3 consecutive poll failures")
        self.assertEqual(res.get("provider"), "assemblyai")
        # 5 total calls: upload + transcribe + 3 poll errors
        self.assertEqual(mock_urlopen.call_count, 5)

    @patch("backend.cloud_stt._load_settings")
    @patch("backend.cloud_stt.urllib.request.urlopen")
    @patch("backend.cloud_stt.time.sleep", return_value=None)
    def test_error_counter_resets_on_successful_poll(
        self, mock_sleep, mock_urlopen, mock_load_settings
    ):
        """Two non-consecutive errors separated by a successful processing poll
        must NOT trigger the cap — only consecutive failures count."""
        mock_load_settings.return_value = {"assemblyai_api_key": "test-key-reset"}

        processing_poll = MagicMock()
        processing_poll.read.return_value = b'{"status": "processing"}'
        processing_ctx = MagicMock(__enter__=MagicMock(return_value=processing_poll))

        mock_urlopen.side_effect = [
            self._make_upload_ctx(),
            self._make_transcribe_ctx("txid-reset-test"),
            OSError("blip 1"),       # error #1 — consecutive count = 1
            processing_ctx,          # successful HTTP → resets consecutive counter
            OSError("blip 2"),       # error #1 again (counter reset) — must retry
            self._make_poll_ctx("counter reset works"),  # succeeds
        ]

        provider = get_cloud_stt_provider("assemblyai")
        res = provider.transcribe(b"pcm_bytes", 16000, "ru")

        self.assertEqual(res.get("text"), "counter reset works",
                         "Expected success: non-consecutive errors must not abort early")
        self.assertNotIn("error", res)


class SafeConfidenceTest(unittest.TestCase):
    """cloud-audit (2026-06-20): провайдер может вернуть confidence: null →
    float(None) ронял transcribe() + молча рвал WS. _safe_confidence терпим."""

    def test_null_returns_default(self):
        self.assertEqual(_safe_confidence(None, 0.0), 0.0)
        self.assertEqual(_safe_confidence(None, 1.0), 1.0)

    def test_valid_float_passthrough(self):
        self.assertEqual(_safe_confidence(0.85, 0.0), 0.85)

    def test_string_number_coerced(self):
        self.assertEqual(_safe_confidence("0.5", 0.0), 0.5)

    def test_nan_inf_return_default(self):
        self.assertEqual(_safe_confidence(float("nan"), 0.0), 0.0)
        self.assertEqual(_safe_confidence(float("inf"), 0.0), 0.0)

    def test_garbage_returns_default(self):
        self.assertEqual(_safe_confidence("abc", 0.3), 0.3)
        self.assertEqual(_safe_confidence({}, 0.3), 0.3)


class PcmToWavSampleRateGuardTest(unittest.TestCase):
    """cloud-audit (2026-06-20): невалидный sample_rate из WS-config ронял
    wave.setframerate → молчаливый обрыв WS. pcm16_to_wav теперь клампит."""

    _PCM = b"\x00\x00" * 100

    def test_zero_sample_rate_no_crash(self):
        wav = pcm16_to_wav(self._PCM, 0)  # раньше: wave.Error
        self.assertTrue(wav.startswith(b"RIFF"))

    def test_negative_sample_rate_no_crash(self):
        self.assertTrue(pcm16_to_wav(self._PCM, -1).startswith(b"RIFF"))

    def test_garbage_sample_rate_no_crash(self):
        self.assertTrue(pcm16_to_wav(self._PCM, "bad").startswith(b"RIFF"))  # type: ignore[arg-type]

    def test_valid_sample_rate_preserved(self):
        import io
        import wave
        with wave.open(io.BytesIO(pcm16_to_wav(self._PCM, 16000))) as w:
            self.assertEqual(w.getframerate(), 16000)

    def test_out_of_range_clamped_to_default(self):
        import io
        import wave
        with wave.open(io.BytesIO(pcm16_to_wav(self._PCM, 999999))) as w:
            self.assertEqual(w.getframerate(), 16000)
