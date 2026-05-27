"""Tests for W1203 security fixes in TwilioAdapter (W1208).

Covers:
  F1 HIGH  — Retry-After unbounded sleep cap at 60s
  F2 HIGH  — call_control_id (Twilio Call SID) path traversal rejection
  F3 MED   — webhook_url SSRF rejection
  F3-TW HIGH — account_sid path traversal at __init__
  F5 MED   — raw Twilio error body truncated to 512 chars
"""

from __future__ import annotations

import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRABEAR_ROOT not in sys.path:
    sys.path.insert(0, KRABEAR_ROOT)

# ---------------------------------------------------------------------------
# Import helpers — stub heavy optional deps before importing the adapter
# ---------------------------------------------------------------------------
for _mod in ("requests", "requests.adapters", "requests.auth", "urllib3", "urllib3.util",
             "urllib3.util.retry"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Ensure requests.exceptions is importable
import types as _types
if "requests.exceptions" not in sys.modules:
    _exc = _types.ModuleType("requests.exceptions")
    _exc.RequestException = Exception
    sys.modules["requests.exceptions"] = _exc
    if hasattr(sys.modules.get("requests"), "exceptions"):
        pass
    else:
        _requests_mod = sys.modules.get("requests")
        if _requests_mod is not None:
            _requests_mod.exceptions = _exc

from backend.twilio_adapter import (
    TwilioAdapter,
    _is_valid_account_sid,
    _is_valid_call_sid,
    _RETRY_AFTER_MAX_SEC,
    _RETRY_STATUS,
    _ERROR_DETAIL_MAX_CHARS,
)

# ---------------------------------------------------------------------------
# Helpers to build a minimal mock Response
# ---------------------------------------------------------------------------

def _mock_response(status: int, json_data=None, text: str = "", headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"x" if json_data or text else b""
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no JSON")
    resp.headers = headers or {}
    return resp


def _valid_account_sid() -> str:
    return "AC" + "a" * 32


def _valid_call_sid() -> str:
    return "CA" + "b" * 32


# ---------------------------------------------------------------------------
# F1 — Retry-After cap
# ---------------------------------------------------------------------------

class TestRetryAfterCapped(unittest.TestCase):
    """F1 HIGH: Retry-After sleep must be capped at 60s."""

    def _make_adapter(self):
        adapter = TwilioAdapter(account_sid=_valid_account_sid(), auth_token="tok", from_number="+1")
        return adapter

    def test_retry_after_capped_at_60s_twilio(self):
        """A huge Retry-After (e.g. 9999s) must be clamped to 60s."""
        adapter = self._make_adapter()
        resp = _mock_response(429, json_data=None, text="rate limit",
                               headers={"Retry-After": "9999"})
        with patch("backend.twilio_adapter.time.sleep") as mock_sleep:
            result = adapter._handle_response(resp)
        mock_sleep.assert_called_once()
        slept = mock_sleep.call_args[0][0]
        self.assertLessEqual(slept, 60.0, f"Sleep should be capped at 60s but was {slept}")
        self.assertEqual(result["error"], "rate_limit")
        self.assertAlmostEqual(result["retry_after"], 60.0)

    def test_retry_after_normal_value_respected(self):
        """A normal Retry-After (e.g. 5s) must be used as-is."""
        adapter = self._make_adapter()
        resp = _mock_response(429, headers={"Retry-After": "5"})
        with patch("backend.twilio_adapter.time.sleep") as mock_sleep:
            adapter._handle_response(resp)
        slept = mock_sleep.call_args[0][0]
        self.assertAlmostEqual(slept, 5.0)

    def test_retry_after_invalid_string_falls_back_to_default(self):
        """Non-numeric Retry-After falls back to _RATE_LIMIT_SLEEP_SEC (2s)."""
        from backend.twilio_adapter import _RATE_LIMIT_SLEEP_SEC
        adapter = self._make_adapter()
        resp = _mock_response(429, headers={"Retry-After": "not-a-number"})
        with patch("backend.twilio_adapter.time.sleep") as mock_sleep:
            adapter._handle_response(resp)
        slept = mock_sleep.call_args[0][0]
        self.assertAlmostEqual(slept, _RATE_LIMIT_SLEEP_SEC)

    def test_429_not_in_retry_status(self):
        """429 must NOT be in _RETRY_STATUS to prevent double-sleep with urllib3."""
        self.assertNotIn(429, _RETRY_STATUS,
                         "429 must be excluded from urllib3 _RETRY_STATUS (F1 fix)")

    def test_retry_after_max_constant_is_60(self):
        self.assertEqual(_RETRY_AFTER_MAX_SEC, 60.0)


# ---------------------------------------------------------------------------
# F2 — call_sid path traversal
# ---------------------------------------------------------------------------

class TestCallSIDTraversalRejected(unittest.TestCase):
    """F2 HIGH: hangup() and get_call_status() must reject invalid Call SIDs."""

    def _make_configured_adapter(self):
        adapter = TwilioAdapter(account_sid=_valid_account_sid(), auth_token="tok", from_number="+1")
        return adapter

    def test_call_sid_traversal_rejected(self):
        """Path traversal payloads like '../../other' must be rejected."""
        adapter = self._make_configured_adapter()
        for bad_sid in ["../../secret", "../admin", "CAXX", "invalid", " "]:
            with self.subTest(bad_sid=bad_sid):
                result = adapter.hangup(bad_sid)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "invalid_call_control_id", bad_sid)

                result2 = adapter.get_call_status(bad_sid)
                self.assertFalse(result2["ok"])
                self.assertEqual(result2["error"], "invalid_call_control_id", bad_sid)

    def test_call_sid_valid_format_accepted(self):
        """A valid Twilio Call SID (CA + 32 hex) must pass through to HTTP."""
        adapter = self._make_configured_adapter()
        valid_sid = _valid_call_sid()

        # Patch _post/_get to avoid real HTTP
        with patch.object(adapter, "_post", return_value={"ok": True}) as mock_post:
            result = adapter.hangup(valid_sid)
        mock_post.assert_called_once()
        # Should proceed (not rejected by validation)
        self.assertNotEqual(result.get("error"), "invalid_call_control_id")

        with patch.object(adapter, "_get", return_value={"ok": True, "data": {"status": "completed"}}) as mock_get:
            result2 = adapter.get_call_status(valid_sid)
        mock_get.assert_called_once()
        self.assertNotEqual(result2.get("error"), "invalid_call_control_id")

    def test_call_sid_regex(self):
        """_is_valid_call_sid verifies CA + exactly 32 hex digits."""
        self.assertTrue(_is_valid_call_sid("CA" + "0" * 32))
        self.assertTrue(_is_valid_call_sid("CA" + "aAbBcCdDeEfF" * 2 + "01234567"))
        self.assertFalse(_is_valid_call_sid(""))
        self.assertFalse(_is_valid_call_sid("CA" + "x" * 32))   # non-hex
        self.assertFalse(_is_valid_call_sid("CA" + "0" * 31))   # too short
        self.assertFalse(_is_valid_call_sid("CA" + "0" * 33))   # too long
        self.assertFalse(_is_valid_call_sid("AC" + "0" * 32))   # wrong prefix
        self.assertFalse(_is_valid_call_sid("../../secret"))


# ---------------------------------------------------------------------------
# F3 — webhook SSRF
# ---------------------------------------------------------------------------

class TestWebhookSSRFRejected(unittest.TestCase):
    """F3 MED: dial() must reject unsafe webhook_url values."""

    def _make_configured_adapter(self):
        return TwilioAdapter(account_sid=_valid_account_sid(), auth_token="tok", from_number="+1999555")

    def test_webhook_url_ssrf_rejected_twilio(self):
        """Internal/private webhook URLs must be rejected before sending to Twilio."""
        adapter = self._make_configured_adapter()
        unsafe_urls = [
            "http://localhost/callback",
            "http://127.0.0.1/hook",
            "http://169.254.169.254/meta-data",
            "http://192.168.1.1/hook",
            "http://10.0.0.1/hook",
        ]
        for url in unsafe_urls:
            with self.subTest(url=url):
                result = adapter.dial("+19991234567", webhook_url=url)
                self.assertFalse(result["ok"], f"Expected rejection for {url}")
                self.assertEqual(result["error"], "unsafe_webhook_url", f"Wrong error for {url}")

    def test_webhook_url_safe_passes_through(self):
        """A safe external https webhook_url must be forwarded to Twilio."""
        adapter = self._make_configured_adapter()
        with patch.object(adapter, "_post", return_value={"ok": True, "data": {"sid": "CA" + "c" * 32}}) as mock_post:
            result = adapter.dial("+19991234567", webhook_url="https://example.com/twilio-callback")
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        payload = call_kwargs[0][1] if call_kwargs[0] else call_kwargs[1].get("payload", {})
        self.assertIn("StatusCallback", payload)
        self.assertEqual(payload["StatusCallback"], "https://example.com/twilio-callback")


# ---------------------------------------------------------------------------
# F3-TW — account_sid path traversal at __init__
# ---------------------------------------------------------------------------

class TestAccountSIDTraversalRejectedAtInit(unittest.TestCase):
    """F3-TW HIGH: TwilioAdapter.__init__ must reject malformed account_sid."""

    def test_account_sid_traversal_rejected_at_init(self):
        """Malformed account_sid values must raise ValueError at construction."""
        bad_sids = [
            "../../etc/passwd",
            "not-a-sid",
            "AC" + "z" * 32,   # non-hex
            "AC" + "0" * 31,   # too short
            "CA" + "0" * 32,   # wrong prefix (CA not AC)
        ]
        for bad_sid in bad_sids:
            with self.subTest(bad_sid=bad_sid):
                with self.assertRaises(ValueError, msg=f"Expected ValueError for {bad_sid!r}"):
                    TwilioAdapter(account_sid=bad_sid, auth_token="tok")

    def test_empty_account_sid_allowed_stub_mode(self):
        """Empty account_sid is valid (stub mode — returns not_configured)."""
        adapter = TwilioAdapter(account_sid="", auth_token="")
        self.assertFalse(adapter.is_configured())
        result = adapter.dial("+19991234567")
        self.assertEqual(result["error"], "twilio_not_configured")

    def test_valid_account_sid_accepted(self):
        """A properly formatted Account SID (AC + 32 hex) must be accepted."""
        valid_sid = "AC" + "1234567890abcdef" * 2
        adapter = TwilioAdapter(account_sid=valid_sid, auth_token="tok")
        self.assertEqual(adapter._account_sid, valid_sid)

    def test_account_sid_regex(self):
        """_is_valid_account_sid verifies AC + exactly 32 hex digits."""
        self.assertTrue(_is_valid_account_sid("AC" + "0" * 32))
        self.assertTrue(_is_valid_account_sid("AC" + "aAbBcCdDeEfF01234567890ABCDEF012"))
        self.assertFalse(_is_valid_account_sid(""))
        self.assertFalse(_is_valid_account_sid("AC" + "x" * 32))  # non-hex
        self.assertFalse(_is_valid_account_sid("CA" + "0" * 32))  # wrong prefix
        self.assertFalse(_is_valid_account_sid("AC" + "0" * 31))  # too short
        self.assertFalse(_is_valid_account_sid("../../secret"))


# ---------------------------------------------------------------------------
# F5 — error body truncation
# ---------------------------------------------------------------------------

class TestErrorBodyTruncated(unittest.TestCase):
    """F5 MED: raw Twilio error body must be truncated to 512 chars."""

    def _make_adapter(self):
        return TwilioAdapter(account_sid=_valid_account_sid(), auth_token="tok")

    def test_error_body_truncated_to_512_chars(self):
        """A very long error message must be truncated to _ERROR_DETAIL_MAX_CHARS."""
        long_message = "A" * 1000
        adapter = self._make_adapter()
        resp = _mock_response(500, json_data={"message": long_message})
        result = adapter._handle_response(resp)
        self.assertFalse(result["ok"])
        self.assertLessEqual(
            len(result["message"]), _ERROR_DETAIL_MAX_CHARS,
            f"Error message length {len(result['message'])} exceeds {_ERROR_DETAIL_MAX_CHARS}",
        )

    def test_error_body_truncated_for_400(self):
        """400 validation errors must also be truncated."""
        adapter = self._make_adapter()
        long_msg = "B" * 2000
        resp = _mock_response(400, json_data={"message": long_msg})
        result = adapter._handle_response(resp)
        self.assertLessEqual(len(result["message"]), _ERROR_DETAIL_MAX_CHARS)

    def test_error_detail_constant_is_512(self):
        self.assertEqual(_ERROR_DETAIL_MAX_CHARS, 512)

    def test_errors_list_detail_preferred_over_message(self):
        """When errors[].detail present, it should be used instead of message."""
        adapter = self._make_adapter()
        resp = _mock_response(500, json_data={
            "message": "generic fallback message",
            "errors": [{"detail": "specific error detail from Twilio"}],
        })
        result = adapter._handle_response(resp)
        self.assertIn("specific error detail", result["message"])

    def test_plain_text_error_truncated(self):
        """Plain text (non-JSON) error body must also be truncated."""
        adapter = self._make_adapter()
        resp = _mock_response(503, text="X" * 1000)
        result = adapter._handle_response(resp)
        self.assertLessEqual(len(result["message"]), _ERROR_DETAIL_MAX_CHARS)


# ---------------------------------------------------------------------------
# AST static check
# ---------------------------------------------------------------------------

class TestASTStaticCheck(unittest.TestCase):
    """Static analysis: verify security patterns are present in source."""

    def _get_source_path(self):
        import backend.twilio_adapter as mod
        import inspect
        return inspect.getfile(mod)

    def test_ast_parses_cleanly(self):
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source)
        self.assertIsNotNone(tree)

    def test_retry_after_max_defined(self):
        """_RETRY_AFTER_MAX_SEC constant must exist in module."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_RETRY_AFTER_MAX_SEC", source)

    def test_account_sid_re_defined(self):
        """_ACCOUNT_SID_RE must exist with AC prefix regex."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_ACCOUNT_SID_RE", source)
        self.assertIn("^AC", source)

    def test_call_sid_re_defined(self):
        """_CALL_SID_RE must exist with CA prefix regex."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_CALL_SID_RE", source)
        self.assertIn("^CA", source)

    def test_ssrf_import_present(self):
        """_is_safe_webhook_url must be imported from webhook_manager."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_is_safe_webhook_url", source)

    def test_429_not_in_retry_status_source(self):
        """429 must not appear in _RETRY_STATUS definition."""
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        # The _RETRY_STATUS frozenset line must not contain 429
        for line in source.splitlines():
            if "_RETRY_STATUS" in line and "frozenset" in line:
                self.assertNotIn("429", line,
                                 "429 must be removed from _RETRY_STATUS (F1 fix)")
                break

    def test_error_detail_max_chars_defined(self):
        path = self._get_source_path()
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_ERROR_DETAIL_MAX_CHARS", source)


if __name__ == "__main__":
    unittest.main()
