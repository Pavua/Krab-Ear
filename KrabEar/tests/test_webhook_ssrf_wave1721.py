"""Regression tests for Wave 1721 SSRF fixes in WebhookManager.

Covers four bugs:
  BUG 1 (HIGH): DNS rebinding — SSRF guard re-validates at fire time.
  BUG 2 (HIGH): IP notation bypass — decimal/hex/IPv6-mapped normalised.
  BUG 3 (MED):  Secrets file permissions — webhooks.json chmod 0600.
  BUG 4 (MED):  Unbounded thread creation — replaced with ThreadPoolExecutor(max=4).

All new tests follow fail-before / pass-after contract and are isolated from
each other (each uses a fresh tempdir and WebhookManager instance).
"""

from __future__ import annotations

import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.webhook_manager import (  # noqa: E402
    WebhookManager,
    _is_safe_webhook_url,
    _DELIVERY_MAX_WORKERS,
)


def _make_manager() -> WebhookManager:
    tmpdir = tempfile.mkdtemp()
    return WebhookManager(data_dir=tmpdir)


# ---------------------------------------------------------------------------
# BUG 2 — IP notation bypass (decimal / hex / IPv6-mapped)
# ---------------------------------------------------------------------------

class IPNotationBypassTestCase(unittest.TestCase):
    """BUG 2 fix: non-standard IP notations are canonicalised and rejected."""

    # 2a — decimal notation for 127.0.0.1
    def test_decimal_loopback_rejected(self) -> None:
        """http://2130706433/ (decimal 127.0.0.1) must be blocked."""
        safe, reason = _is_safe_webhook_url("http://2130706433/hook")
        self.assertFalse(safe, f"Expected rejection, got safe=True, reason={reason!r}")
        self.assertIsNotNone(reason)

    # 2b — hex notation for 127.0.0.1
    def test_hex_loopback_rejected(self) -> None:
        """http://0x7f000001/ (hex 127.0.0.1) must be blocked."""
        safe, reason = _is_safe_webhook_url("http://0x7f000001/hook")
        self.assertFalse(safe, f"Expected rejection, got safe=True, reason={reason!r}")
        self.assertIsNotNone(reason)

    # 2c — IPv6-mapped loopback
    def test_ipv6_mapped_loopback_rejected(self) -> None:
        """http://[::ffff:127.0.0.1]/ must be blocked (IPv6-mapped loopback)."""
        safe, reason = _is_safe_webhook_url("http://[::ffff:127.0.0.1]/hook")
        self.assertFalse(safe, f"Expected rejection, got safe=True, reason={reason!r}")
        self.assertIsNotNone(reason)

    # 2d — IPv6-mapped cloud metadata address (169.254.169.254)
    def test_ipv6_mapped_metadata_rejected(self) -> None:
        """http://[::ffff:169.254.169.254]/ must be blocked (IPv6-mapped link-local)."""
        safe, reason = _is_safe_webhook_url("http://[::ffff:169.254.169.254]/hook")
        self.assertFalse(safe, f"Expected rejection, got safe=True, reason={reason!r}")
        self.assertIsNotNone(reason)

    # 2e — decimal notation for 192.168.1.1 (RFC1918)
    def test_decimal_private_rejected(self) -> None:
        """http://3232235777/ (decimal 192.168.1.1) must be blocked."""
        # 192*16777216 + 168*65536 + 1*256 + 1 = 3232235777
        safe, reason = _is_safe_webhook_url("http://3232235777/hook")
        self.assertFalse(safe, f"Expected rejection, got safe=True, reason={reason!r}")
        self.assertIsNotNone(reason)

    # 2f — register_webhook rejects decimal IP notation
    def test_register_decimal_loopback_raises(self) -> None:
        """register_webhook must raise ValueError for decimal loopback notation."""
        mgr = _make_manager()
        with self.assertRaises(ValueError):
            mgr.register_webhook("http://2130706433/hook", events=[])

    # 2g — register_webhook rejects hex notation
    def test_register_hex_loopback_raises(self) -> None:
        """register_webhook must raise ValueError for hex loopback notation."""
        mgr = _make_manager()
        with self.assertRaises(ValueError):
            mgr.register_webhook("http://0x7f000001/hook", events=[])

    # 2h — register_webhook rejects IPv6-mapped metadata IP
    def test_register_ipv6_mapped_metadata_raises(self) -> None:
        """register_webhook must raise ValueError for IPv6-mapped metadata notation."""
        mgr = _make_manager()
        with self.assertRaises(ValueError):
            mgr.register_webhook("http://[::ffff:169.254.169.254]/hook", events=[])


# ---------------------------------------------------------------------------
# BUG 1 — DNS rebinding: fire-time re-validation
# ---------------------------------------------------------------------------

