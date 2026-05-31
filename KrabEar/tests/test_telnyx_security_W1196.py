"""Tests for W1195 security fixes in TelnyxAdapter (W1196).

Covers:
  F1 – Retry-After capped at 60 s (no unbounded sleep from malicious header)
  F2 – call_control_id validated against safe regex (path traversal blocked)
  F3 – webhook_url SSRF-checked before forwarding to Telnyx
"""

from __future__ import annotations

import ast
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap — allow running from repo root or tests/ directory
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.dirname(_HERE)   # KrabEar/
_REPO_ROOT = os.path.dirname(_BACKEND_ROOT)  # repo root

for _p in [_BACKEND_ROOT, _REPO_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from backend.telnyx_adapter import (  # noqa: E402
    TelnyxAdapter,
    _RETRY_AFTER_MAX_SEC,
    _RETRY_STATUS,
    _is_valid_call_control_id,
    _is_valid_phone,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_response(status: int, headers: dict | None = None, json_body: dict | None = None):
    """Build a minimal mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.text = ""

    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")

    return resp


def _make_adapter() -> TelnyxAdapter:
    """Return a configured TelnyxAdapter (api_key set so stub mode is off)."""
    return TelnyxAdapter(api_key="KEY_test_1234", from_number="+12025550100")


# ===========================================================================
# F1 — Retry-After capped at 60 s
# ===========================================================================

class TestRetryAfterCapped(unittest.TestCase):
    """F1: time.sleep is called with min(Retry-After, 60.0), never more."""

    def test_retry_after_capped_at_60_seconds(self):
        """A Retry-After: 9999 header should sleep for at most 60 s."""
        adapter = _make_adapter()
        resp = _make_mock_response(
            status=429,
            headers={"Retry-After": "9999"},
        )
        slept: list[float] = []
        with patch("backend.telnyx_adapter.time.sleep", side_effect=slept.append):
            result = adapter._handle_response(resp)

        self.assertEqual(len(slept), 1, "Expected exactly one sleep call")
        self.assertLessEqual(slept[0], _RETRY_AFTER_MAX_SEC,
                             "Sleep must be capped at _RETRY_AFTER_MAX_SEC")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rate_limit")
        self.assertLessEqual(result["retry_after"], _RETRY_AFTER_MAX_SEC)

    def test_retry_after_negative_treated_as_zero(self):
        """A negative Retry-After should be clamped to 0 (no sleep hang)."""
        adapter = _make_adapter()
        resp = _make_mock_response(
            status=429,
            headers={"Retry-After": "-100"},
        )
        slept: list[float] = []
        with patch("backend.telnyx_adapter.time.sleep", side_effect=slept.append):
            result = adapter._handle_response(resp)

        self.assertEqual(len(slept), 1)
        self.assertGreaterEqual(slept[0], 0.0, "Sleep must not be negative")
        self.assertLessEqual(slept[0], _RETRY_AFTER_MAX_SEC)

    def test_429_not_in_urllib3_retry_status(self):
        """429 must be removed from _RETRY_STATUS to prevent double-sleep."""
        self.assertNotIn(429, _RETRY_STATUS,
                         "429 should be removed from urllib3 retry list (F1 fix)")

    def test_retry_after_max_constant_is_60(self):
        """Sanity: _RETRY_AFTER_MAX_SEC == 60.0."""
        self.assertEqual(_RETRY_AFTER_MAX_SEC, 60.0)


# ===========================================================================
# F2 — call_control_id validation
# ===========================================================================

class TestCallControlIdValidation(unittest.TestCase):
    """F2: path traversal via call_control_id must be blocked."""

    def test_call_control_id_traversal_rejected_in_hangup(self):
        """hangup() with a path-traversal call_control_id returns error, no HTTP call."""
        adapter = _make_adapter()
        traversal_ids = [
            "../../other-resource",
            "../secret",
            "/etc/passwd",
            "foo bar",
            "a" * 129,   # too long
        ]
        for cid in traversal_ids:
            with self.subTest(cid=cid):
                with patch.object(adapter, "_post") as mock_post:
                    result = adapter.hangup(cid)

                mock_post.assert_not_called()
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "invalid_call_control_id",
                                 f"Expected invalid_call_control_id for {cid!r}")

    def test_call_control_id_traversal_rejected_in_get_call_status(self):
        """get_call_status() with a traversal id returns error, no HTTP call."""
        adapter = _make_adapter()
        with patch.object(adapter, "_get") as mock_get:
            result = adapter.get_call_status("../../other")

        mock_get.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_call_control_id")

    def test_call_control_id_valid_uuid_accepted(self):
        """A UUID-shaped call_control_id proceeds to the HTTP call."""
        adapter = _make_adapter()
        valid_id = "a1B2c3d4-e5f6-7890-abcd-ef1234567890"
        with patch.object(adapter, "_post", return_value={"ok": True}) as mock_post:
            adapter.hangup(valid_id)

        mock_post.assert_called_once()
        called_path = mock_post.call_args[0][0]
        self.assertIn(valid_id, called_path)

    def test_call_control_id_valid_alphanumeric_accepted(self):
        """Short alphanumeric IDs are accepted."""
        adapter = _make_adapter()
        for cid in ["abc123", "CALL_001", "a-b-c_1", "A" * 128]:
            with self.subTest(cid=cid):
                self.assertTrue(_is_valid_call_control_id(cid),
                                f"Expected valid: {cid!r}")

    def test_call_control_id_helper_rejects_bad_chars(self):
        """Helper function itself blocks traversal patterns."""
        bad = ["../../x", "/root", "foo bar", "", "x!y", "a" * 129]
        for cid in bad:
            with self.subTest(cid=cid):
                self.assertFalse(_is_valid_call_control_id(cid),
                                 f"Expected invalid: {cid!r}")


# ===========================================================================
# F3 — webhook_url SSRF guard
# ===========================================================================

class TestWebhookSsrfGuard(unittest.TestCase):
    """F3: webhook_url in dial() is validated via _is_safe_webhook_url."""

    def _dial_with_webhook(self, webhook_url: str) -> dict:
        adapter = _make_adapter()
        # _post should never be reached for rejected URLs
        with patch.object(adapter, "_post", return_value={"ok": True}) as mock_post:
            result = adapter.dial("+74951234567", webhook_url=webhook_url)
        return result, mock_post

    def test_webhook_url_ssrf_link_local_rejected(self):
        """169.254.x.x link-local URLs must be rejected before HTTP call."""
        result, mock_post = self._dial_with_webhook("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsafe_webhook_url")
        mock_post.assert_not_called()

    def test_webhook_url_ssrf_localhost_rejected(self):
        """localhost webhook must be rejected."""
        result, mock_post = self._dial_with_webhook("http://localhost:8080/hook")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsafe_webhook_url")
        mock_post.assert_not_called()

    def test_webhook_url_ssrf_private_ip_rejected(self):
        """RFC1918 IP webhook must be rejected."""
        result, mock_post = self._dial_with_webhook("https://10.0.0.1/hook")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsafe_webhook_url")
        mock_post.assert_not_called()

    def test_webhook_url_public_https_accepted(self):
        """A public HTTPS webhook URL passes SSRF check and proceeds to dial.

        W1759: патчим socket.getaddrinfo в webhook_manager чтобы SSRF guard
        получал публичный IP для hooks.example.com и пропускал URL.
        Тест проверяет что при безопасном webhook_url вызов dial() доходит до _post().
        """
        import socket as _sock
        # 93.184.216.34 — IP example.com (IANA), публичный адрес
        _public_addr = (_sock.AF_INET, _sock.SOCK_STREAM, 0, "", ("93.184.216.34", 443))
        adapter = _make_adapter()
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   return_value=[_public_addr]):
            with patch.object(adapter, "_post", return_value={
                "ok": True,
                "data": {"call_leg_id": "leg1", "call_control_id": "ctrl1"},
                "status": 200,
            }) as mock_post:
                result = adapter.dial("+74951234567", webhook_url="https://hooks.example.com/telnyx")

        mock_post.assert_called_once()
        # webhook_url должен быть передан в payload к Telnyx
        payload = mock_post.call_args[0][1]
        self.assertEqual(payload.get("webhook_url"), "https://hooks.example.com/telnyx")

    def test_no_webhook_url_skips_ssrf_check(self):
        """When webhook_url is None, dial succeeds without SSRF check."""
        adapter = _make_adapter()
        with patch.object(adapter, "_post", return_value={
            "ok": True,
            "data": {"call_leg_id": "leg2", "call_control_id": "ctrl2"},
            "status": 200,
        }):
            result = adapter.dial("+74951234567")

        self.assertTrue(result["ok"])


# ===========================================================================
# AST static check — ensure no bare time.sleep(retry_after) remains
# ===========================================================================

class TestStaticAnalysis(unittest.TestCase):
    """AST smoke test: verify the source no longer has unbounded sleep pattern."""

    def _load_source(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "..", "backend", "telnyx_adapter.py")
        with open(src, encoding="utf-8") as fh:
            return fh.read()

    def test_no_unbounded_sleep_on_retry_after(self):
        """Source must not contain time.sleep(retry_after) without min() cap."""
        source = self._load_source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Detect time.sleep(retry_after) — a bare variable, no min() wrapping
            func = node.func
            is_sleep = (
                isinstance(func, ast.Attribute)
                and func.attr == "sleep"
                and isinstance(func.value, ast.Name)
                and func.value.id == "time"
            )
            if not is_sleep:
                continue
            if node.args:
                arg = node.args[0]
                # Flag if argument is a plain Name (variable) that looks like raw retry_after
                if isinstance(arg, ast.Name) and arg.id == "retry_after":
                    self.fail(
                        "Found bare time.sleep(retry_after) — must be capped via min()"
                    )

    def test_retry_after_max_constant_present(self):
        """_RETRY_AFTER_MAX_SEC constant must exist in module."""
        self.assertEqual(_RETRY_AFTER_MAX_SEC, 60.0)

    def test_call_control_id_regex_present(self):
        """_CALL_CONTROL_ID_RE regex must be importable."""
        from backend.telnyx_adapter import _CALL_CONTROL_ID_RE  # noqa: F401
        import re
        self.assertIsInstance(_CALL_CONTROL_ID_RE, re.Pattern)


if __name__ == "__main__":
    unittest.main(verbosity=2)
