"""Wave-22 — TLS cert+hostname verification in EmailSender (MED, MITM fix).

Before this fix: SMTP_SSL and starttls() passed no SSLContext, so Python
defaulted to ssl._create_stdlib_context() with CERT_NONE + check_hostname=False
— encrypted but completely unverified → MITM could capture password + body.

After the fix:
- Default (smtp_tls_insecure=False): ssl.create_default_context() is used,
  giving CERT_REQUIRED + check_hostname=True via the system trust store.
- Opt-out (smtp_tls_insecure=True): explicit unverified SSLContext is built
  with a loud warning log; intended for local test mail servers only.

These tests verify the context passed to smtplib — no real network connections.
"""
from __future__ import annotations

import ssl
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.email_sender import EmailSender


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _smtp_sender(use_ssl: bool = False, use_tls: bool = True,
                 insecure: bool = False) -> EmailSender:
    return EmailSender(
        backend_name="smtp",
        smtp_host="smtp.example.com",
        smtp_port=465 if use_ssl else 587,
        smtp_user="u@example.com",
        smtp_password="pw",
        smtp_use_tls=use_tls,
        smtp_use_ssl=use_ssl,
        smtp_tls_insecure=insecure,
        use_keychain=False,
    )


# ---------------------------------------------------------------------------
# Wave-22.1: default STARTTLS path — context must be CERT_REQUIRED
# ---------------------------------------------------------------------------

class TestStarttlsDefaultSecure(unittest.TestCase):
    """starttls() called with a CERT_REQUIRED + check_hostname=True context by default."""

    def test_starttls_context_cert_required(self):
        """Default send via STARTTLS passes ssl.create_default_context() to starttls()."""
        sender = _smtp_sender(use_ssl=False, use_tls=True, insecure=False)
        mock_server = MagicMock()
        captured = {}

        def fake_starttls(context=None):
            captured["ctx"] = context

        mock_server.starttls.side_effect = fake_starttls

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured.get("ctx")
        self.assertIsNotNone(ctx, "starttls() must receive a context arg")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED,
                         "Default context must have CERT_REQUIRED")
        self.assertTrue(ctx.check_hostname,
                        "Default context must have check_hostname=True")

    def test_starttls_default_context_is_system_trust_store(self):
        """Default context comes from ssl.create_default_context() (not _create_stdlib_context)."""
        sender = _smtp_sender(use_ssl=False, use_tls=True, insecure=False)
        mock_server = MagicMock()
        captured_ctx = {}

        def fake_starttls(context=None):
            captured_ctx["ctx"] = context

        mock_server.starttls.side_effect = fake_starttls

        # Create a reference default context to compare type and verify_mode
        reference_ctx = ssl.create_default_context()

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured_ctx.get("ctx")
        self.assertIsNotNone(ctx)
        # Both must be CERT_REQUIRED SSLContexts
        self.assertEqual(ctx.verify_mode, reference_ctx.verify_mode)
        self.assertEqual(ctx.check_hostname, reference_ctx.check_hostname)


# ---------------------------------------------------------------------------
# Wave-22.2: default SMTP_SSL path — context must be CERT_REQUIRED
# ---------------------------------------------------------------------------

class TestSmtpSslDefaultSecure(unittest.TestCase):
    """SMTP_SSL() called with a CERT_REQUIRED + check_hostname=True context by default."""

    def test_smtp_ssl_context_cert_required(self):
        """Default send via SMTP_SSL passes ssl.create_default_context() as context kwarg."""
        sender = _smtp_sender(use_ssl=True, use_tls=False, insecure=False)
        mock_server = MagicMock()
        captured_context = {}

        def fake_smtp_ssl(host, port, context=None):
            captured_context["ctx"] = context
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=mock_server)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("smtplib.SMTP_SSL", side_effect=fake_smtp_ssl):
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured_context.get("ctx")
        self.assertIsNotNone(ctx, "SMTP_SSL() must receive a context= arg")
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED,
                         "SSL context must have CERT_REQUIRED")
        self.assertTrue(ctx.check_hostname,
                        "SSL context must have check_hostname=True")

    def test_smtp_ssl_context_is_not_cert_none(self):
        """SMTP_SSL() context must NOT be CERT_NONE (the old insecure default)."""
        sender = _smtp_sender(use_ssl=True, use_tls=False, insecure=False)
        mock_server = MagicMock()
        captured_context = {}

        def fake_smtp_ssl(host, port, context=None):
            captured_context["ctx"] = context
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=mock_server)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("smtplib.SMTP_SSL", side_effect=fake_smtp_ssl):
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured_context.get("ctx")
        self.assertNotEqual(ctx.verify_mode, ssl.CERT_NONE,
                            "CERT_NONE is insecure and must never be used by default")


