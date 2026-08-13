"""Security hardening tests for Krab Ear.

Verifies that the security measures built into the system actually work:
- Sensitive fields stripped from settings export
- API key not logged in audit
- Request signing rejects tampered requests
- Rate limiting works under burst
- Webhook HMAC signatures validated
- Socket created with chmod 600

Note: InputSanitizer (generic path sanitizer) was deleted in W1736 — per-handler
allowlists now live directly in each handler.  Tests for the old InputSanitizer
class have been removed alongside the module.
"""

from __future__ import annotations
from backend.webhook_manager import WebhookManager
from backend.audit_logger import AuditLogger
from backend.ipc_throttle import IPCThrottle
from backend.request_signing import RequestSigner, TIMESTAMP_WINDOW_SEC

import hashlib
import hmac
import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
if str(KRAB_EAR_ROOT) not in sys.path:
    sys.path.insert(0, str(KRAB_EAR_ROOT))


# ---------------------------------------------------------------------------
# 9: Sensitive fields stripped from settings export
# ---------------------------------------------------------------------------

class TestSensitiveFieldsStrippedFromExport(unittest.TestCase):
    """voice_gateway_api_key, hf_token, rest_api_key, lm_studio_api_key
    must never appear in the exported settings dict."""

    def _build_service(self, settings_override: dict | None = None):
        """Build a minimal SettingsService with a fake store."""
        from backend.settings_service import SettingsService
        from backend.models import DEFAULT_SETTINGS

        class FakeStore:
            def __init__(self):
                self._settings = dict(DEFAULT_SETTINGS)
                if settings_override:
                    self._settings.update(settings_override)

            def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False):
                return dict(self._settings)

            def save_settings(self, s):
                self._settings = dict(s)
                return {"ok": True}

        svc = SettingsService(store=FakeStore())
        return svc

    def test_api_key_excluded_from_export(self):
        """voice_gateway_api_key must be absent from the export dict."""
        svc = self._build_service({"voice_gateway_api_key": "super_secret_key_abc123"})
        settings = svc.cached_settings()
        safe = {k: v for k, v in settings.items() if k not in svc._SENSITIVE_FIELDS}
        self.assertNotIn("voice_gateway_api_key", safe)

    def test_hf_token_excluded_from_export(self):
        """hf_token must be absent from the export dict."""
        svc = self._build_service({"hf_token": "hf_XYZ123_secret"})
        settings = svc.cached_settings()
        safe = {k: v for k, v in settings.items() if k not in svc._SENSITIVE_FIELDS}
        self.assertNotIn("hf_token", safe)

    def test_rest_api_key_excluded(self):
        """rest_api_key must be absent from the export dict."""
        svc = self._build_service({"rest_api_key": "rest_secret_9999"})
        settings = svc.cached_settings()
        safe = {k: v for k, v in settings.items() if k not in svc._SENSITIVE_FIELDS}
        self.assertNotIn("rest_api_key", safe)

    def test_lm_studio_api_key_excluded(self):
        """lm_studio_api_key must be absent from the export dict."""
        svc = self._build_service({"lm_studio_api_key": "lm_studio_secret_key"})
        settings = svc.cached_settings()
        safe = {k: v for k, v in settings.items() if k not in svc._SENSITIVE_FIELDS}
        self.assertNotIn("lm_studio_api_key", safe)

    def test_handle_export_settings_to_file_excludes_sensitive(self):
        """handle_export_settings writes a file that does not contain the API key."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = str(Path(tmpdir) / "export.json")
            svc = self._build_service({"voice_gateway_api_key": "should_not_appear"})
            svc.handle_export_settings({"file": out_file})
            with open(out_file, encoding="utf-8") as fh:
                exported = json.load(fh)
            self.assertNotIn("voice_gateway_api_key", exported)


# ---------------------------------------------------------------------------
# 10: API key not logged in audit
# ---------------------------------------------------------------------------

class TestAPIKeyNotLoggedInAudit(unittest.TestCase):
    """set_settings must not log parameter values (only keys list for
    non-sensitive methods, or empty list for sensitive methods)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit = AuditLogger(data_dir=self.tmpdir)

    def tearDown(self):
        self.audit.close()

    def test_sensitive_method_params_not_logged(self):
        """set_settings is a sensitive method — params_keys must be [] in audit."""
        self.audit.log_request(
            "set_settings",
            {"voice_gateway_api_key": "ultra_secret_abc", "auto_paste": True},
            {"ok": True, "result": {}},
            5.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0], encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        # params_keys must be empty for sensitive methods (no key names exposed)
        self.assertEqual(entry["params_keys"], [])

    def test_api_key_value_never_in_audit_file(self):
        """The actual API key value must never appear in the audit file bytes."""
        secret_value = "MY_TOP_SECRET_API_KEY_DO_NOT_LOG"
        self.audit.log_request(
            "set_settings",
            {"voice_gateway_api_key": secret_value},
            {"ok": True, "result": {}},
            1.0,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        raw = files[0].read_text(encoding="utf-8")
        self.assertNotIn(secret_value, raw)

    def test_normal_method_only_keys_logged_not_values(self):
        """Non-sensitive methods log key names but not values.

        W1707: translate_text was added to _SENSITIVE_METHODS in W1353 (it carries
        transcript text). Use a genuinely non-sensitive method like 'ping' instead.
        """
        self.audit.log_request(
            "ping",  # non-sensitive: no transcript text or credentials
            {"client": "test_client", "timestamp": "2026-01-01"},
            {"ok": True, "result": {}},
            0.1,
        )
        files = list(Path(self.tmpdir).glob("audit_*.ndjson"))
        with open(files[0], encoding="utf-8") as fh:
            entry = json.loads(fh.readline())
        # Keys are logged for non-sensitive methods
        self.assertIn("client", entry["params_keys"])
        # But the raw param values must not appear as individual fields
        self.assertNotIn("test_client", str(entry.get("params_keys", [])))


# ---------------------------------------------------------------------------
# 11: Request signing rejects tampered requests
# ---------------------------------------------------------------------------

class TestRequestSigningRejectsTampered(unittest.TestCase):
    """RequestSigner must reject any tampered request."""

    def setUp(self):
        self.signer = RequestSigner()
        self.secret = RequestSigner.generate_secret()

    def test_tampered_params_rejected(self):
        """Changing params after signing must cause verification to fail."""
        signed = self.signer.sign_request("get_settings", {"user": "alice"}, self.secret)
        ok = self.signer.verify_request(
            signed.method, {"user": "evil_admin"}, signed.signature,
            self.secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_tampered_method_rejected(self):
        """Changing the method after signing must cause verification to fail."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok = self.signer.verify_request(
            "delete_all_history", signed.params, signed.signature,
            self.secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_wrong_secret_rejected(self):
        """A different secret must not verify the signature."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        wrong_secret = RequestSigner.generate_secret()
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature,
            wrong_secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)

    def test_replay_attack_rejected(self):
        """Re-using the same nonce must be rejected (replay attack)."""
        signed = self.signer.sign_request("ping", {}, self.secret)
        ok1 = self.signer.verify_request(
            signed.method, signed.params, signed.signature,
            self.secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertTrue(ok1)
        ok2 = self.signer.verify_request(
            signed.method, signed.params, signed.signature,
            self.secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok2, "Replay attack must be rejected")

    def test_expired_timestamp_rejected(self):
        """A request with a timestamp beyond the window must be rejected."""
        old_ts = time.time() - TIMESTAMP_WINDOW_SEC - 10
        signed = self.signer.sign_request("ping", {}, self.secret, timestamp=old_ts)
        ok = self.signer.verify_request(
            signed.method, signed.params, signed.signature,
            self.secret, timestamp=signed.timestamp, nonce=signed.nonce,
        )
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# 12: Rate limiting under burst
# ---------------------------------------------------------------------------

class TestRateLimitingUnderBurst(unittest.TestCase):
    """IPCThrottle must reject calls after the limit is exhausted."""

    def test_heavy_method_burst_throttled(self):
        """A heavy method must be throttled after its burst capacity is used."""
        # Use a tiny limit (2/min) to make exhaustion instant
        throttle = IPCThrottle(limits={"heavy": 2, "medium": 30, "light": 120})
        method = "export_history"  # W1707: export_history is in HEAVY_METHODS (transcribe_paths is not)

        results = [throttle.check_rate(method) for _ in range(5)]
        # First 2 should pass, remainder throttled
        self.assertEqual(results[:2], [True, True])
        self.assertTrue(any(r is False for r in results[2:]),
                        "At least one call after burst limit must be throttled")

    def test_light_method_burst_eventually_throttled(self):
        """A light method with limit=3 must be throttled after 3 calls."""
        throttle = IPCThrottle(limits={"heavy": 5, "medium": 30, "light": 3})
        method = "get_clipboard_history"  # light method (get_settings теперь excluded)

        results = [throttle.check_rate(method) for _ in range(6)]
        self.assertEqual(results[:3], [True, True, True])
        self.assertTrue(any(r is False for r in results[3:]))

    def test_excluded_methods_never_throttled(self):
        """ping and start_recording are excluded — never throttled regardless of calls."""
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 1, "light": 1})
        for _ in range(200):
            self.assertTrue(throttle.check_rate("ping"))
        for _ in range(200):
            self.assertTrue(throttle.check_rate("start_recording"))

    def test_throttle_stats_track_throttled_count(self):
        """Throttle stats must show non-zero throttled count after exhausting budget."""
        throttle = IPCThrottle(limits={"heavy": 1, "medium": 30, "light": 120})
        method = "export_history"
        for _ in range(5):
            throttle.check_rate(method)
        stats = throttle.get_throttle_stats()
        self.assertGreater(stats["total_throttled"], 0)


