"""Unit-тесты SSRF-защиты WebhookManager.

Wave 157 — закрывает gap из Wave 100 PR #455:
register_webhook теперь блокирует localhost, RFC1918, link-local и mDNS.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.webhook_manager import WebhookManager, _is_safe_webhook_url  # noqa: E402


def _make_manager(allow_local: bool = False) -> tuple[WebhookManager, str]:
    tmpdir = tempfile.mkdtemp()
    return WebhookManager(data_dir=tmpdir), tmpdir


class SSRFGuardFunctionTestCase(unittest.TestCase):
    """Прямые тесты функции _is_safe_webhook_url."""

    # 1 — localhost http отклонён
    def test_localhost_http_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://localhost/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("localhost", reason.lower())

    # 2 — localhost https отклонён
    def test_localhost_https_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("https://localhost:8080/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 3 — 127.0.0.1 отклонён
    def test_127_0_0_1_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://127.0.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("loopback", reason.lower())

    # 4 — произвольный loopback 127.x.x.x отклонён
    def test_127_x_x_x_loopback_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://127.99.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 5 — IPv6 loopback ::1 отклонён
    def test_ipv6_loopback_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://[::1]/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("loopback", reason.lower())

    # 6 — RFC1918 10.x.x.x отклонён
    def test_rfc1918_10_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://10.0.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("private", reason.lower())

    # 7 — RFC1918 192.168.x.x отклонён
    def test_rfc1918_192_168_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("https://192.168.1.100/api/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("private", reason.lower())

    # 8 — RFC1918 172.16.x.x отклонён
    def test_rfc1918_172_16_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://172.16.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("private", reason.lower())

    # 9 — RFC1918 172.31.x.x отклонён (последний блок диапазона)
    def test_rfc1918_172_31_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://172.31.255.255/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 10 — link-local 169.254.x.x отклонён
    def test_link_local_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://169.254.0.1/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("link-local", reason.lower())

    # 11 — mDNS .local отклонён
    def test_mdns_local_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://mydevice.local/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("local", reason.lower())

    # 12 — 0.0.0.0 отклонён
    def test_zero_addr_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("http://0.0.0.0/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)

    # 13 — публичный https URL принят (must be a host that actually resolves;
    # gap 3 fix makes DNS-fail fail-closed)
    def test_public_https_accepted(self) -> None:
        safe, reason = _is_safe_webhook_url("https://example.com/notify")
        self.assertTrue(safe)
        self.assertIsNone(reason)

    # 14 — публичный http URL принят
    def test_public_http_accepted(self) -> None:
        safe, reason = _is_safe_webhook_url("http://example.com/abc123")
        self.assertTrue(safe)
        self.assertIsNone(reason)

    # 15 — ftp:// отклонён (неверная схема)
    def test_ftp_scheme_rejected(self) -> None:
        safe, reason = _is_safe_webhook_url("ftp://example.com/hook")
        self.assertFalse(safe)
        self.assertIsNotNone(reason)
        self.assertIn("http", reason.lower())

    # 16 — allow_local=True пропускает localhost
    def test_allow_local_bypasses_localhost(self) -> None:
        safe, reason = _is_safe_webhook_url("http://localhost/hook", allow_local=True)
        self.assertTrue(safe)
        self.assertIsNone(reason)

    # 17 — allow_local=True пропускает RFC1918
    def test_allow_local_bypasses_rfc1918(self) -> None:
        safe, reason = _is_safe_webhook_url("http://192.168.1.1/hook", allow_local=True)
        self.assertTrue(safe)
        self.assertIsNone(reason)

    # 18 — allow_local=True всё равно блокирует ftp://
    def test_allow_local_still_blocks_bad_scheme(self) -> None:
        safe, reason = _is_safe_webhook_url("ftp://localhost/hook", allow_local=True)
        self.assertFalse(safe)
        self.assertIsNotNone(reason)


class SSRFGuardRegisterTestCase(unittest.TestCase):
    """Тесты SSRF-защиты через register_webhook."""

    def setUp(self) -> None:
        self._mgr, _ = _make_manager()

    # 19 — register_webhook с localhost вызывает ValueError
    def test_register_localhost_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._mgr.register_webhook("http://localhost:5005/hook", events=[])
        self.assertIn("SSRF", str(ctx.exception))

    # 20 — register_webhook с 127.0.0.1 вызывает ValueError
    def test_register_127_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("http://127.0.0.1:8088/hook", events=[])

    # 21 — register_webhook с 192.168.x вызывает ValueError
    def test_register_private_ip_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("https://192.168.0.100/hook", events=[])

    # 22 — register_webhook с .local вызывает ValueError
    def test_register_mdns_raises(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.register_webhook("http://openclaw.local:18789/hook", events=[])

    # 23 — register_webhook с публичным URL работает (must resolve; use real domain)
    def test_register_public_url_works(self) -> None:
        wid = self._mgr.register_webhook("https://example.com/notify", events=[])
        self.assertIsInstance(wid, str)
        self.assertTrue(len(wid) > 0)

    # 24 — allow_local=True в register_webhook позволяет localhost
    def test_register_allow_local_override(self) -> None:
        wid = self._mgr.register_webhook(
            "http://localhost:1234/hook",
            events=[],
            allow_local=True,
        )
        self.assertIsInstance(wid, str)

    # 25 — handle_register_webhook с webhook_allow_local=True позволяет localhost через IPC
    def test_ipc_allow_local_override(self) -> None:
        result = self._mgr.handle_register_webhook({
            "url": "http://127.0.0.1:5005/internal",
            "events": [],
            "webhook_allow_local": True,
        })
        self.assertIn("webhook_id", result)

    # 26 — handle_register_webhook без allow_local отклоняет 10.x
    def test_ipc_blocks_private_by_default(self) -> None:
        with self.assertRaises(ValueError):
            self._mgr.handle_register_webhook({
                "url": "http://10.0.0.1/hook",
                "events": [],
            })


if __name__ == "__main__":
    unittest.main()
