"""Unit-тесты для W1349 F1+F2+F3 fixes в WebhookManager.

W1355:
- F1 MED: SSRF via HTTP redirect — allow_redirects=False (_NoRedirectHandler blocks all 3xx).
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
    _NoRedirectHandler,
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
# F1: redirect SSRF tests (Option 1 — all redirects blocked)
# ---------------------------------------------------------------------------

class NoRedirectHandlerTestCase(unittest.TestCase):
    """_NoRedirectHandler (alias _SafeRedirectHandler) blocks ALL 3xx redirects."""

    def _make_handler(self) -> _NoRedirectHandler:
        return _NoRedirectHandler()

    def _make_req(self, url: str = "http://example.com/hook") -> MagicMock:
        req = MagicMock()
        req.full_url = url
        return req

    def _make_headers(self) -> MagicMock:
        return MagicMock()

    def test_redirect_to_loopback_blocked(self) -> None:
        """Redirect to 127.0.0.1 must raise HTTPError (allow_redirects=False)."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        with self.assertRaises(HTTPError) as ctx:
            handler.redirect_request(req, fp, 302, "Found", headers, "http://127.0.0.1/internal")

        self.assertIn("allow_redirects=False", str(ctx.exception.reason))

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

    def test_redirect_to_public_url_also_blocked(self) -> None:
        """Option 1: even redirects to public URLs are blocked (all 3xx rejected)."""
        handler = self._make_handler()
        req = self._make_req()
        fp = MagicMock()
        headers = self._make_headers()

        # All redirects are blocked — no exceptions for "safe" URLs
        with self.assertRaises(HTTPError):
            handler.redirect_request(req, fp, 302, "Found", headers, "https://safe.example.com/hop")

    def test_redirect_to_attacker_via_chain_blocked(self) -> None:
        """Second redirect in chain — also blocked (all 3xx are errors)."""
        handler = self._make_handler()
        req = self._make_req("http://safe.example.com/hop2")
        fp = MagicMock()
        headers = self._make_headers()

        with self.assertRaises(HTTPError) as ctx:
            handler.redirect_request(req, fp, 302, "Found", headers, "http://10.0.0.1/internal")
        self.assertIn("allow_redirects=False", str(ctx.exception.reason))

    def test_safe_redirect_handler_is_alias_for_no_redirect(self) -> None:
        """_SafeRedirectHandler must be the same class as _NoRedirectHandler."""
        self.assertIs(_SafeRedirectHandler, _NoRedirectHandler)


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

    def test_post_once_uses_no_redirect_handler(self) -> None:
        """_post_once builds opener with _NoRedirectHandler (allow_redirects=False).

        Gap 4 fix (W1721): _post_once now also passes a _PinnedHTTP[S]Handler to
        build_opener (to close the TOCTOU DNS-rebinding window).  The test now
        asserts that at least one of the handlers is a _NoRedirectHandler.
        """
        from backend.webhook_manager import _PinnedHTTPHandler

        mgr = _make_manager()
        resp_mock = _fake_response(status=200)

        opener_mock = MagicMock()
        opener_mock.open.return_value = resp_mock

        with patch("backend.webhook_manager.urllib.request.build_opener", return_value=opener_mock) as mock_build:
            mgr._post_once(url="http://example.com/hook", body=b"{}", secret="")

        # Verify build_opener was called with at least the _NoRedirectHandler and
        # a _PinnedHTTPHandler (IP-pinning, gap 4 fix).
        mock_build.assert_called_once()
        args, _ = mock_build.call_args
        # At least 2 handlers: NoRedirect + Pinned
        self.assertGreaterEqual(len(args), 2,
                                f"Expected >=2 handlers, got {len(args)}: {args}")
        handler_types = [type(a).__name__ for a in args]
        self.assertIn("_NoRedirectHandler", handler_types,
                      f"_NoRedirectHandler missing from {handler_types}")
        self.assertIn("_PinnedHTTPHandler", handler_types,
                      f"_PinnedHTTPHandler missing from {handler_types}")


