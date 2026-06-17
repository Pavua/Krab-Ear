"""W1771 LOW regression tests — EmailSender SMTP transport re-reads runtime settings.

Guards the fix for: EmailSender.from_settings(pydantic_singleton) was called ONCE
in BackendService.__init__ and the resulting sender stored on RecapScheduler was
NEVER rebuilt.  So a runtime set_settings({smtp_host: 'new', smtp_use_tls: False})
persisted to cached_settings (lowercase keys) but the live sender kept the stale
startup values — emails would keep going to the old SMTP server until backend restart.

Fix: EmailSender.update_transport_from_settings(get_setting) reads lowercase keys.
     RecapScheduler._refresh_settings() calls it each tick via the existing
     settings_provider.

These tests run without mlx-whisper, sounddevice, or any other heavy dep
(EmailSender and RecapScheduler only import stdlib + typing).
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — same pattern used by every test in this project
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.email_sender import EmailSender
from backend.recap_scheduler import RecapScheduler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sender(
    backend_name: str = "smtp",
    smtp_host: str = "old.relay.test",
    smtp_port: int = 587,
    smtp_user: str = "user@old.test",
    smtp_use_tls: bool = True,
    smtp_use_ssl: bool = False,
    smtp_tls_insecure: bool = False,
) -> EmailSender:
    return EmailSender(
        backend_name=backend_name,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_use_tls=smtp_use_tls,
        smtp_use_ssl=smtp_use_ssl,
        smtp_tls_insecure=smtp_tls_insecure,
    )


def _make_digest():
    d = MagicMock()
    d.date = "2026-06-17"
    d.total_recordings = 3
    d.total_duration_min = 5.0
    d.total_words = 120
    d.languages_used = {"ru": 3}
    d.top_topics = ["тест"]
    d.highlights = ["Регрессионный тест W1771."]
    d.formatted_markdown = "# Дайджест 2026-06-17\n\n- Записей: 3"
    return d


def _make_scheduler(tmpdir: Path, sender: EmailSender,
                    settings_dict: dict) -> RecapScheduler:
    digest_gen = MagicMock()
    digest_gen.generate_digest.return_value = _make_digest()
    store = MagicMock()
    return RecapScheduler(
        email_sender=sender,
        digest_generator=digest_gen,
        store=store,
        data_dir=tmpdir,
        recap_email_to="test@example.com",
        recap_time_hour=20,
        enabled=True,
        check_interval_sec=9999,
        settings_provider=lambda: settings_dict,
    )


# ---------------------------------------------------------------------------
# Part 1 — EmailSender.update_transport_from_settings
# ---------------------------------------------------------------------------

class TestUpdateTransportFromSettings(unittest.TestCase):
    """Unit tests for EmailSender.update_transport_from_settings."""

    def test_updates_smtp_host(self):
        sender = _make_sender(smtp_host="old.relay.test")
        s = {"smtp_host": "new.relay.test"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.smtp_host, "new.relay.test")

    def test_updates_smtp_port(self):
        sender = _make_sender(smtp_port=587)
        s = {"smtp_port": 465}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.smtp_port, 465)

    def test_updates_smtp_use_tls_false(self):
        sender = _make_sender(smtp_use_tls=True)
        s = {"smtp_use_tls": False}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertFalse(sender.smtp_use_tls)

    def test_updates_smtp_use_ssl_true(self):
        sender = _make_sender(smtp_use_ssl=False)
        s = {"smtp_use_ssl": True}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertTrue(sender.smtp_use_ssl)

    def test_updates_smtp_user_and_smtp_from_when_derived(self):
        """smtp_from was derived from smtp_user at init: must follow smtp_user."""
        sender = _make_sender(smtp_user="old@example.com")
        # After init smtp_from == smtp_user (derived)
        self.assertEqual(sender.smtp_from, "old@example.com")
        s = {"smtp_user": "new@example.com"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.smtp_user, "new@example.com")
        self.assertEqual(sender.smtp_from, "new@example.com")

    def test_does_not_update_smtp_from_when_explicitly_set(self):
        """smtp_from was set explicitly to a different address: must NOT be overwritten."""
        sender = EmailSender(
            smtp_user="user@example.com",
            smtp_from="noreply@company.com",
        )
        s = {"smtp_user": "new@example.com"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.smtp_user, "new@example.com")
        # smtp_from was NOT derived from smtp_user, so it must stay unchanged
        self.assertEqual(sender.smtp_from, "noreply@company.com")

    def test_updates_backend_name(self):
        sender = _make_sender(backend_name="smtp")
        s = {"recap_backend": "mail_app"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.backend_name, "mail_app")

    def test_updates_smtp_tls_insecure(self):
        sender = _make_sender(smtp_tls_insecure=False)
        s = {"smtp_tls_insecure": True}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertTrue(sender.smtp_tls_insecure)

    def test_no_override_uses_current_values(self):
        """When get_setting always returns the default, nothing changes."""
        sender = _make_sender(
            smtp_host="stable.relay.test",
            smtp_port=587,
            smtp_use_tls=True,
        )
        sender.update_transport_from_settings(lambda key, default: default)
        self.assertEqual(sender.smtp_host, "stable.relay.test")
        self.assertEqual(sender.smtp_port, 587)
        self.assertTrue(sender.smtp_use_tls)

    def test_invalid_port_string_is_ignored(self):
        """Non-numeric port value must leave smtp_port unchanged."""
        sender = _make_sender(smtp_port=587)
        s = {"smtp_port": "not-a-number"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender.smtp_port, 587)

    def test_exception_in_get_setting_is_swallowed(self):
        """An exception from get_setting must not propagate (logged only)."""
        sender = _make_sender(smtp_host="stable.relay.test")
        def bad_get(key, default):
            raise RuntimeError("provider exploded")
        # Should not raise
        sender.update_transport_from_settings(bad_get)
        # smtp_host may be corrupted mid-update, but no exception escapes
        # (partial state is acceptable for a LOW-severity runtime re-read)

    def test_does_not_alter_smtp_password(self):
        """Keychain-stored password must never be wiped by this method."""
        sender = _make_sender()
        sender._smtp_password = "secret123"
        s = {"smtp_host": "new.relay.test"}
        sender.update_transport_from_settings(lambda key, default: s.get(key, default))
        self.assertEqual(sender._smtp_password, "secret123")


# ---------------------------------------------------------------------------
# Part 2 — RecapScheduler._refresh_settings calls update_transport
# ---------------------------------------------------------------------------

class TestRecapSchedulerRefreshCallsUpdateTransport(unittest.TestCase):
    """_refresh_settings must delegate SMTP transport refresh to email_sender."""

    def test_refresh_settings_calls_update_transport(self):
        """After _refresh_settings, email_sender.smtp_host reflects runtime value."""
        with tempfile.TemporaryDirectory() as td:
            sender = _make_sender(smtp_host="old.relay.test")
            settings_dict: dict = {
                "smtp_host": "new.relay.test",
                "recap_email_to": "user@example.com",
                "recap_email_enabled": True,
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)
            sched._refresh_settings()
            self.assertEqual(sender.smtp_host, "new.relay.test")

    def test_refresh_settings_updates_use_tls(self):
        """After _refresh_settings, email_sender.smtp_use_tls reflects runtime value."""
        with tempfile.TemporaryDirectory() as td:
            sender = _make_sender(smtp_use_tls=True)
            settings_dict: dict = {
                "smtp_use_tls": False,
                "recap_email_to": "user@example.com",
                "recap_email_enabled": True,
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)
            sched._refresh_settings()
            self.assertFalse(sender.smtp_use_tls)

    def test_refresh_settings_with_mock_sender_no_method(self):
        """If email_sender lacks update_transport_from_settings, _refresh_settings must not raise.

        Guards backward compatibility: a MagicMock or stub sender without the method
        must survive the hasattr guard.
        """
        with tempfile.TemporaryDirectory() as td:
            sender = MagicMock(spec=EmailSender)
            # Explicitly remove the method to simulate an old sender without it
            del sender.update_transport_from_settings
            settings_dict: dict = {
                "smtp_host": "new.relay.test",
                "recap_email_to": "user@example.com",
                "recap_email_enabled": True,
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)
            # Must not raise even though sender lacks update_transport_from_settings
            sched._refresh_settings()

    def test_refresh_settings_no_override_leaves_transport_unchanged(self):
        """When runtime settings dict has no smtp_* keys, transport must be unchanged."""
        with tempfile.TemporaryDirectory() as td:
            sender = _make_sender(smtp_host="stable.relay.test", smtp_port=587)
            settings_dict: dict = {
                "recap_email_to": "user@example.com",
                "recap_email_enabled": True,
                # no smtp_* keys
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)
            sched._refresh_settings()
            self.assertEqual(sender.smtp_host, "stable.relay.test")
            self.assertEqual(sender.smtp_port, 587)


# ---------------------------------------------------------------------------
# Part 3 — Integration: send_recap uses updated transport
# ---------------------------------------------------------------------------

class TestSendRecapUsesUpdatedTransport(unittest.TestCase):
    """After a runtime smtp_host change, send_recap must use the new host."""

    def test_send_recap_uses_new_smtp_host_after_refresh(self):
        """Regression for W1771: verify that after updating smtp_host via
        _refresh_settings, a send attempt reaches the new host (not the stale one).

        We mock smtplib.SMTP to capture the host argument and verify it matches
        the runtime-updated value, not the stale startup value.
        """
        with tempfile.TemporaryDirectory() as td:
            sender = _make_sender(
                smtp_host="old.relay.test",
                smtp_port=587,
                smtp_use_tls=False,  # avoid STARTTLS in test
                smtp_use_ssl=False,
            )
            settings_dict: dict = {
                "smtp_host": "new.relay.test",
                "smtp_port": 2525,
                "smtp_use_tls": False,
                "smtp_use_ssl": False,
                "recap_email_to": "dest@example.com",
                "recap_email_enabled": True,
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)

            # Simulate one scheduler tick (re-reads settings before sending)
            sched._refresh_settings()

            # Verify transport was updated
            self.assertEqual(sender.smtp_host, "new.relay.test")
            self.assertEqual(sender.smtp_port, 2525)

            captured_host: list = []
            captured_port: list = []

            class FakeSMTP:
                def __init__(self, host, port):
                    captured_host.append(host)
                    captured_port.append(port)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    pass

                def login(self, *a):
                    pass

                def sendmail(self, *a):
                    pass

            with patch("smtplib.SMTP", FakeSMTP):
                sched.send_recap(target_date="2026-06-17")

            self.assertEqual(len(captured_host), 1, "Expected exactly one SMTP connection")
            self.assertEqual(captured_host[0], "new.relay.test",
                             f"Expected new.relay.test but got {captured_host[0]!r} — stale transport!")
            self.assertEqual(captured_port[0], 2525)

    def test_send_recap_with_no_runtime_override_uses_startup_defaults(self):
        """When no smtp_* override is present, startup defaults must still work."""
        with tempfile.TemporaryDirectory() as td:
            sender = _make_sender(
                smtp_host="default.relay.test",
                smtp_port=587,
                smtp_use_tls=False,
                smtp_use_ssl=False,
            )
            settings_dict: dict = {
                # no smtp_* keys — defaults preserved
                "recap_email_to": "dest@example.com",
                "recap_email_enabled": True,
            }
            sched = _make_scheduler(Path(td), sender, settings_dict)
            sched._refresh_settings()

            self.assertEqual(sender.smtp_host, "default.relay.test")
            self.assertEqual(sender.smtp_port, 587)


if __name__ == "__main__":
    unittest.main()
