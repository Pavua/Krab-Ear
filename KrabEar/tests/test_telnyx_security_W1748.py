"""Regression tests for W1748 security fixes in TelnyxAdapter.

Covers:
  F1-W1748 — connection_id validated as numeric string before URL embedding
              (query injection hardening in list_active_calls)
  F2-W1748 — from_number validated E.164 before sending to Telnyx in dial()
  F3-W1748 — timeout already present on all HTTP calls (static regression guard)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_BACKEND_ROOT = _HERE.parent          # KrabEar/
_REPO_ROOT = _BACKEND_ROOT.parent     # repo root

for _p in [str(_BACKEND_ROOT), str(_REPO_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from backend.telnyx_adapter import (  # noqa: E402
    TelnyxAdapter,
    _CONNECTION_ID_RE,
    _is_valid_connection_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(
    api_key: str = "KEY_test_1234",
    connection_id: str = "12345678",
    from_number: str = "+15550001111",
) -> TelnyxAdapter:
    return TelnyxAdapter(
        api_key=api_key,
        connection_id=connection_id,
        from_number=from_number,
    )


def _make_mock_response(status: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = ""
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


# ===========================================================================
# F1-W1748 — connection_id validation helper
# ===========================================================================

class TestConnectionIdValidationHelper(unittest.TestCase):
    """_is_valid_connection_id helper rejects non-numeric values."""

    def test_empty_string_is_valid(self):
        """Empty connection_id = no filter applied; must be accepted."""
        self.assertTrue(_is_valid_connection_id(""))

    def test_numeric_string_valid(self):
        for v in ["1", "0", "123456789", "9" * 64]:
            with self.subTest(v=v):
                self.assertTrue(_is_valid_connection_id(v))

    def test_non_numeric_rejected(self):
        bad = [
            "abc",
            "123abc",
            "123&other_param=value",    # injection attempt
            "123 456",                  # space
            "1.2.3",                    # dots
            "../secret",                # path traversal
            "12345678901234567890" * 4,  # too long (>64 chars)
        ]
        for v in bad:
            with self.subTest(v=v):
                self.assertFalse(_is_valid_connection_id(v))

    def test_regex_constant_is_numeric_only(self):
        """Sanity: _CONNECTION_ID_RE pattern is anchored numeric."""
        import re
        self.assertIsInstance(_CONNECTION_ID_RE, re.Pattern)
        # Must reject non-numeric
        self.assertIsNone(_CONNECTION_ID_RE.match("abc"))
        self.assertIsNotNone(_CONNECTION_ID_RE.match("123456"))


# ===========================================================================
# F1-W1748 — list_active_calls rejects invalid connection_id (no HTTP call)
# ===========================================================================

class TestListActiveCallsConnectionIdGuard(unittest.TestCase):
    """list_active_calls rejects malformed connection_id before any HTTP call."""

    def _make_adapter_with_conn_id(self, conn_id: str) -> TelnyxAdapter:
        a = TelnyxAdapter(api_key="KEY_x", connection_id=conn_id, from_number="+15550001111")
        return a

    def _assert_no_http_call(self, conn_id: str) -> None:
        adapter = self._make_adapter_with_conn_id(conn_id)
        with patch.object(adapter, "_get") as mock_get:
            result = adapter.list_active_calls()
        mock_get.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_connection_id",
                         f"Expected invalid_connection_id for conn_id={conn_id!r}")

    def test_injection_string_rejected(self):
        """A connection_id with '&' must be rejected without HTTP call."""
        self._assert_no_http_call("123&evil_param=bad")

    def test_alpha_connection_id_rejected(self):
        """Letters in connection_id must be rejected."""
        self._assert_no_http_call("conn_abc")

    def test_empty_connection_id_proceeds(self):
        """Empty connection_id = no filter; list_active_calls proceeds (no guard error)."""
        adapter = self._make_adapter_with_conn_id("")
        mock_resp = _make_mock_response(200, {"data": []})
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        adapter._session = sess_mock

        result = adapter.list_active_calls()
        self.assertTrue(result["ok"])
        self.assertEqual(result["calls"], [])

    def test_valid_numeric_connection_id_proceeds(self):
        """A purely numeric connection_id proceeds to HTTP GET."""
        adapter = self._make_adapter_with_conn_id("19876543")
        mock_resp = _make_mock_response(200, {"data": []})
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        adapter._session = sess_mock

        result = adapter.list_active_calls()
        self.assertTrue(result["ok"])
        # HTTP GET must have been called
        sess_mock.get.assert_called_once()
        # The URL must contain the connection_id
        called_url = sess_mock.get.call_args[0][0]
        self.assertIn("19876543", called_url)

    def test_valid_connection_id_url_uses_urlencode(self):
        """connection_id is encoded via urlencode (no raw f-string injection)."""
        adapter = self._make_adapter_with_conn_id("55566677")
        mock_resp = _make_mock_response(200, {"data": []})
        sess_mock = MagicMock()
        sess_mock.get.return_value = mock_resp
        sess_mock.headers = {}
        adapter._session = sess_mock

        adapter.list_active_calls()
        called_url = sess_mock.get.call_args[0][0]
        # Should include properly encoded filter param
        self.assertIn("filter%5Bconnection_id%5D=55566677", called_url)

    def test_stub_mode_ignores_connection_id(self):
        """Stub mode (no api_key) short-circuits before connection_id validation."""
        adapter = TelnyxAdapter(api_key="", connection_id="bad&injection", from_number="")
        with patch.object(adapter, "_get") as mock_get:
            result = adapter.list_active_calls()
        mock_get.assert_not_called()
        self.assertEqual(result["error"], "telnyx_not_configured")


# ===========================================================================
# F2-W1748 — from_number validation in dial()
# ===========================================================================

class TestDialFromNumberValidation(unittest.TestCase):
    """dial() validates from_number (E.164) before sending to Telnyx."""

    def test_valid_from_number_proceeds(self):
        """A valid E.164 from_number proceeds to the HTTP call."""
        adapter = _make_adapter(from_number="+15550001111")
        with patch.object(adapter, "_post", return_value={
            "ok": True,
            "data": {"call_leg_id": "leg1", "call_control_id": "ctrl1"},
            "status": 201,
        }) as mock_post:
            result = adapter.dial("+15550009999")
        mock_post.assert_called_once()
        self.assertTrue(result["ok"])

    def test_invalid_from_number_rejected(self):
        """A malformed from_number must be rejected before any HTTP call."""
        adapter = _make_adapter(from_number="not_e164")
        with patch.object(adapter, "_post") as mock_post:
            result = adapter.dial("+15550009999")
        mock_post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_from_number")

    def test_from_number_no_plus_rejected(self):
        """from_number without leading '+' must be rejected."""
        adapter = _make_adapter(from_number="15550001111")
        with patch.object(adapter, "_post") as mock_post:
            result = adapter.dial("+15550009999")
        mock_post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_from_number")

    def test_empty_from_number_skips_validation(self):
        """Empty from_number is allowed (Telnyx may infer from connection_id)."""
        adapter = _make_adapter(from_number="")
        with patch.object(adapter, "_post", return_value={
            "ok": True,
            "data": {"call_leg_id": "leg2", "call_control_id": "ctrl2"},
            "status": 201,
        }) as mock_post:
            result = adapter.dial("+15550009999")
        mock_post.assert_called_once()
        self.assertTrue(result["ok"])

    def test_invalid_to_number_still_rejected_first(self):
        """to_number validation fires before from_number validation."""
        adapter = _make_adapter(from_number="bad_from")
        with patch.object(adapter, "_post") as mock_post:
            result = adapter.dial("not_a_number")
        mock_post.assert_not_called()
        # First error must be invalid_phone_number (to), not invalid_from_number
        self.assertEqual(result["error"], "invalid_phone_number")

    def test_stub_mode_skips_from_number_validation(self):
        """Stub mode (no api_key) returns telnyx_not_configured immediately."""
        adapter = TelnyxAdapter(api_key="", from_number="bad_number")
        result = adapter.dial("+15550009999")
        self.assertEqual(result["error"], "telnyx_not_configured")


# ===========================================================================
# F3-W1748 — timeout present on all HTTP methods (static AST regression guard)
# ===========================================================================

class TestTimeoutPresent(unittest.TestCase):
    """All HTTP helper methods must have a bounded timeout= argument."""

    def _load_source(self) -> str:
        src = _BACKEND_ROOT / "backend" / "telnyx_adapter.py"
        return src.read_text(encoding="utf-8")

    def _extract_method_source(self, source: str, method_name: str) -> str:
        """Very simple heuristic: extract lines from 'def _method_name' until next 'def '."""
        lines = source.splitlines()
        in_method = False
        collected: list[str] = []
        for line in lines:
            if f"def {method_name}(" in line:
                in_method = True
            elif in_method and line.strip().startswith("def "):
                break
            if in_method:
                collected.append(line)
        return "\n".join(collected)

    def _assert_method_has_timeout(self, method_name: str) -> None:
        source = self._load_source()
        method_src = self._extract_method_source(source, method_name)
        self.assertIn(
            "timeout=",
            method_src,
            f"Method {method_name!r} must have a bounded timeout= argument on its HTTP call",
        )

    def test_post_has_timeout(self):
        self._assert_method_has_timeout("_post")

    def test_get_has_timeout(self):
        self._assert_method_has_timeout("_get")

    def test_delete_has_timeout(self):
        self._assert_method_has_timeout("_delete")


# ===========================================================================
# Combined — stub mode intact after W1748 changes
# ===========================================================================

class TestStubModeIntactW1748(unittest.TestCase):
    """Stub mode still returns telnyx_not_configured for all public methods."""

    def setUp(self):
        self.stub = TelnyxAdapter(api_key="", connection_id="", from_number="")

    def test_dial_stub(self):
        result = self.stub.dial("+15550001234")
        self.assertEqual(result["error"], "telnyx_not_configured")

    def test_hangup_stub(self):
        result = self.stub.hangup("ctrl_abc")
        self.assertEqual(result["error"], "telnyx_not_configured")

    def test_get_call_status_stub(self):
        result = self.stub.get_call_status("ctrl_abc")
        self.assertEqual(result["error"], "telnyx_not_configured")

    def test_list_active_calls_stub(self):
        result = self.stub.list_active_calls()
        self.assertEqual(result["error"], "telnyx_not_configured")


if __name__ == "__main__":
    unittest.main(verbosity=2)