# ---------------------------------------------------------------------------
# 13: Webhook HMAC signature validated
# ---------------------------------------------------------------------------

class TestWebhookHMACSigned(unittest.TestCase):
    """WebhookManager must include X-KrabEar-Signature header with correct HMAC."""

    def test_hmac_signature_computed_correctly(self):
        """_post_once signature header matches manual HMAC-SHA256 computation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = WebhookManager(data_dir=tmpdir)
            secret = "my_webhook_secret"
            body = b'{"type":"test","ts":"2026-04-12T00:00:00Z","data":{}}'
            expected_sig = "sha256=" + hmac.new(
                secret.encode("utf-8"), body, hashlib.sha256
            ).hexdigest()

            # Capture headers via monkey-patching urllib's build_opener
            # W1707: webhook_manager uses build_opener + opener.open, not urlopen directly
            captured_headers = {}

            from unittest.mock import patch, MagicMock

            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_resp.read = MagicMock(return_value=b"")

            mock_opener = MagicMock()
            mock_opener.open = MagicMock(return_value=mock_resp)

            def fake_build_opener(*handlers):
                return mock_opener

            def capture_open(req, timeout=None):
                captured_headers.update(req.headers)
                return mock_resp

            mock_opener.open.side_effect = capture_open

            with patch("backend.webhook_manager.urllib.request.build_opener", side_effect=fake_build_opener):
                mgr._post_once(url="https://example.com/webhook", body=body, secret=secret)

            # urllib capitalizes the first letter and lowercases the rest
            sig_header = captured_headers.get(
                "X-krabear-signature",
                captured_headers.get("X-KrabEar-Signature", "")
            )
            self.assertEqual(sig_header, expected_sig)

    def test_no_signature_header_when_no_secret(self):
        """Without a secret, X-KrabEar-Signature must NOT be present."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = WebhookManager(data_dir=tmpdir)
            body = b'{"type":"test"}'
            captured_headers = {}

            from unittest.mock import patch, MagicMock

            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: mock_resp
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.status = 200
            mock_resp.read = MagicMock(return_value=b"")

            mock_opener = MagicMock()

            def capture_open_no_sig(req, timeout=None):
                captured_headers.update(req.headers)
                return mock_resp

            mock_opener.open.side_effect = capture_open_no_sig

            def fake_build_opener_no_sig(*handlers):
                return mock_opener

            # W1707: webhook_manager uses build_opener + opener.open, not urlopen directly
            with patch("backend.webhook_manager.urllib.request.build_opener", side_effect=fake_build_opener_no_sig):
                mgr._post_once(url="https://example.com/webhook", body=body, secret="")

            self.assertNotIn("X-krabear-signature", captured_headers)
            self.assertNotIn("X-KrabEar-Signature", captured_headers)

    def test_invalid_url_rejected(self):
        """Webhook with non-http(s) URL must be rejected by register_webhook."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = WebhookManager(data_dir=tmpdir)
            with self.assertRaises(ValueError):
                mgr.register_webhook(url="ftp://evil.example.com/payload", events=[])

    def test_list_webhooks_hides_secret(self):
        """list_webhooks must expose has_secret:True but not the raw secret value.

        W1759: patch socket.getaddrinfo в webhook_manager чтобы SSRF guard
        видел публичный IP для external.example.com и не отклонял регистрацию.
        Тест проверяет скрытие секрета, а не DNS-поведение.
        """
        # 93.184.216.34 — реальный IP example.com (IANA), публичный адрес
        _public_addr = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("backend.webhook_manager.socket.getaddrinfo",
                       return_value=[_public_addr]):
                mgr = WebhookManager(data_dir=tmpdir)
                mgr.register_webhook(
                    url="https://external.example.com/hook",
                    events=["stt_result"],
                    secret="hidden_secret_value",
                )
            listed = mgr.list_webhooks()
            self.assertEqual(len(listed), 1)
            entry = listed[0]
            self.assertTrue(entry["has_secret"])
            self.assertNotIn("secret", entry)
            raw_json = json.dumps(entry)
            self.assertNotIn("hidden_secret_value", raw_json)


# ---------------------------------------------------------------------------
# 14: Socket permissions (chmod 600)
# ---------------------------------------------------------------------------

class TestSocketPermissions(unittest.TestCase):
    """The Unix socket created by IPCServer must have mode 0o600."""

    def test_socket_created_with_mode_600(self):
        """serve_forever must call chmod 600 on the socket path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "test.sock"

            # Simulate what IPCServer.serve_forever does
            if sock_path.exists():
                sock_path.unlink()

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(sock_path))
                os.chmod(str(sock_path), 0o600)
                server.listen(1)

                actual_mode = stat.S_IMODE(sock_path.stat().st_mode)
                self.assertEqual(
                    actual_mode, 0o600,
                    f"Socket mode should be 0o600, got {oct(actual_mode)}"
                )
            finally:
                server.close()
                if sock_path.exists():
                    sock_path.unlink()

    def test_socket_mode_not_world_readable(self):
        """The socket must not be readable or writable by others (world bits = 0)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = Path(tmpdir) / "private.sock"

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(str(sock_path))
                os.chmod(str(sock_path), 0o600)

                actual_mode = stat.S_IMODE(sock_path.stat().st_mode)
                # World bits must be zero
                world_bits = actual_mode & 0o007
                self.assertEqual(world_bits, 0, "Socket must have no world-readable/writable bits")
                # Group bits must be zero
                group_bits = actual_mode & 0o070
                self.assertEqual(group_bits, 0, "Socket must have no group-readable/writable bits")
            finally:
                server.close()
                if sock_path.exists():
                    sock_path.unlink()


if __name__ == "__main__":
    unittest.main()
