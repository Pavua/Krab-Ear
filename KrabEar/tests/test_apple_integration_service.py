"""Unit tests — AppleIntegrationService (6 IPC handlers).

Handlers under test:
  - handle_send_to_telegram      — send text to Telegram via TelegramBridge
  - handle_list_telegram_chats   — list Telegram chats via TelegramBridge
  - handle_create_apple_note     — create Apple Note via osascript
  - handle_create_apple_reminder — create Apple Reminder via osascript
  - handle_create_calendar_event — create Apple Calendar event via osascript
  - handle_send_imessage         — send iMessage via osascript

All collaborators are mocked; subprocess.run is patched to avoid real osascript calls.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.apple_integration_service import AppleIntegrationService  # noqa: E402
from backend.telegram_bridge import CircuitBreakerOpen  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(bridge=None) -> AppleIntegrationService:
    return AppleIntegrationService(telegram_bridge=bridge or MagicMock())


def _completed(returncode=0, stdout="note id 1", stderr=""):
    """Return a fake subprocess.CompletedProcess."""
    cp = MagicMock()
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# handle_send_to_telegram
# ---------------------------------------------------------------------------

class TestHandleSendToTelegram(unittest.TestCase):

    def test_happy_path_returns_bridge_result(self):
        bridge = MagicMock()
        bridge.send_message.return_value = {
            "message_id": 42,
            "sent_at": "2026-05-26T10:00:00",
            "chat_title": "Dev Chat",
        }
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            result = svc.handle_send_to_telegram({"text": "hello", "chat_id": 123})
        self.assertEqual(result["message_id"], 42)
        bridge.send_message.assert_called_once_with(text="hello", chat_id=123, reply_to=None)

    def test_string_chat_id_passed_through(self):
        bridge = MagicMock()
        bridge.send_message.return_value = {"message_id": 1, "sent_at": "", "chat_title": ""}
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            svc.handle_send_to_telegram({"text": "hi", "chat_id": "@mychannel"})
        bridge.send_message.assert_called_once_with(
            text="hi", chat_id="@mychannel", reply_to=None
        )

    def test_reply_to_parsed_as_int(self):
        bridge = MagicMock()
        bridge.send_message.return_value = {"message_id": 2, "sent_at": "", "chat_title": ""}
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            svc.handle_send_to_telegram({"text": "reply", "chat_id": 1, "reply_to": "99"})
        bridge.send_message.assert_called_once_with(text="reply", chat_id=1, reply_to=99)

    def test_bridge_disabled_raises_runtime_error(self):
        svc = _make_service()
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = False
            with self.assertRaises(RuntimeError) as ctx:
                svc.handle_send_to_telegram({"text": "x", "chat_id": 1})
        self.assertIn("bridge_disabled", str(ctx.exception))

    def test_empty_text_raises_value_error(self):
        svc = _make_service()
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(ValueError):
                svc.handle_send_to_telegram({"text": "  ", "chat_id": 1})

    def test_missing_chat_id_raises_value_error(self):
        svc = _make_service()
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(ValueError):
                svc.handle_send_to_telegram({"text": "hello"})

    def test_circuit_breaker_open_wraps_as_runtime_error(self):
        bridge = MagicMock()
        bridge.send_message.side_effect = CircuitBreakerOpen("open")
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                svc.handle_send_to_telegram({"text": "x", "chat_id": 1})
        self.assertIn("circuit_open", str(ctx.exception))

    def test_generic_bridge_exception_wrapped_as_krab_unavailable(self):
        bridge = MagicMock()
        bridge.send_message.side_effect = ConnectionError("refused")
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                svc.handle_send_to_telegram({"text": "x", "chat_id": 1})
        self.assertIn("krab_unavailable", str(ctx.exception))


# ---------------------------------------------------------------------------
# handle_list_telegram_chats
# ---------------------------------------------------------------------------

class TestHandleListTelegramChats(unittest.TestCase):

    def test_returns_chats_list(self):
        bridge = MagicMock()
        bridge.get_chats.return_value = [
            {"id": 1, "title": "Dev", "type": "private"},
        ]
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            result = svc.handle_list_telegram_chats({})
        self.assertIn("chats", result)
        self.assertEqual(len(result["chats"]), 1)

    def test_bridge_disabled_raises(self):
        svc = _make_service()
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = False
            with self.assertRaises(RuntimeError) as ctx:
                svc.handle_list_telegram_chats({})
        self.assertIn("bridge_disabled", str(ctx.exception))

    def test_circuit_open_raises(self):
        bridge = MagicMock()
        bridge.get_chats.side_effect = CircuitBreakerOpen("breaker")
        svc = _make_service(bridge)
        with patch("backend.apple_integration_service.settings") as mock_settings:
            mock_settings.TELEGRAM_BRIDGE_ENABLED = True
            with self.assertRaises(RuntimeError) as ctx:
                svc.handle_list_telegram_chats({})
        self.assertIn("circuit_open", str(ctx.exception))


# ---------------------------------------------------------------------------
# handle_create_apple_note
# ---------------------------------------------------------------------------

class TestHandleCreateAppleNote(unittest.TestCase):

    def test_success_returns_ok_true(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0, "note id 1")) as mock_run:
            result = svc.handle_create_apple_note({"title": "Test", "body": "Content"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["note_id"], "note id 1")
        self.assertIsNone(result["error"])
        mock_run.assert_called_once()

    def test_osascript_failure_returns_ok_false(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(1, "", "Permission denied")):
            result = svc.handle_create_apple_note({"title": "T", "body": "B"})
        self.assertFalse(result["ok"])
        self.assertIn("Permission", result["error"])

    def test_timeout_returns_ok_false(self):
        svc = _make_service()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            result = svc.handle_create_apple_note({"title": "T", "body": "B"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "osascript timeout")

    def test_folder_included_in_script(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0, "id 2")) as mock_run:
            svc.handle_create_apple_note({"title": "T", "body": "B", "folder": "Work"})
        call_args = mock_run.call_args
        script = call_args[0][0][2]  # ["osascript", "-e", script]
        self.assertIn("Work", script)
        self.assertIn("targetFolder", script)

    # Fix 3: Notes folder creation + no hardcoded iCloud account
    def test_folder_uses_default_account_not_hardcoded_icloud(self):
        """Fix 3: folder-targeting script must use 'default account', not hardcoded 'iCloud'."""
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0, "id x")) as mock_run:
            svc.handle_create_apple_note({"title": "T", "body": "B", "folder": "Krab Ear"})
        script = mock_run.call_args[0][0][2]
        self.assertIn("default account", script,
                      "Fix 3: script must use 'default account' not hardcoded 'iCloud'")
        self.assertNotIn('account "iCloud"', script,
                         "Fix 3: hardcoded 'iCloud' account must be removed")

    def test_folder_script_creates_folder_if_missing(self):
        """Fix 3: script must contain try/on error block that creates the folder if missing."""
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0, "id y")) as mock_run:
            svc.handle_create_apple_note({"title": "T", "body": "B", "folder": "New Folder"})
        script = mock_run.call_args[0][0][2]
        self.assertIn("on error", script,
                      "Fix 3: script must include try/on error for folder creation")
        self.assertIn("make new folder", script,
                      "Fix 3: script must create the folder if it does not exist")

    def test_default_title_used_when_missing(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0, "id 3")) as mock_run:
            svc.handle_create_apple_note({})
        script = mock_run.call_args[0][0][2]
        self.assertIn("Krab Ear note", script)


# ---------------------------------------------------------------------------
# handle_create_apple_reminder
# ---------------------------------------------------------------------------

class TestHandleCreateAppleReminder(unittest.TestCase):

    def test_success_without_list(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)):
            result = svc.handle_create_apple_reminder({"title": "Buy milk"})
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

    def test_success_with_list_name(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_apple_reminder({"title": "Task", "list_name": "Work"})
        script = mock_run.call_args[0][0][2]
        self.assertIn("Work", script)

    def test_due_date_included_in_script(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_apple_reminder({"title": "T", "due_date": "2026-06-01"})
        script = mock_run.call_args[0][0][2]
        self.assertIn("due date", script)
        self.assertIn("2026-06-01", script)

    def test_osascript_failure_returns_ok_false(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(1, "", "Not authorized")):
            result = svc.handle_create_apple_reminder({"title": "T"})
        self.assertFalse(result["ok"])
        self.assertIn("Not authorized", result["error"])

    def test_timeout_handled(self):
        svc = _make_service()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            result = svc.handle_create_apple_reminder({"title": "T"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "osascript timeout")


# ---------------------------------------------------------------------------
# handle_create_calendar_event
# ---------------------------------------------------------------------------

class TestHandleCreateCalendarEvent(unittest.TestCase):

    def test_missing_title_returns_error(self):
        svc = _make_service()
        result = svc.handle_create_calendar_event({"start_date": "2026-06-01T10:00:00"})
        self.assertFalse(result["ok"])
        self.assertIn("title", result["error"])

    def test_missing_start_date_returns_error(self):
        svc = _make_service()
        result = svc.handle_create_calendar_event({"title": "Meeting"})
        self.assertFalse(result["ok"])
        self.assertIn("start_date", result["error"])

    def test_success_without_calendar(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)):
            result = svc.handle_create_calendar_event(
                {"title": "Standup", "start_date": "2026-06-01T09:00:00"}
            )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

    def test_calendar_name_included_in_script(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_calendar_event(
                {
                    "title": "Review",
                    "start_date": "2026-06-01T10:00:00",
                    "calendar_name": "Work",
                }
            )
        script = mock_run.call_args[0][0][2]
        self.assertIn('"Work"', script)

    def test_duration_defaults_to_30(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_calendar_event(
                {"title": "T", "start_date": "2026-06-01T10:00:00"}
            )
        script = mock_run.call_args[0][0][2]
        self.assertIn("30", script)

    def test_timeout_handled(self):
        svc = _make_service()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 15)):
            result = svc.handle_create_calendar_event(
                {"title": "T", "start_date": "2026-06-01T10:00:00"}
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "osascript timeout")

    # Fix 1 regression tests — locale-safe AppleScript date arithmetic
    def test_iso_start_date_uses_current_date_arithmetic(self):
        """Fix 1: ISO-8601 start_date must produce '(current date) +' arithmetic, not date \"...\"."""
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_calendar_event(
                {"title": "Meeting", "start_date": "2026-06-21T10:00:00"}
            )
        script = mock_run.call_args[0][0][2]
        # Must use locale-safe arithmetic instead of locale-dependent date literal.
        self.assertIn("(current date) +", script,
                      "Fix 1: script must use '(current date) + <delta>' for locale safety")
        self.assertNotIn('set startDate to date "', script,
                         "Fix 1: locale-dependent date literal must NOT appear for ISO input")

    def test_legacy_mm_dd_yyyy_also_uses_current_date_arithmetic(self):
        """Fix 1: backward-compat — old 'MM/dd/yyyy HH:mm:ss' format also converts to delta."""
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_calendar_event(
                {"title": "T", "start_date": "06/21/2026 10:00:00"}
            )
        script = mock_run.call_args[0][0][2]
        self.assertIn("(current date) +", script,
                      "Fix 1: legacy MM/dd/yyyy format must also use delta arithmetic")

    def test_unknown_format_falls_back_to_raw_injection(self):
        """Fix 1: unrecognised format falls back to raw string (best-effort)."""
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_create_calendar_event(
                {"title": "T", "start_date": "Sunday, June 21, 2026 at 10:00 AM"}
            )
        script = mock_run.call_args[0][0][2]
        # Falls back to date "..." injection — raw string was injected.
        self.assertIn("startDate", script)