# ---------------------------------------------------------------------------
# F3: privacy mode gate tests
# ---------------------------------------------------------------------------

class PrivacyModeGateTestCase(unittest.TestCase):
    """fire_webhook is skipped entirely when privacy_mode is active."""

    def setUp(self) -> None:
        # Track managers so we can shutdown executors in tearDown.
        # fire_webhook() uses a ThreadPoolExecutor; without explicit shutdown,
        # worker threads may linger and cause the test process to hang on ubuntu CI.
        self._managers = []

    def tearDown(self) -> None:
        for mgr in self._managers:
            try:
                mgr._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        self._managers.clear()

    def _make(self) -> object:
        """Create a manager and register it for teardown."""
        mgr = _make_manager()  # module-level function, not self._make()
        self._managers.append(mgr)
        return mgr

    def test_fire_webhook_skipped_in_privacy_mode(self) -> None:
        """When privacy_mode=True, fire_webhook must not start any delivery threads."""
        mgr = self._make()
        mgr.register_webhook("https://hooks.example.com/cb", events=[], allow_local=True)
        mgr.set_privacy_mode(True)

        with patch.object(mgr, "_deliver_with_retry") as mock_deliver:
            mgr.fire_webhook("transcription.done", {"text": "hello"})

        mock_deliver.assert_not_called()

    def test_fire_webhook_active_when_privacy_mode_disabled(self) -> None:
        """When privacy_mode=False (default), fire_webhook submits delivery tasks."""
        mgr = self._make()
        mgr.register_webhook("https://hooks.example.com/cb", events=[], allow_local=True)
        # privacy_mode defaults to False

        called = []

        def fake_deliver(*args, **kwargs):
            called.append(True)

        # fire_webhook() uses _executor.submit() (ThreadPoolExecutor), not
        # threading.Thread directly. Patch _deliver_with_retry AND run the
        # executor task synchronously by replacing submit with a direct call.
        import concurrent.futures as _cf

        def sync_submit(fn, *args, **kwargs):
            fn(*args, **kwargs)
            fut = _cf.Future()
            fut.set_result(None)
            return fut

        with patch.object(mgr, "_deliver_with_retry", side_effect=fake_deliver), \
             patch.object(mgr._executor, "submit", side_effect=sync_submit):
            mgr.fire_webhook("transcription.done", {"text": "hello"})

        # Delivery was triggered (not skipped by privacy mode)
        self.assertGreater(len(called), 0)

    def test_privacy_mode_toggle(self) -> None:
        """set_privacy_mode toggles the internal flag correctly."""
        mgr = self._make()
        self.assertFalse(mgr._privacy_mode)

        mgr.set_privacy_mode(True)
        self.assertTrue(mgr._privacy_mode)

        mgr.set_privacy_mode(False)
        self.assertFalse(mgr._privacy_mode)

    def test_fire_webhook_after_disabling_privacy_mode_works(self) -> None:
        """After disabling privacy mode, delivery resumes normally."""
        mgr = self._make()
        mgr.register_webhook("https://hooks.example.com/cb", events=[], allow_local=True)
        mgr.set_privacy_mode(True)
        mgr.set_privacy_mode(False)

        called = []

        def fake_deliver(*args, **kwargs):
            called.append(True)

        with patch.object(mgr, "_deliver_with_retry", side_effect=fake_deliver):
            # Intercept threading.Thread to call target synchronously.
            # SyncThread does NOT inherit from threading.Thread — inheriting
            # but overriding start() without calling super().start() leaves the
            # thread in _limbo (never moved to _active), causing threading._shutdown()
            # at atexit to hang indefinitely on ubuntu CI (exit code 124 = timeout).
            class SyncThread:
                def __init__(self, target=None, args=(), kwargs=None, daemon=None, **kw):
                    self._target = target
                    self._args = args
                    self._kwargs = kwargs or {}
                def start(self) -> None:
                    if self._target:
                        self._target(*self._args, **self._kwargs)
                def join(self, timeout=None) -> None:
                    pass
                def is_alive(self) -> bool:
                    return False
                daemon = True

            with patch("backend.webhook_manager.threading.Thread", SyncThread):
                mgr.fire_webhook("test.event", {})

        self.assertGreater(len(called), 0)