# ---------------------------------------------------------------------------
# Wave-22.3: insecure opt-out (smtp_tls_insecure=True)
# ---------------------------------------------------------------------------

class TestInsecureOptOut(unittest.TestCase):
    """smtp_tls_insecure=True yields an unverified context only when explicitly set."""

    def test_starttls_insecure_yields_cert_none(self):
        """With smtp_tls_insecure=True, starttls() receives CERT_NONE context."""
        sender = _smtp_sender(use_ssl=False, use_tls=True, insecure=True)
        mock_server = MagicMock()
        captured = {}

        def fake_starttls(context=None):
            captured["ctx"] = context

        mock_server.starttls.side_effect = fake_starttls

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured.get("ctx")
        self.assertIsNotNone(ctx)
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE,
                         "Insecure opt-out must set CERT_NONE")
        self.assertFalse(ctx.check_hostname,
                         "Insecure opt-out must disable check_hostname")

    def test_smtp_ssl_insecure_yields_cert_none(self):
        """With smtp_tls_insecure=True, SMTP_SSL() receives CERT_NONE context."""
        sender = _smtp_sender(use_ssl=True, use_tls=False, insecure=True)
        mock_server = MagicMock()
        captured_context = {}

        def fake_smtp_ssl(host, port, context=None):
            captured_context["ctx"] = context
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=mock_server)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("smtplib.SMTP_SSL", side_effect=fake_smtp_ssl):
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured_context.get("ctx")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_insecure_logs_warning(self):
        """smtp_tls_insecure=True must emit a WARNING log."""
        sender = _smtp_sender(use_ssl=False, use_tls=True, insecure=True)
        mock_server = MagicMock()

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            with self.assertLogs("KrabEar.Backend.EmailSender", level="WARNING") as log_ctx:
                sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        joined = " ".join(log_ctx.output)
        self.assertIn("insecure", joined.lower(),
                      "Warning log must mention insecure mode")


# ---------------------------------------------------------------------------
# Wave-22.4: default is False — no opt-out without explicit flag
# ---------------------------------------------------------------------------

class TestInsecureDefaultFalse(unittest.TestCase):
    """smtp_tls_insecure defaults to False — secure by default."""

    def test_default_insecure_is_false(self):
        """Constructing EmailSender without smtp_tls_insecure keeps default False."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="u@example.com",
            smtp_password="pw",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            use_keychain=False,
        )
        self.assertFalse(sender.smtp_tls_insecure,
                         "smtp_tls_insecure must default to False")

    def test_from_settings_insecure_false_by_default(self):
        """from_settings() without SMTP_TLS_INSECURE defaults to insecure=False."""
        class MinCfg:
            SMTP_HOST = "smtp.example.com"

        sender = EmailSender.from_settings(MinCfg())
        self.assertFalse(sender.smtp_tls_insecure)

    def test_from_settings_insecure_can_be_opted_in(self):
        """from_settings() reads SMTP_TLS_INSECURE=True when explicitly set."""
        class InsecureCfg:
            SMTP_HOST = "localhost"
            SMTP_TLS_INSECURE = True

        sender = EmailSender.from_settings(InsecureCfg())
        self.assertTrue(sender.smtp_tls_insecure)


# ---------------------------------------------------------------------------
# Wave-22.5: secure context propagated even when no login credentials
# ---------------------------------------------------------------------------

class TestSecureContextNoCredentials(unittest.TestCase):
    """TLS context is used even when smtp_user/password are empty (anonymous relay)."""

    def test_starttls_context_used_without_credentials(self):
        """starttls() still receives CERT_REQUIRED context when no login is needed."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_host="relay.example.com",
            smtp_port=587,
            smtp_user="",
            smtp_password="",
            smtp_use_tls=True,
            smtp_use_ssl=False,
            smtp_tls_insecure=False,
            use_keychain=False,
        )
        mock_server = MagicMock()
        captured = {}

        def fake_starttls(context=None):
            captured["ctx"] = context

        mock_server.starttls.side_effect = fake_starttls

        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(to="r@example.com", subject="S", body_html="<p>x</p>")

        ctx = captured.get("ctx")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        # login must NOT have been called
        mock_server.login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
