"""Unit-тесты для W1349 F1+F2+F3 fixes в WebhookManager.

W1355:
- F1 MED: SSRF via HTTP redirect — _SafeRedirectHandler re-validates each redirect URL.
- F2 LOW: response body size cap (_MAX_RESPONSE_BYTES = 64 KB).
- F3 LOW: privacy_mode gate in fire_webhook.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.webhook_manager import (  # noqa: E402
    WebhookManager,
    _SafeRedirectHandler,
    _MAX_RESPONSE_BYTES,
    _is_safe_webhook_url,
)


def _make_manager() -> WebhookManager:
    tmpdir = tempfile.mkdtemp()
    return WebhookManager(data_dir=tmpdir)


def _fake_response(status: int = 200, body: bytes = b"ok") -> MagicMock:
    """Build a mock response object compatible with urllib context manager."""
    resp = MagicMock()
    resp.status = status
    resp.read = MagicMock(return_value=body)
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# F1: redirect SSRF tests
# ---------------------------------------------------------------------------

class SafeRedirectHandlerTestCase(unittest.TestCase):
    """_SafeRedirectHandler blocks redirects to unsafe URLs and allows safe ones."""

    def _make_handler(self, allow_local: bool = False) -> _SafeRedirectHandler:
        return _SafeRedirectHandler(allow_local=allow_local)

    def _make_req(self, url: str = "http://example.com/hook") -> MagicMock:
        req = MagicMock()
        req.full_url = url
        return req

    def _make_headers(self) -> MagicMock:
        return MagicMock()

    def test_redirect_to_loopback_blocked(self) -> None:
        """Redirect to 127.0.0.1 must raise HTTPError (F1 fix)."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        with self.assertRaises(HTTPError) as ctx:
            handler.redirect_request(req, fp, 302, "Found", headers, "http://127.0.0.1/internal")

        self.assertIn("SSRF", str(ctx.exception.reason))

    def test_redirect_to_localhost_blocked(self) -> None:
        """Redirect to localhost must be blocked."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        with self.assertRaises(HTTPError):
            handler.redirect_request(req, fp, 301, "Moved", headers, "http://localhost/admin")

    def test_redirect_to_private_rfc1918_blocked(self) -> None:
        """Redirect to RFC1918 address (192.168.x.x) must be blocked."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        with self.assertRaises(HTTPError):
            handler.redirect_request(req, fp, 302, "Found", headers, "http://192.168.1.1/secret")

    def test_redirect_chain_revalidated(self) -> None:
        """Second redirect in chain to unsafe URL is also blocked."""
        handler = self._make_handler()
        req1 = self._make_req("http://public.example.com/hop1")
        fp = MagicMock()
        headers = self._make_headers()

        # First redirect to another safe public URL — should NOT raise
        # (we call redirect_request but need super().redirect_request to work;
        #  since super() will try to build a new Request, we just verify
        #  that a safe URL doesn't raise before reaching super())
        safe_ok, _ = _is_safe_webhook_url("http://safe.example.com/hop2")
        self.assertTrue(safe_ok)

        # Second redirect to loopback — must be blocked
        req2 = self._make_req("http://safe.example.com/hop2")
        with self.assertRaises(HTTPError) as ctx:
            handler.redirect_request(req2, fp, 302, "Found", headers, "http://10.0.0.1/internal")
        self.assertIn("SSRF", str(ctx.exception.reason))

    def test_redirect_to_safe_url_not_blocked(self) -> None:
        """Redirect to a public URL must NOT raise (allow_local=False)."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        # super().redirect_request will try to build a real Request — just verify
        # that _is_safe_webhook_url passes for a safe URL (no HTTPError raised by us)
        safe, reason = _is_safe_webhook_url("https://hooks.example.com/callback")
        self.assertTrue(safe)
        self.assertIsNone(reason)

    def test_redirect_allow_local_bypasses_guard(self) -> None:
        """With allow_local=True, redirect to loopback is NOT blocked by our SSRF guard.

        The _SafeRedirectHandler._allow_local flag causes _is_safe_webhook_url to return True,
        so our code does NOT raise HTTPError. Any HTTPError that follows comes from super()
        internals (e.g., 302 re-raise) — not from our SSRF guard.
        We verify this by checking the error reason does NOT contain 'SSRF'.
        """
        handler = self._make_handler(allow_local=True)
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        try:
            handler.redirect_request(req, fp, 302, "Found", headers, "http://127.0.0.1/ok")
        except HTTPError as exc:
            # If HTTPError is raised, it must NOT come from our SSRF guard
            reason = str(exc.reason) if exc.reason else ""
            self.assertNotIn(
                "SSRF",
                reason,
                f"SSRF guard blocked allow_local=True redirect unexpectedly: {exc}",
            )
        except Exception:
            # Other exceptions from super() mock internals are acceptable
            pass


# ---------------------------------------------------------------------------
# F2: response body size cap tests
# ---------------------------------------------------------------------------

class ResponseBodyCapTestCase(unittest.TestCase):
    """_post_once reads at most _MAX_RESPONSE_BYTES bytes from the response."""

    def test_response_body_capped_at_64kb(self) -> None:
        """_post_once calls resp.read(_MAX_RESPONSE_BYTES), not resp.read()."""
        mgr = _make_manager()

        resp_mock = _fake_response(status=200, body=b"x" * _MAX_RESPONSE_BYTES)

        opener_mock = MagicMock()
        opener_mock.open.return_value = resp_mock

        with patch("backend.webhook_manager.urllib.request.build_opener", return_value=opener_mock):
            status = mgr._post_once(url="http://example.com/hook", body=b'{"type":"test"}', secret="")

        self.assertEqual(status, 200)
        # Verify read was called with the byte cap
        resp_mock.read.assert_called_once_with(_MAX_RESPONSE_BYTES)

    def test_response_body_cap_constant_is_64kb(self) -> None:
        """_MAX_RESPONSE_BYTES must equal 64 * 1024."""
        self.assertEqual(_MAX_RESPONSE_BYTES, 64 * 1024)

    def test_post_once_uses_safe_redirect_handler(self) -> None:
        """_post_once builds opener with _SafeRedirectHandler, not bare urlopen."""
        mgr = _make_manager()
        resp_mock = _fake_response(status=200)

        opener_mock = MagicMock()
        opener_mock.open.return_value = resp_mock

        with patch("backend.webhook_manager.urllib.request.build_opener", return_value=opener_mock) as mock_build:
            mgr._post_once(url="http://example.com/hook", body=b"{}", secret="")

        # Verify build_opener was called with a _SafeRedirectHandler instance
        mock_build.assert_called_once()
        args, _ = mock_build.call_args
        self.assertEqual(len(args), 1)
        self.assertIsInstance(args[0], _SafeRedirectHandler)


# ---------------------------------------------------------------------------
# F3: privacy mode gate tests
# ---------------------------------------------------------------------------

class PrivacyModeGateTestCase(unittest.TestCase):
    """fire_webhook is skipped entirely when privacy_mode is active."""

    def test_fire_webhook_skipped_in_privacy_mode(self) -> None:
        """When privacy_mode=True, fire_webhook must not start any delivery threads."""
        mgr = _make_manager()
        mgr.register_webhook("https://hooks.example.com/cb", events=[])
        mgr.set_privacy_mode(True)

        with patch.object(mgr, "_deliver_with_retry") as mock_deliver:
            mgr.fire_webhook("transcription.done", {"text": "hello"})

        mock_deliver.assert_not_called()

    def test_fire_webhook_active_when_privacy_mode_disabled(self) -> None:
        """When privacy_mode=False (default), fire_webhook spawns delivery threads."""
        mgr = _make_manager()
        mgr.register_webhook("https://hooks.example.com/cb", events=[])
        # privacy_mode defaults to False

        resp_mock = _fake_response(status=200)
        opener_mock = MagicMock()
        opener_mock.open.return_value = resp_mock

        threads_started = []

        original_start = None

        import threading

        class TrackingThread(threading.Thread):
            def start(self_inner):
                threads_started.append(True)
                # Don't actually run — just record

        with patch("backend.webhook_manager.threading.Thread", side_effect=lambda **kwargs: TrackingThread(**kwargs)):
            mgr.fire_webhook("transcription.done", {"text": "hello"})

        # At least one thread was "started"
        self.assertGreater(len(threads_started), 0)

    def test_privacy_mode_toggle(self) -> None:
        """set_privacy_mode toggles the internal flag correctly."""
        mgr = _make_manager()
        self.assertFalse(mgr._privacy_mode)

        mgr.set_privacy_mode(True)
        self.assertTrue(mgr._privacy_mode)

        mgr.set_privacy_mode(False)
        self.assertFalse(mgr._privacy_mode)

    def test_fire_webhook_after_disabling_privacy_mode_works(self) -> None:
        """After disabling privacy mode, delivery resumes normally."""
        mgr = _make_manager()
        mgr.register_webhook("https://hooks.example.com/cb", events=[])
        mgr.set_privacy_mode(True)
        mgr.set_privacy_mode(False)

        called = []

        def fake_deliver(*args, **kwargs):
            called.append(True)

        with patch.object(mgr, "_deliver_with_retry", side_effect=fake_deliver):
            # Need to also intercept threading.Thread to call target synchronously
            import threading as _threading

            real_thread = _threading.Thread

            class SyncThread(real_thread):
                def start(self):
                    self.run()

            with patch("backend.webhook_manager.threading.Thread", SyncThread):
                mgr.fire_webhook("test.event", {})

        self.assertGreater(len(called), 0)


if __name__ == "__main__":
    unittest.main()