class DNSRebindingTestCase(unittest.TestCase):
    """BUG 1 fix: _post_once re-validates URL at fire time to block DNS rebinding."""

    def test_fire_time_ssrf_blocked_when_dns_repoints(self) -> None:
        """URL passes registration (public IP) but fails at fire time (private IP).

        Simulates DNS rebinding: attacker registers a URL that resolves to a
        public IP, then re-points DNS to 127.0.0.1 before the webhook fires.
        """
        mgr = _make_manager()

        # Registration: _is_safe_webhook_url returns True (public IP)
        with patch("backend.webhook_manager._is_safe_webhook_url", return_value=(True, None)):
            wid = mgr.register_webhook("http://attacker.example.com/hook", events=[])

        self.assertIn(wid, mgr._webhooks)

        # Fire time: _is_safe_webhook_url returns False (DNS now points to private IP)
        with patch(
            "backend.webhook_manager._is_safe_webhook_url",
            return_value=(False, "loopback IP blocked (127.0.0.1)"),
        ):
            # _post_once should raise ValueError — caught by _deliver_with_retry as exception
            with self.assertRaises(ValueError) as ctx:
                mgr._post_once(
                    url="http://attacker.example.com/hook",
                    body=b"{}",
                    secret="",
                    allow_local=False,
                )

        self.assertIn("fire-time SSRF", str(ctx.exception))

    def test_fire_time_check_skipped_when_allow_local(self) -> None:
        """When allow_local=True the fire-time check is bypassed (dev mode)."""
        import unittest.mock as mock_mod
        mgr = _make_manager()

        call_count = [0]

        def counting_safe_check(url, allow_local=False, strict=False):
            # The guard in _post_once only calls this when allow_local=False
            call_count[0] += 1
            return (True, None)

        fake_resp = mock_mod.MagicMock()
        fake_resp.status = 200
        fake_resp.read.return_value = b""
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: False

        with patch("backend.webhook_manager._is_safe_webhook_url", side_effect=counting_safe_check):
            with patch("backend.webhook_manager.urllib.request.build_opener") as mock_opener:
                mock_opener.return_value.open.return_value = fake_resp
                mgr._post_once(
                    url="http://localhost/hook",
                    body=b"{}",
                    secret="",
                    allow_local=True,
                )

        # fire-time SSRF check must be skipped when allow_local=True
        self.assertEqual(call_count[0], 0, "fire-time check must be skipped when allow_local=True")

    def test_deliver_with_retry_records_failure_on_dns_rebinding(self) -> None:
        """When fire-time check fails, _deliver_with_retry records it as a failure."""
        mgr = _make_manager()
        mgr._webhooks["test-id"] = {
            "url": "http://attacker.example.com/hook",
            "events": [],
            "secret": "",
            "enabled": True,
            "allow_local": False,
        }

        # _post_once raises ValueError (DNS rebinding caught at fire time)
        with patch.object(mgr, "_post_once", side_effect=ValueError("fire-time SSRF check failed")):
            with patch("backend.webhook_manager.time.sleep"):
                mgr._deliver_with_retry(
                    webhook_id="test-id",
                    url="http://attacker.example.com/hook",
                    secret="",
                    body=b"{}",
                    event_type="test.event",
                )

        stats = mgr._stats.get("test-id", {})
        self.assertGreater(stats.get("failures", 0), 0, "DNS rebinding attempt must be recorded as failure")
        self.assertEqual(stats.get("deliveries", 0), 0)


# ---------------------------------------------------------------------------
# BUG 3 — Secrets file permissions (0600)
# ---------------------------------------------------------------------------

class SecretsFilePermissionsTestCase(unittest.TestCase):
    """BUG 3 fix: webhooks.json is written with mode 0600."""

    def test_webhooks_json_is_mode_0600(self) -> None:
        """webhooks.json must have permissions 0600 after first save."""
        mgr = _make_manager()
        mgr.register_webhook("https://example.com/hook", events=[], secret="s3cr3t")

        mode = oct(stat.S_IMODE(mgr._webhooks_path.stat().st_mode))
        self.assertEqual(mode, "0o600", f"Expected 0600, got {mode}")

    def test_webhooks_json_stays_0600_after_multiple_saves(self) -> None:
        """Subsequent saves must not loosen permissions."""
        mgr = _make_manager()
        mgr.register_webhook("https://example.com/hook1", events=[])
        mgr.register_webhook("https://example.com/hook2", events=[])

        mode = oct(stat.S_IMODE(mgr._webhooks_path.stat().st_mode))
        self.assertEqual(mode, "0o600", f"Expected 0600 after second save, got {mode}")

    def test_webhooks_json_secrets_not_world_readable(self) -> None:
        """webhooks.json must not be group-readable or other-readable."""
        mgr = _make_manager()
        mgr.register_webhook("https://example.com/hook", events=[], secret="top-secret")

        file_stat = mgr._webhooks_path.stat()
        mode = file_stat.st_mode
        # Check no group or other read bits
        self.assertEqual(mode & stat.S_IRGRP, 0, "Group read bit must not be set")
        self.assertEqual(mode & stat.S_IROTH, 0, "Other read bit must not be set")