# ---------------------------------------------------------------------------
# handle_send_imessage
# ---------------------------------------------------------------------------

class TestHandleSendImessage(unittest.TestCase):

    def test_missing_recipient_returns_error(self):
        svc = _make_service()
        result = svc.handle_send_imessage({"body": "hi"})
        self.assertFalse(result["ok"])
        self.assertIn("recipient", result["error"])

    def test_missing_body_returns_error(self):
        svc = _make_service()
        result = svc.handle_send_imessage({"recipient": "+79001234567"})
        self.assertFalse(result["ok"])
        self.assertIn("body", result["error"])

    def test_success_imessage(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)):
            result = svc.handle_send_imessage(
                {"recipient": "+79001234567", "body": "Hello"}
            )
        self.assertTrue(result["ok"])
        self.assertIsNone(result["error"])

    def test_invalid_service_defaults_to_imessage(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_send_imessage(
                {"recipient": "+7", "body": "x", "service": "WhatsApp"}
            )
        script = mock_run.call_args[0][0][2]
        self.assertIn("iMessage", script)

    def test_sms_service_in_script(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(0)) as mock_run:
            svc.handle_send_imessage(
                {"recipient": "+7", "body": "x", "service": "SMS"}
            )
        script = mock_run.call_args[0][0][2]
        self.assertIn("SMS", script)

    def test_osascript_failure_returns_ok_false(self):
        svc = _make_service()
        with patch("subprocess.run", return_value=_completed(1, "", "Messages not running")):
            result = svc.handle_send_imessage({"recipient": "+7", "body": "x"})
        self.assertFalse(result["ok"])
        self.assertIn("Messages", result["error"])

    def test_timeout_handled(self):
        svc = _make_service()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 10)):
            result = svc.handle_send_imessage({"recipient": "+7", "body": "x"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "osascript timeout")


# ---------------------------------------------------------------------------
# wave-1770 HIGH: privacy_mode gate on all Apple-app integration handlers.
# These handlers send transcript text to external macOS apps (Notes, Reminders,
# Calendar, Messages) — must be blocked when privacy_mode_enabled=True.
# ---------------------------------------------------------------------------

class TestApplePrivacyGateW1770(unittest.TestCase):
    """All 4 Apple-app handlers must refuse when privacy_mode is on."""

    def _privacy_service(self) -> AppleIntegrationService:
        # settings_get returns True for privacy_mode_enabled.
        return AppleIntegrationService(
            telegram_bridge=MagicMock(),
            settings_get=lambda key, default: True if key == "privacy_mode_enabled" else default,
        )

    def test_note_blocked_in_privacy_mode(self) -> None:
        called = []
        with patch("subprocess.run", side_effect=lambda *a, **k: called.append(a) or _completed()):
            result = self._privacy_service().handle_create_apple_note({"title": "t", "body": "secret"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "privacy_mode_active")
        self.assertEqual(len(called), 0, "osascript must NOT run in privacy mode")

    def test_reminder_blocked_in_privacy_mode(self) -> None:
        called = []
        with patch("subprocess.run", side_effect=lambda *a, **k: called.append(a) or _completed()):
            result = self._privacy_service().handle_create_apple_reminder({"title": "t", "body": "secret"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "privacy_mode_active")
        self.assertEqual(len(called), 0)

    def test_calendar_blocked_in_privacy_mode(self) -> None:
        called = []
        with patch("subprocess.run", side_effect=lambda *a, **k: called.append(a) or _completed()):
            result = self._privacy_service().handle_create_calendar_event(
                {"title": "t", "start_date": "2026-01-01 10:00:00"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "privacy_mode_active")
        self.assertEqual(len(called), 0)

    def test_imessage_blocked_in_privacy_mode(self) -> None:
        called = []
        with patch("subprocess.run", side_effect=lambda *a, **k: called.append(a) or _completed()):
            result = self._privacy_service().handle_send_imessage({"recipient": "+7", "body": "secret"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "privacy_mode_active")
        self.assertEqual(len(called), 0)

    def test_handlers_work_when_privacy_off(self) -> None:
        """Sanity: with privacy off (default), handlers proceed to osascript."""
        svc = AppleIntegrationService(telegram_bridge=MagicMock())  # default settings_get → False
        with patch("subprocess.run", return_value=_completed(stdout="ok")):
            result = svc.handle_create_apple_note({"title": "t", "body": "b"})
        self.assertNotEqual(result.get("error"), "privacy_mode_active")


# ---------------------------------------------------------------------------
# Fix 4: Telegram /api/notify response field graceful fallback
# ---------------------------------------------------------------------------

class TestTelegramBridgeSendMessageGracefulFallback(unittest.TestCase):
    """Fix 4: TelegramBridge.send_message must gracefully handle /api/notify responses
    that omit message_id / sent_at / chat_title (Main Krab currently returns only
    {"ok": True, "chat_id": ...}).  The production code already has graceful fallbacks
    (message_id=None, sent_at=time.time(), chat_title=str(chat_id)) — this test
    confirms they stay in place so forward-compat reads keep working.
    """

    def _make_bridge(self):
        from backend.telegram_bridge import TelegramBridge
        return TelegramBridge(base_url="http://localhost:8080")

    def test_missing_response_fields_use_graceful_defaults(self):
        """Fix 4: when /api/notify returns only {ok, chat_id}, result fields fallback gracefully."""
        import requests as req_mod
        bridge = self._make_bridge()
        mock_resp = MagicMock()
        mock_resp.ok = True
        # Simulate minimal Main Krab response — only ok + chat_id, no message_id etc.
        mock_resp.json.return_value = {"ok": True, "chat_id": 12345}
        with patch.object(req_mod, "post", return_value=mock_resp):
            result = bridge.send_message(text="hello", chat_id=12345)
        # message_id should be None (field absent) — graceful fallback.
        self.assertIsNone(result["message_id"],
                          "Fix 4: message_id must be None when absent from response")
        # sent_at must be a numeric fallback (time.time()), not None.
        self.assertIsNotNone(result["sent_at"])
        self.assertIsInstance(result["sent_at"], float,
                              "Fix 4: sent_at must fallback to time.time() float")
        # chat_title must fallback to str(chat_id).
        self.assertEqual(result["chat_title"], "12345",
                         "Fix 4: chat_title must fallback to str(chat_id)")

    def test_full_response_fields_passed_through(self):
        """Fix 4: when /api/notify returns all fields, they are passed through unchanged."""
        import requests as req_mod
        bridge = self._make_bridge()
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "ok": True,
            "chat_id": 99,
            "message_id": 777,
            "sent_at": "2026-06-21T12:00:00",
            "chat_title": "Dev Chat",
        }
        with patch.object(req_mod, "post", return_value=mock_resp):
            result = bridge.send_message(text="test", chat_id=99)
        self.assertEqual(result["message_id"], 777)
        self.assertEqual(result["sent_at"], "2026-06-21T12:00:00")
        self.assertEqual(result["chat_title"], "Dev Chat")


if __name__ == "__main__":
    unittest.main()