# ---------------------------------------------------------------------------
# Option 1 allow_redirects=False contract tests (required by W1355 spec)
# ---------------------------------------------------------------------------

class AllowRedirectsFalseTestCase(unittest.TestCase):
    """Verifies allow_redirects=False semantics: 3xx responses are treated as failures."""

    def test_webhook_redirect_3xx_not_followed(self) -> None:
        """3xx response must NOT be followed — _deliver_with_retry treats it as failure.

        Simulates the SSRF scenario: attacker registers https://attacker.com/redir
        which returns 302. The delivery must record a failure and NOT follow.
        """
        mgr = _make_manager()

        # _post_once returns 302 (NoRedirectHandler raises HTTPError 302 → caught → 302)
        with patch.object(mgr, "_post_once", return_value=302):
            with patch.object(mgr, "_record_failure") as mock_fail:
                with patch.object(mgr, "_record_success") as mock_ok:
                    # Register a webhook so we have an ID
                    mgr._webhooks["test-id"] = {
                        "url": "https://attacker.com/redir",
                        "events": [],
                        "secret": "",
                        "enabled": True,
                        "allow_local": False,
                    }
                    mgr._deliver_with_retry(
                        webhook_id="test-id",
                        url="https://attacker.com/redir",
                        secret="",
                        body=b'{"type":"test"}',
                        event_type="test.event",
                    )

        # 3xx must record failure, not success
        mock_ok.assert_not_called()
        mock_fail.assert_called_once()
        status_arg = mock_fail.call_args[0][1]
        self.assertEqual(status_arg, 302)

    def test_webhook_redirect_logs_warning_and_fails(self) -> None:
        """3xx response must emit a warning log containing SSRF or redirect mention."""
        mgr = _make_manager()
        mgr._webhooks["test-id"] = {
            "url": "https://attacker.com/redir",
            "events": [],
            "secret": "",
            "enabled": True,
            "allow_local": False,
        }

        with patch.object(mgr, "_post_once", return_value=301):
            with self.assertLogs("KrabEar.Backend.WebhookManager", level="WARNING") as log_ctx:
                mgr._deliver_with_retry(
                    webhook_id="test-id",
                    url="https://attacker.com/redir",
                    secret="",
                    body=b"{}",
                    event_type="test.event",
                )

        # At least one WARNING must mention redirect or 3xx
        warning_messages = " ".join(log_ctx.output)
        self.assertTrue(
            "redirect" in warning_messages.lower() or "3xx" in warning_messages.lower(),
            f"Expected redirect/3xx warning in logs, got: {warning_messages}",
        )

    def test_webhook_2xx_success_unchanged(self) -> None:
        """2xx responses must still be treated as successful deliveries (no regression)."""
        mgr = _make_manager()
        mgr._webhooks["test-id"] = {
            "url": "https://hooks.example.com/cb",
            "events": [],
            "secret": "",
            "enabled": True,
            "allow_local": False,
        }

        with patch.object(mgr, "_post_once", return_value=200):
            with patch.object(mgr, "_record_success") as mock_ok:
                with patch.object(mgr, "_record_failure") as mock_fail:
                    mgr._deliver_with_retry(
                        webhook_id="test-id",
                        url="https://hooks.example.com/cb",
                        secret="",
                        body=b'{"type":"test"}',
                        event_type="test.event",
                    )

        # 2xx must succeed
        mock_ok.assert_called_once()
        mock_fail.assert_not_called()
        status_arg = mock_ok.call_args[0][1]
        self.assertEqual(status_arg, 200)


if __name__ == "__main__":
    unittest.main()