# ---------------------------------------------------------------------------
# BUG 4 — Bounded thread pool (ThreadPoolExecutor)
# ---------------------------------------------------------------------------

class BoundedThreadPoolTestCase(unittest.TestCase):
    """BUG 4 fix: fire_webhook uses ThreadPoolExecutor(max_workers=4)."""

    def test_executor_attribute_exists(self) -> None:
        """WebhookManager must have a _executor attribute (ThreadPoolExecutor)."""
        from concurrent.futures import ThreadPoolExecutor
        mgr = _make_manager()
        self.assertIsInstance(mgr._executor, ThreadPoolExecutor)

    def test_delivery_max_workers_is_four(self) -> None:
        """_DELIVERY_MAX_WORKERS constant must equal 4."""
        self.assertEqual(_DELIVERY_MAX_WORKERS, 4)

    def test_burst_fire_does_not_exceed_pool_cap(self) -> None:
        """A burst of many simultaneous fire_webhook calls must not spawn more threads
        than _DELIVERY_MAX_WORKERS at any given instant.

        We register N webhooks (bypassing SSRF guard via allow_local) and fire a
        single event.  The executor queues work but only _DELIVERY_MAX_WORKERS threads
        run concurrently — measured by tracking the peak active-worker count.
        """
        N_WEBHOOKS = 20
        SLOW_DELIVERY_SEC = 0.05  # each delivery sleeps briefly

        mgr = _make_manager()
        # Use allow_local=True to bypass DNS resolution for fake test URLs
        for i in range(N_WEBHOOKS):
            mgr.register_webhook(f"https://example{i}.test/hook", events=[], allow_local=True)

        peak_concurrent = [0]
        current_concurrent = [0]
        counter_lock = threading.Lock()

        def slow_and_counted_post(url, body, secret, allow_local=False):
            with counter_lock:
                current_concurrent[0] += 1
                if current_concurrent[0] > peak_concurrent[0]:
                    peak_concurrent[0] = current_concurrent[0]
            try:
                time.sleep(SLOW_DELIVERY_SEC)
                return 200
            finally:
                with counter_lock:
                    current_concurrent[0] -= 1

        with patch.object(mgr, "_post_once", side_effect=slow_and_counted_post):
            mgr.fire_webhook("stt.final", {"text": "test"})
            # Wait for all queued tasks to complete
            mgr._executor.shutdown(wait=True, cancel_futures=False)

        # Peak concurrency must not exceed the pool cap
        self.assertLessEqual(
            peak_concurrent[0],
            _DELIVERY_MAX_WORKERS,
            f"Peak concurrent deliveries {peak_concurrent[0]} exceeded pool cap {_DELIVERY_MAX_WORKERS}",
        )
        self.assertGreater(peak_concurrent[0], 0, "Expected at least one delivery")

    def test_fire_webhook_does_not_spawn_threading_thread(self) -> None:
        """fire_webhook must NOT call threading.Thread (uses executor.submit instead)."""
        mgr = _make_manager()
        mgr.register_webhook("https://example.com/hook", events=[], allow_local=True)

        thread_created = []

        original_thread = threading.Thread

        class CapturingThread(original_thread):
            def __init__(self, *args, **kwargs):
                thread_created.append(True)
                super().__init__(*args, **kwargs)

        # If fire_webhook calls threading.Thread directly, thread_created will be non-empty
        with patch("backend.webhook_manager.threading.Thread", CapturingThread):
            with patch.object(mgr._executor, "submit") as mock_submit:
                mgr.fire_webhook("stt.final", {})

        # executor.submit should be called, NOT threading.Thread
        mock_submit.assert_called()
        self.assertEqual(thread_created, [], "fire_webhook must not create raw threads — use executor")


# ---------------------------------------------------------------------------
# Existing SSRF guard regression (ensure old tests still conceptually pass)
# ---------------------------------------------------------------------------

class ExistingSSRFNotRegressedTestCase(unittest.TestCase):
    """Sanity checks that BUG 2 fixes did not break the existing SSRF guard behaviour."""

    def test_standard_127_0_0_1_still_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://127.0.0.1/hook")
        self.assertFalse(safe)
        self.assertIn("loopback", reason.lower())

    def test_standard_192_168_still_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://192.168.1.1/hook")
        self.assertFalse(safe)
        self.assertIn("private", reason.lower())

    def test_standard_169_254_still_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://169.254.169.254/hook")
        self.assertFalse(safe)

    def test_public_url_still_accepted(self) -> None:
        # Gap 3 fix (W1721): DNS failures are now fail-closed at registration too.
        # Use a real resolvable domain (example.com) for this test.
        safe, reason = _is_safe_webhook_url("https://example.com/notify", strict=False)
        self.assertTrue(safe, f"Expected safe=True, got reason={reason!r}")
        self.assertIsNone(reason)

    def test_allow_local_bypasses_private(self) -> None:
        safe, reason = _is_safe_webhook_url("http://192.168.1.1/hook", allow_local=True)
        self.assertTrue(safe)
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
