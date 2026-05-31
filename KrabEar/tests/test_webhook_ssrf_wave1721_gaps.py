"""Regression tests for Wave 1721 SSRF gap fixes in WebhookManager.

Four gaps found by pr-review-toolkit and closed in this wave:

  Gap 1 (CGNAT not blocked):
    100.64.0.0/10 (RFC 6598 Shared Address Space / CGNAT) was not blocked —
    Python's ipaddress marks it neither private nor reserved.  Added explicit
    _CGNAT_NETWORK block in _is_ip_safe(), including the ::ffff:100.64.x.x
    IPv6-mapped form.

  Gap 2 (Uncaught exception type → guard bypass):
    _resolve_and_check_host only caught socket.gaierror, but getaddrinfo can
    raise UnicodeError (invalid IDNA hostname) or OverflowError — NOT subclasses
    of gaierror — allowing them to propagate out of the guard.  Fixed by
    catching (socket.gaierror, UnicodeError, OSError, ValueError).

  Gap 3 (Registration-time DNS fail-open):
    With strict=False, DNS failure returned (True, None) — accepting an
    unverifiable host at registration.  Fixed to fail-closed at all times;
    the guard no longer defers to fire-time for unresolvable hosts.

  Gap 4 (TOCTOU DNS rebinding — IP-pinning):
    The fire-time guard resolved the hostname (getaddrinfo #1) and validated
    it, but urllib then independently called getaddrinfo again (#2) when
    opening the TCP connection.  With TTL=0 rebinding, #2 could return
    127.0.0.1 or 169.254.169.254.  Fixed by IP-pinning: _resolve_pinned_ip
    resolves once; _PinnedHTTPHandler/_PinnedHTTPSHandler inject the validated
    IP directly into connect() so urllib never re-resolves.  The original
    hostname is preserved for HTTP Host + TLS SNI.

All tests follow fail-before / pass-after contract and are isolated.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.webhook_manager import (  # noqa: E402
    WebhookManager,
    _is_ip_safe,
    _is_safe_webhook_url,
    _resolve_and_check_host,
    _resolve_pinned_ip,
    _PinnedHTTPHandler,
    _PinnedHTTPSHandler,
    _CGNAT_NETWORK,
)


def _make_manager() -> WebhookManager:
    tmpdir = tempfile.mkdtemp()
    return WebhookManager(data_dir=tmpdir)


# =============================================================================
# Gap 1 — CGNAT 100.64.0.0/10 not blocked
# =============================================================================

class CGNATBlockTestCase(unittest.TestCase):
    """Gap 1 fix: 100.64.0.0/10 (CGNAT/RFC 6598) must be blocked."""

    # 1a — first address in CGNAT range
    def test_cgnat_first_address_blocked(self) -> None:
        safe, reason = _is_ip_safe("100.64.0.1")
        self.assertFalse(safe, "100.64.0.1 must be blocked (CGNAT)")
        self.assertIsNotNone(reason)
        self.assertIn("cgnat", reason.lower())

    # 1b — last address in CGNAT range
    def test_cgnat_last_address_blocked(self) -> None:
        safe, reason = _is_ip_safe("100.127.255.255")
        self.assertFalse(safe, "100.127.255.255 must be blocked (CGNAT)")
        self.assertIsNotNone(reason)

    # 1c — middle of CGNAT range
    def test_cgnat_mid_range_blocked(self) -> None:
        safe, reason = _is_ip_safe("100.100.50.10")
        self.assertFalse(safe, "100.100.50.10 must be blocked (CGNAT)")
        self.assertIsNotNone(reason)

    # 1d — IPv6-mapped CGNAT form must also be blocked
    def test_cgnat_ipv6_mapped_blocked(self) -> None:
        safe, reason = _is_ip_safe("::ffff:100.64.0.1")
        self.assertFalse(safe, "::ffff:100.64.0.1 must be blocked (IPv6-mapped CGNAT)")
        self.assertIsNotNone(reason)
        self.assertIn("cgnat", reason.lower())

    # 1e — address just above CGNAT range (100.128.0.0) should NOT be blocked by CGNAT rule
    # (it may be blocked by other rules, but not CGNAT specifically)
    def test_address_above_cgnat_not_cgnat_blocked(self) -> None:
        safe, reason = _is_ip_safe("100.128.0.0")
        # Whether blocked or not, the reason must NOT say "CGNAT" if blocked
        if not safe and reason:
            self.assertNotIn("cgnat", reason.lower(),
                             "100.128.0.0 is outside CGNAT range — must not be blocked as CGNAT")

    # 1f — _CGNAT_NETWORK constant is correctly defined
    def test_cgnat_network_constant(self) -> None:
        import ipaddress
        self.assertIsInstance(_CGNAT_NETWORK, ipaddress.IPv4Network)
        self.assertIn(ipaddress.ip_address("100.64.0.1"), _CGNAT_NETWORK)
        self.assertNotIn(ipaddress.ip_address("100.128.0.0"), _CGNAT_NETWORK)

    # 1g — full URL with CGNAT IP is rejected at registration
    def test_register_cgnat_url_rejected(self) -> None:
        mgr = _make_manager()
        with self.assertRaises(ValueError) as ctx:
            mgr.register_webhook("http://100.64.1.1/hook", events=[])
        self.assertIn("SSRF", str(ctx.exception))

    # 1h — _is_safe_webhook_url rejects CGNAT URL
    def test_is_safe_url_cgnat_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://100.64.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)


# =============================================================================
# Gap 2 — Uncaught exception types (UnicodeError, OverflowError) from getaddrinfo
# =============================================================================

class ResolverExceptionCoverageTestCase(unittest.TestCase):
    """Gap 2 fix: any resolver exception must fail-closed, not propagate."""

    # 2a — UnicodeError for invalid IDNA hostname must fail-closed (strict=True)
    def test_unicode_error_hostname_strict_failclosed(self) -> None:
        safe, reason = _resolve_and_check_host("host\x80invalid", strict=True)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 2b — UnicodeError for invalid IDNA hostname must fail-closed (strict=False)
    def test_unicode_error_hostname_nonstrict_failclosed(self) -> None:
        safe, reason = _resolve_and_check_host("host\x80invalid", strict=False)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 2c — guard must NOT propagate UnicodeError as an unhandled exception
    def test_unicode_error_does_not_propagate(self) -> None:
        """_resolve_and_check_host must return (False, reason) — never raise."""
        try:
            result = _resolve_and_check_host("host\x80invalid", strict=True)
            self.assertIsInstance(result, tuple)
            self.assertFalse(result[0])
        except UnicodeError:
            self.fail("_resolve_and_check_host propagated UnicodeError — guard bypassed")

    # 2d — OSError (broader than gaierror) from getaddrinfo must fail-closed
    def test_os_error_from_getaddrinfo_failclosed(self) -> None:
        with patch("backend.webhook_manager.socket.getaddrinfo", side_effect=OSError("os error")):
            safe, reason = _resolve_and_check_host("example.com", strict=True)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 2e — ValueError from getaddrinfo must fail-closed
    def test_value_error_from_getaddrinfo_failclosed(self) -> None:
        with patch("backend.webhook_manager.socket.getaddrinfo", side_effect=ValueError("bad")):
            safe, reason = _resolve_and_check_host("example.com", strict=False)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 2f — _is_safe_webhook_url with a unicode-invalid hostname must fail-closed
    def test_is_safe_url_unicode_hostname_failclosed(self) -> None:
        safe, reason = _is_safe_webhook_url("http://host\x80bad/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)


# =============================================================================
# Gap 3 — Registration-time DNS fail-open (strict=False)
# =============================================================================

class RegistrationFailClosedTestCase(unittest.TestCase):
    """Gap 3 fix: DNS failure at registration must be fail-closed (reject), not fail-open."""

    # 3a — gaierror at registration time must now fail-closed
    def test_gaierror_at_registration_failclosed(self) -> None:
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   side_effect=socket.gaierror("name not resolved")):
            safe, reason = _resolve_and_check_host("unresolvable.internal", strict=False)
        self.assertFalse(safe, "DNS failure at registration must reject (fail-closed)")
        self.assertIsNotNone(reason)

    # 3b — register_webhook must raise ValueError when host is unresolvable at registration
    def test_register_unresolvable_host_rejected(self) -> None:
        mgr = _make_manager()
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   side_effect=socket.gaierror("NXDOMAIN")):
            with self.assertRaises(ValueError) as ctx:
                mgr.register_webhook("http://totally-unresolvable-xyz.invalid/hook", events=[])
        self.assertIn("SSRF", str(ctx.exception))

    # 3c — _is_safe_webhook_url with DNS fail must reject (strict=False)
    def test_is_safe_url_dns_fail_nonstrict_rejected(self) -> None:
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   side_effect=socket.gaierror("connection refused")):
            safe, reason = _is_safe_webhook_url("http://unresolvable.internal/hook", strict=False)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 3d — legitimate public URL still resolves and is accepted (gap 3 fix must not break normal flow)
    def test_legitimate_public_url_still_accepted(self) -> None:
        # hooks.example.com does not resolve, but our gap 3 fix rejects unresolvable hosts.
        # Use a real resolvable public domain for this test.
        safe, reason = _is_safe_webhook_url("https://example.com/hook", strict=False)
        self.assertTrue(safe, f"Public domain must be accepted; reason={reason!r}")
        self.assertIsNone(reason)


# =============================================================================
# Gap 4 — TOCTOU DNS rebinding: IP-pinning approach
# =============================================================================

class IPPinningTOCTOUTestCase(unittest.TestCase):
    """Gap 4 fix: IP-pinning closes the check-vs-connect TOCTOU window.

    Design: _resolve_pinned_ip() resolves+validates ONCE; _PinnedHTTP[S]Handler
    injects the pinned IP into connect() so urllib never calls getaddrinfo again.
    """

    # 4a — _resolve_pinned_ip returns a valid IP for a real public host
    def test_resolve_pinned_ip_public_host(self) -> None:
        ip_str, family = _resolve_pinned_ip("example.com", 443, "https")
        self.assertIsInstance(ip_str, str)
        self.assertTrue(len(ip_str) > 0)
        # The returned IP must itself pass _is_ip_safe
        safe, reason = _is_ip_safe(ip_str)
        self.assertTrue(safe, f"Pinned IP {ip_str!r} must be safe; reason={reason!r}")

    # 4b — _resolve_pinned_ip rejects a CGNAT IP directly
    def test_resolve_pinned_ip_rejects_cgnat(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _resolve_pinned_ip("100.64.0.1", 80, "http")
        self.assertIn("blocked", str(ctx.exception).lower())

    # 4c — _resolve_pinned_ip rejects a loopback IP directly
    def test_resolve_pinned_ip_rejects_loopback(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _resolve_pinned_ip("127.0.0.1", 80, "http")
        self.assertIn("blocked", str(ctx.exception).lower())

    # 4d — _resolve_pinned_ip raises on any resolution error (fail-closed)
    def test_resolve_pinned_ip_failclosed_on_dns_error(self) -> None:
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   side_effect=socket.gaierror("NXDOMAIN")):
            with self.assertRaises(ValueError) as ctx:
                _resolve_pinned_ip("unresolvable.internal", 443, "https")
        self.assertIn("DNS resolution failed", str(ctx.exception))

    # 4e — _post_once connects to pinned IP even when a second getaddrinfo call would differ
    # (simulates TTL=0 DNS rebinding: first call → public IP, second call → 127.0.0.1)
    def test_post_once_uses_pinned_ip_not_reresolved(self) -> None:
        """The key TOCTOU test: mock getaddrinfo so the second call returns 127.0.0.1.

        Before the fix: urllib would call getaddrinfo again when opening the connection,
        and the second call could return a private IP.
        After the fix: the pinned handler uses the first (validated) IP for connect();
        getaddrinfo is never called a second time by urllib.
        """
        # First call: returns a safe public IP (93.184.216.34 = example.com)
        # Second call: returns loopback (simulates rebinding)
        call_count = [0]
        public_ip = "93.184.216.34"
        rebind_ip = "127.0.0.1"

        def mock_getaddrinfo(host, port, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First resolution: safe public IP
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                         (public_ip, port or 80))]
            else:
                # Subsequent resolutions: rebind to loopback
                return [(socket.AF_INET, socket.SOCK_STREAM, 0, "",
                         (rebind_ip, port or 80))]

        mgr = _make_manager()
        connections_made_to = []

        class TrackingHTTPHandler(_PinnedHTTPHandler):
            def http_open(self, req):
                connections_made_to.append(self._pinned_ip)
                raise OSError("connection deliberately aborted in test")

        # Patch getaddrinfo and inject our tracking handler
        with patch("backend.webhook_manager.socket.getaddrinfo", side_effect=mock_getaddrinfo):
            with patch("backend.webhook_manager._PinnedHTTPHandler",
                       side_effect=lambda ip: TrackingHTTPHandler(ip)):
                try:
                    mgr._post_once(url="http://attacker.example.com/hook",
                                   body=b"{}", secret="", allow_local=False)
                except (OSError, ValueError):
                    pass  # Expected: our mock aborts the connection

        # getaddrinfo must have been called exactly ONCE (by _resolve_pinned_ip)
        # urllib must NOT have called it a second time
        self.assertEqual(call_count[0], 1,
                         f"getaddrinfo called {call_count[0]} times — expected 1 (IP-pinning broken)")

        # The connection attempt must have gone to the FIRST (validated) IP, not the rebind target
        if connections_made_to:
            self.assertEqual(connections_made_to[0], public_ip,
                             f"Connected to {connections_made_to[0]!r} instead of pinned {public_ip!r}")
            self.assertNotEqual(connections_made_to[0], rebind_ip,
                                "Connection went to rebind IP — TOCTOU not closed")

    # 4f — _PinnedHTTPHandler is created with the pinned IP from _resolve_pinned_ip
    def test_post_once_injects_pinned_http_handler(self) -> None:
        """_post_once must build a _PinnedHTTPHandler (not a plain HTTPHandler) for http URLs."""
        pinned_handlers_created = []

        original_handler = _PinnedHTTPHandler

        class SpyHTTPHandler(original_handler):
            def __init__(self, ip):
                pinned_handlers_created.append(ip)
                super().__init__(ip)

            def http_open(self, req):
                raise OSError("abort in test")

        with patch("backend.webhook_manager._resolve_pinned_ip",
                   return_value=("93.184.216.34", 2)):  # AF_INET=2
            with patch("backend.webhook_manager._PinnedHTTPHandler", SpyHTTPHandler):
                mgr = _make_manager()
                try:
                    mgr._post_once(url="http://example.com/hook",
                                   body=b"{}", secret="", allow_local=False)
                except (OSError, ValueError):
                    pass

        self.assertTrue(len(pinned_handlers_created) > 0,
                        "_PinnedHTTPHandler must be instantiated for http URLs")
        self.assertEqual(pinned_handlers_created[0], "93.184.216.34")

    # 4g — allow_local=True skips IP-pinning entirely (dev mode)
    def test_post_once_allow_local_skips_pinning(self) -> None:
        """allow_local=True must bypass _resolve_pinned_ip entirely."""
        pinned_called = [False]

        def spy_resolve(*args, **kwargs):
            pinned_called[0] = True
            return ("127.0.0.1", 2)

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.return_value = b""
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: False

        with patch("backend.webhook_manager._resolve_pinned_ip", side_effect=spy_resolve):
            with patch("backend.webhook_manager.urllib.request.build_opener") as mock_opener:
                mock_opener.return_value.open.return_value = fake_resp
                mgr = _make_manager()
                mgr._post_once(url="http://localhost/hook",
                               body=b"{}", secret="", allow_local=True)

        self.assertFalse(pinned_called[0],
                         "_resolve_pinned_ip must NOT be called when allow_local=True")

    # 4h — _PinnedHTTPHandler and _PinnedHTTPSHandler classes are importable and instantiable
    def test_pinned_handler_classes_exist(self) -> None:
        h = _PinnedHTTPHandler("1.2.3.4")
        self.assertEqual(h._pinned_ip, "1.2.3.4")
        hs = _PinnedHTTPSHandler("1.2.3.4")
        self.assertEqual(hs._pinned_ip, "1.2.3.4")

    # 4i — _resolve_pinned_ip raises ValueError (not propagates) when all IPs blocked
    def test_resolve_pinned_ip_raises_when_all_blocked(self) -> None:
        # Mock getaddrinfo to return only private IPs
        with patch("backend.webhook_manager.socket.getaddrinfo",
                   return_value=[(2, 1, 0, "", ("192.168.1.1", 80))]):
            with self.assertRaises(ValueError) as ctx:
                _resolve_pinned_ip("evil.internal", 80, "http")
        self.assertIn("blocked", str(ctx.exception).lower())


# =============================================================================
# Regression guard: existing good checks must still pass
# =============================================================================

class ExistingSSRFNotRegressedGapsPatchTestCase(unittest.TestCase):
    """Sanity checks: the new fixes must not break existing valid SSRF protections."""

    def test_loopback_still_blocked(self) -> None:
        safe, _ = _is_ip_safe("127.0.0.1")
        self.assertFalse(safe)

    def test_rfc1918_still_blocked(self) -> None:
        safe, _ = _is_ip_safe("192.168.1.1")
        self.assertFalse(safe)

    def test_link_local_still_blocked(self) -> None:
        safe, _ = _is_ip_safe("169.254.169.254")
        self.assertFalse(safe)

    def test_ipv6_loopback_still_blocked(self) -> None:
        safe, _ = _is_ip_safe("::1")
        self.assertFalse(safe)

    def test_public_ip_still_safe(self) -> None:
        safe, reason = _is_ip_safe("93.184.216.34")
        self.assertTrue(safe, f"Public IP must be safe; reason={reason!r}")
        self.assertIsNone(reason)

    def test_decimal_loopback_url_still_blocked(self) -> None:
        safe, reason = _is_safe_webhook_url("http://2130706433/hook")
        self.assertFalse(safe)

    def test_public_url_allow_local_still_bypasses(self) -> None:
        safe, reason = _is_safe_webhook_url("http://192.168.1.1/hook", allow_local=True)
        self.assertTrue(safe)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
