"""Tests for LM Studio Bearer token authentication in LLMRewriter.

Covers:
- No key → no Authorization header (backward-compat with LM Studio < 0.3)
- Key set → Authorization: Bearer <token> header present
- HTTP 401 response → pushes rewriter.unauthorized error code
- open_lm_studio_settings action handler returns hint side_effect
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure KrabEar package is importable
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, status_code: int, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


def _make_rewriter(api_key: str = ""):
    """Create an LLMRewriter with given api_key, no real network."""
    from backend.llm_rewriter import LLMRewriter
    return LLMRewriter(
        base_url="http://localhost:1234/v1",
        api_key=api_key,
        model="test-model",
        timeout_sec=5.0,
    )


class TestLmStudioHeaders(unittest.TestCase):
    """_lm_studio_headers() and _lm_studio_get_headers() header construction."""

    def test_no_key_no_authorization_header(self):
        """When api_key is empty, no Authorization header must be sent."""
        rewriter = _make_rewriter(api_key="")
        headers = rewriter._lm_studio_headers()
        self.assertNotIn("Authorization", headers)
        self.assertIn("Content-Type", headers)

    def test_key_set_authorization_header_present(self):
        """When api_key is set, Authorization: Bearer <token> must be present."""
        rewriter = _make_rewriter(api_key="lm-studio-test-token-abc123")
        headers = rewriter._lm_studio_headers()
        self.assertIn("Authorization", headers)
        self.assertEqual(headers["Authorization"], "Bearer lm-studio-test-token-abc123")

    def test_no_key_get_headers_empty(self):
        """GET headers without key must be empty dict."""
        rewriter = _make_rewriter(api_key="")
        headers = rewriter._lm_studio_get_headers()
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers, {})

    def test_key_set_get_headers_has_authorization(self):
        """GET headers with key must include Authorization."""
        rewriter = _make_rewriter(api_key="my-token")
        headers = rewriter._lm_studio_get_headers()
        self.assertEqual(headers["Authorization"], "Bearer my-token")


class TestRewrite401(unittest.TestCase):
    """HTTP 401 response handling in rewrite()."""

    def _make_rewriter_with_error_bus(self, api_key: str = ""):
        from backend.llm_rewriter import LLMRewriter
        rewriter = LLMRewriter(
            base_url="http://localhost:1234/v1",
            api_key=api_key,
            model="test-model",
            timeout_sec=5.0,
        )
        # Attach a fake error_bus
        error_bus = MagicMock()
        rewriter._error_bus = error_bus
        return rewriter, error_bus

    def test_401_response_returns_unauthorized_fallback(self):
        """A 401 response must return ok=False with fallback_reason='unauthorized'."""
        rewriter, error_bus = self._make_rewriter_with_error_bus(api_key="")
        fake_401 = FakeResponse(
            status_code=401,
            text='{"error":{"type":"invalid_request","code":"invalid_api_key"}}',
        )
        with patch.object(rewriter._session, "post", return_value=fake_401):
            result = rewriter.rewrite("test transcript text for processing")
        self.assertFalse(result.ok)
        self.assertEqual(result.fallback_reason, "unauthorized")
        self.assertIsNone(result.text)

    def test_401_response_pushes_unauthorized_error_code(self):
        """A 401 must push rewriter.unauthorized via error_bus."""
        from backend.error_bus import KrabError
        rewriter, error_bus = self._make_rewriter_with_error_bus(api_key="")
        fake_401 = FakeResponse(
            status_code=401,
            text='{"error":{"code":"invalid_api_key"}}',
        )
        with patch.object(rewriter._session, "post", return_value=fake_401):
            rewriter.rewrite("test transcript text for processing")

        # error_bus.push must have been called with a KrabError that has code=rewriter.unauthorized
        error_bus.push.assert_called_once()
        pushed = error_bus.push.call_args[0][0]
        self.assertEqual(pushed.code, "rewriter.unauthorized")
        self.assertEqual(pushed.severity, "error")

    def test_401_increments_circuit_failure(self):
        """A 401 must record a circuit failure so repeated 401s open the circuit."""
        rewriter, _ = self._make_rewriter_with_error_bus(api_key="")
        fake_401 = FakeResponse(status_code=401, text="")
        # Suppress error_bus push side effects
        with patch.object(rewriter, "_push_error"):
            with patch.object(rewriter._session, "post", return_value=fake_401):
                rewriter.rewrite("hello world")
        self.assertEqual(rewriter._circuit._consecutive_failures, 1)


class TestOpenLmStudioSettingsAction(unittest.TestCase):
    """open_lm_studio_settings action handler."""

    def test_open_lm_studio_settings_action_handler_returns_hint(self):
        """The handler must return executed=True and side_effect=swift_focus_lm_studio_api_key."""
        from backend.error_actions import handle_action
        mock_settings_service = MagicMock()
        # Suppress any subprocess.run call (opening LM Studio app)
        with patch("backend.error_actions.subprocess.run"):
            result = handle_action("open_lm_studio_settings", settings_service=mock_settings_service)
        self.assertTrue(result["executed"])
        self.assertEqual(result["side_effect"], "swift_focus_lm_studio_api_key")
        self.assertIsNone(result["reason"])

    def test_open_lm_studio_settings_in_registry(self):
        """open_lm_studio_settings must be registered in ACTION_HANDLERS."""
        from backend.error_actions import ACTION_HANDLERS
        self.assertIn("open_lm_studio_settings", ACTION_HANDLERS)

    def test_rewriter_unauthorized_in_error_registry(self):
        """rewriter.unauthorized must be present in ERROR_REGISTRY."""
        from backend.error_codes import ERROR_REGISTRY
        self.assertIn("rewriter.unauthorized", ERROR_REGISTRY)
        entry = ERROR_REGISTRY["rewriter.unauthorized"]
        self.assertEqual(entry["severity"], "error")
        self.assertTrue(entry["actionable"])
        self.assertEqual(entry["action_id"], "open_lm_studio_settings")


if __name__ == "__main__":
    unittest.main()
