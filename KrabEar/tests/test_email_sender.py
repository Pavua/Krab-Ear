"""Wave 142 — unit tests for EmailSender (backend/email_sender.py).

All SMTP and subprocess calls are mocked; no real network or keychain access.
"""
from __future__ import annotations

import smtplib
import subprocess
import sys
import threading
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

def _make_smtp_sender(host: str = "smtp.example.com", user: str = "u@example.com",
                      password: str = "secret", use_tls: bool = True,
                      use_ssl: bool = False) -> EmailSender:
    return EmailSender(
        backend_name="smtp",
        smtp_host=host,
        smtp_port=587,
        smtp_user=user,
        smtp_password=password,
        smtp_use_tls=use_tls,
        smtp_use_ssl=use_ssl,
        use_keychain=False,
    )


# ---------------------------------------------------------------------------
# Wave 142.1: test_smtp_send_basic
# ---------------------------------------------------------------------------

class TestSmtpSendBasic(unittest.TestCase):
    """SMTP basic send path: message assembled and sent via smtplib."""

    def test_smtp_send_basic(self):
        """send() calls server.sendmail with correct from/to/message."""
        sender = _make_smtp_sender()
        mock_server = MagicMock()
        # smtplib.SMTP used as context manager
        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(
                to="recv@example.com",
                subject="Test subject",
                body_html="<b>Hello</b>",
            )
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("u@example.com", "secret")
        mock_server.sendmail.assert_called_once()
        send_args = mock_server.sendmail.call_args
        # sendmail(from_addr, to_addrs, msg)
        self.assertEqual(send_args[0][0], "u@example.com")
        self.assertIn("recv@example.com", send_args[0][1])

    def test_smtp_send_ssl_path(self):
        """When smtp_use_ssl=True, SMTP_SSL is used instead of SMTP."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="u@example.com",
            smtp_password="pass",
            smtp_use_tls=False,
            smtp_use_ssl=True,
            use_keychain=False,
        )
        mock_server = MagicMock()
        with patch("smtplib.SMTP_SSL") as MockSSL:
            MockSSL.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSSL.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(to="r@example.com", subject="S", body_html="<p>hi</p>")
        mock_server.sendmail.assert_called_once()
        # starttls must NOT be called for SSL mode
        mock_server.starttls.assert_not_called()

    def test_smtp_no_host_raises_runtime_error(self):
        """Missing smtp_host must raise RuntimeError before connecting."""
        sender = EmailSender(backend_name="smtp", smtp_host="", use_keychain=False)
        with self.assertRaises(RuntimeError, msg="Expected RuntimeError for missing host"):
            sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")

    def test_send_empty_to_raises_value_error(self):
        """Empty recipient address must raise ValueError."""
        sender = _make_smtp_sender()
        with self.assertRaises(ValueError):
            sender.send(to="", subject="S", body_html="<p>x</p>")

    def test_send_empty_subject_raises_value_error(self):
        """Empty subject must raise ValueError."""
        sender = _make_smtp_sender()
        with self.assertRaises(ValueError):
            sender.send(to="r@x.com", subject="", body_html="<p>x</p>")


# ---------------------------------------------------------------------------
# Wave 142.2: test_keychain_password_retrieved
# ---------------------------------------------------------------------------

class TestKeychainPasswordRetrieved(unittest.TestCase):
    """Keychain lookup is invoked when smtp_password is empty and use_keychain=True."""

    def test_keychain_password_retrieved(self):
        """get_smtp_password() calls subprocess security when no explicit password."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_user="u@example.com",
            smtp_password="",
            use_keychain=True,
        )
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stdout = "keychain-secret\n"
        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            pwd = sender.get_smtp_password()
        self.assertEqual(pwd, "keychain-secret")
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        self.assertIn("security", cmd_args)
        self.assertIn("-w", cmd_args)

    def test_keychain_disabled_returns_empty(self):
        """When use_keychain=False, get_smtp_password() returns empty without subprocess call."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_user="u@example.com",
            smtp_password="",
            use_keychain=False,
        )
        with patch("subprocess.run") as mock_run:
            pwd = sender.get_smtp_password()
        self.assertEqual(pwd, "")
        mock_run.assert_not_called()

    def test_explicit_password_bypasses_keychain(self):
        """When explicit password is set, keychain subprocess must NOT be called."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_user="u@example.com",
            smtp_password="explicit-pass",
            use_keychain=True,
        )
        with patch("subprocess.run") as mock_run:
            pwd = sender.get_smtp_password()
        self.assertEqual(pwd, "explicit-pass")
        mock_run.assert_not_called()

    def test_keychain_unavailable_returns_empty(self):
        """If keychain subprocess raises, get_smtp_password() returns empty string gracefully."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_user="u@example.com",
            smtp_password="",
            use_keychain=True,
        )
        with patch("subprocess.run", side_effect=FileNotFoundError("security not found")):
            pwd = sender.get_smtp_password()
        self.assertEqual(pwd, "")

    def test_keychain_nonzero_returncode_returns_empty(self):
        """Non-zero returncode from security(1) returns empty string."""
        sender = EmailSender(
            backend_name="smtp",
            smtp_user="u@example.com",
            smtp_password="",
            use_keychain=True,
        )
        fake_proc = MagicMock()
        fake_proc.returncode = 44  # not found
        fake_proc.stdout = ""
        with patch("subprocess.run", return_value=fake_proc):
            pwd = sender.get_smtp_password()
        self.assertEqual(pwd, "")


# ---------------------------------------------------------------------------
# Wave 142.3: test_mail_app_fallback
# ---------------------------------------------------------------------------

class TestMailAppFallback(unittest.TestCase):
    """Mail.app backend calls osascript with correct script and handles errors."""

    def test_mail_app_fallback(self):
        """backend_name='mail_app' invokes osascript; success = no exception raised.

        Post-W1747: values are passed as argv elements, not interpolated into
        the script text.  cmd structure: [osascript, -e, <script>, to, subject, body]
        """
        sender = EmailSender(backend_name="mail_app")
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        with patch("subprocess.run", return_value=fake_proc) as mock_run:
            sender.send(to="r@x.com", subject="Test", body_html="<p>Hello</p>")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "osascript")
        # Script text (cmd[2]) must reference Mail.app; values are in later argv elements
        script_text = cmd[2]
        self.assertIn("Mail", script_text)
        # Recipient is passed as a discrete argv element (cmd[3]), NOT in the script text
        self.assertEqual(cmd[3], "r@x.com")

    def test_mail_app_osascript_failure_raises_runtime_error(self):
        """Non-zero osascript exit must raise RuntimeError."""
        sender = EmailSender(backend_name="mail_app")
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = "Mail.app error: account not found"
        with patch("subprocess.run", return_value=fake_proc):
            with self.assertRaises(RuntimeError):
                sender.send(to="r@x.com", subject="Test", body_html="<p>Hi</p>")

    def test_mail_app_timeout_raises_runtime_error(self):
        """osascript timeout must raise RuntimeError."""
        sender = EmailSender(backend_name="mail_app")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=30)):
            with self.assertRaises(RuntimeError):
                sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")

    def test_mail_app_osascript_not_found_raises(self):
        """FileNotFoundError from osascript must raise RuntimeError."""
        sender = EmailSender(backend_name="mail_app")
        with patch("subprocess.run", side_effect=FileNotFoundError("No such file")):
            with self.assertRaises(RuntimeError):
                sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")


# ---------------------------------------------------------------------------
# Wave 142.4: test_unicode_subject_body
# ---------------------------------------------------------------------------

class TestUnicodeSubjectBody(unittest.TestCase):
    """Unicode (Cyrillic, emoji) in subject and body must be handled without error."""

    def test_unicode_subject_body(self):
        """Russian subject/body with emoji is encoded and sent without exception."""
        sender = _make_smtp_sender()
        mock_server = MagicMock()
        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            sender.send(
                to="recv@example.com",
                subject="Краб — ежедневный дайджест",
                body_html="<p>Привет! Записей сегодня: <b>7</b> </p>",
                body_text="Привет! Записей сегодня: 7",
            )
        mock_server.sendmail.assert_called_once()
        raw_msg: str = mock_server.sendmail.call_args[0][2]
        # Subject in the message must encode Cyrillic (base64 or quoted-printable)
        self.assertIn("Subject", raw_msg)

    def test_strip_html_preserves_text_content(self):
        """_strip_html() must strip tags and preserve text content."""
        html = "<h1>Заголовок</h1><p>Текст <b>важный</b></p>"
        result = EmailSender._strip_html(html)
        self.assertIn("Заголовок", result)
        self.assertIn("Текст", result)
        self.assertIn("важный", result)
        self.assertNotIn("<", result)

    def test_strip_html_br_to_newline(self):
        """<br> tags must be converted to newlines."""
        html = "Строка 1<br>Строка 2<br/>Строка 3"
        result = EmailSender._strip_html(html)
        self.assertIn("\n", result)
        self.assertIn("Строка 1", result)
        self.assertIn("Строка 2", result)


# ---------------------------------------------------------------------------
# Wave 142.5: test_concurrent_send
# ---------------------------------------------------------------------------

class TestConcurrentSend(unittest.TestCase):
    """Multiple threads sending simultaneously must not race or raise unexpectedly."""

    def test_concurrent_send(self):
        """10 concurrent send() calls each from their own thread must all succeed."""
        errors: list[Exception] = []
        lock = threading.Lock()

        def _worker(tid: int) -> None:
            sender = _make_smtp_sender(user=f"u{tid}@example.com", password="pw")
            mock_server = MagicMock()
            try:
                with patch("smtplib.SMTP") as MockSMTP:
                    MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
                    MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
                    sender.send(
                        to=f"r{tid}@example.com",
                        subject=f"Thread {tid} subject",
                        body_html=f"<p>Thread {tid} body</p>",
                    )
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Unexpected errors in concurrent send: {errors}")


# ---------------------------------------------------------------------------
# Wave 142.6: test_handles_smtp_failure_gracefully
# ---------------------------------------------------------------------------

class TestHandlesSmtpFailureGracefully(unittest.TestCase):
    """SMTP failure must raise RuntimeError, not leak smtplib internals."""

    def test_handles_smtp_failure_gracefully(self):
        """SMTPException must be wrapped in RuntimeError."""
        sender = _make_smtp_sender()
        mock_server = MagicMock()
        mock_server.sendmail.side_effect = smtplib.SMTPRecipientsRefused(recipients={"r@x.com": (550, "User unknown")})
        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            with self.assertRaises(RuntimeError) as ctx:
                sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")
        self.assertIn("SMTP", str(ctx.exception))

    def test_handles_connection_refused_gracefully(self):
        """OSError (connection refused) must be wrapped in RuntimeError."""
        sender = _make_smtp_sender()
        with patch("smtplib.SMTP", side_effect=OSError("Connection refused")):
            with self.assertRaises(RuntimeError) as ctx:
                sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")
        self.assertIn("Сетевая", str(ctx.exception))

    def test_smtp_auth_failure_raises_runtime_error(self):
        """SMTPAuthenticationError must be raised as RuntimeError."""
        sender = _make_smtp_sender()
        mock_server = MagicMock()
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, "Auth failed")
        with patch("smtplib.SMTP") as MockSMTP:
            MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
            MockSMTP.return_value.__exit__ = MagicMock(return_value=False)
            with self.assertRaises(RuntimeError):
                sender.send(to="r@x.com", subject="S", body_html="<p>hi</p>")


# ---------------------------------------------------------------------------
# from_settings() factory
# ---------------------------------------------------------------------------

class TestFromSettings(unittest.TestCase):
    """EmailSender.from_settings() correctly reads config attributes."""

    def test_from_settings_smtp_backend(self):
        """from_settings() maps RECAP_BACKEND / SMTP_* attributes correctly."""
        class FakeCfg:
            RECAP_BACKEND = "smtp"
            SMTP_HOST = "mail.example.com"
            SMTP_PORT = 465
            SMTP_USER = "cfg@example.com"
            SMTP_PASSWORD = "cfg-secret"
            SMTP_USE_TLS = False
            SMTP_USE_SSL = True

        sender = EmailSender.from_settings(FakeCfg())
        self.assertEqual(sender.backend_name, "smtp")
        self.assertEqual(sender.smtp_host, "mail.example.com")
        self.assertEqual(sender.smtp_port, 465)
        self.assertTrue(sender.smtp_use_ssl)
        self.assertFalse(sender.smtp_use_tls)

    def test_from_settings_mail_app_backend(self):
        """from_settings() with RECAP_BACKEND='mail_app' creates mail_app sender."""
        class FakeCfg:
            RECAP_BACKEND = "mail_app"
            SMTP_HOST = ""
            SMTP_PORT = 587
            SMTP_USER = ""
            SMTP_PASSWORD = ""
            SMTP_USE_TLS = True
            SMTP_USE_SSL = False

        sender = EmailSender.from_settings(FakeCfg())
        self.assertEqual(sender.backend_name, "mail_app")

    def test_from_settings_missing_attrs_use_defaults(self):
        """from_settings() must not crash if optional attributes are absent."""
        class MinimalCfg:
            pass  # no attributes at all

        sender = EmailSender.from_settings(MinimalCfg())
        self.assertIsInstance(sender, EmailSender)
        self.assertEqual(sender.backend_name, "smtp")


if __name__ == "__main__":
    unittest.main()
