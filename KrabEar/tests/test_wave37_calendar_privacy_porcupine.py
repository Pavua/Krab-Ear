"""Wave-37 security tests.

Covers:
  C1 (MED) — _handle_get_calendar_link returns ok:False with reason:privacy_mode_active
              when privacy_mode_enabled is True.
  C2 (MED) — _handle_search_by_calendar_event same gate.
  C3 (LOW) — porcupine_access_key is in SENSITIVE_FIELDS (backup redaction).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.settings_backup import SENSITIVE_FIELDS, SettingsBackup  # noqa: E402
from backend.translator import TranslationResult  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal fakes (same pattern as test_calendar_linker_ipc_w947.py)
# ---------------------------------------------------------------------------

class _FakeEngine:
    def __init__(self):
        self.cleanup_profile = "soft"
        self.quality_profile = "balanced"


class _FakeRecorder:
    def __init__(self):
        self.is_recording = False
        self.sample_rate = 16000
        self.last_stop_trim_ms = 0
        self.last_stop_timeout_sec = 3.0

    def start(self):
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        self.is_recording = False
        return None


class _FakeTranscriber:
    def __init__(self):
        self.counter = 0
        self.engine = _FakeEngine()

    def transcribe(self, audio_data, quality_profile="balanced", cleanup_profile="soft",
                   domain="casual", extra_vocabulary=None, lang_hint=None,
                   history_context=None, stt_hotwords=None):
        self.counter += 1
        return f"test #{self.counter}"

    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return "preview"


class _FakeTranslator:
    def __init__(self):
        self.last_mode = "off"

    def translate(self, text, mode, network_mode, translation_style="neutral", glossary=None):
        self.last_mode = mode
        return TranslationResult(
            text="", status="not_requested", source_lang="", target_lang="",
            mode="off", engine="fake",
        )


def _make_service():
    """Return a BackendService instance using fakes and a temp StateStore."""
    tmp = Path(tempfile.mkdtemp())
    from backend.state_store import StateStore
    from backend.service import BackendService

    store = StateStore(data_dir=tmp)
    svc = BackendService(
        store=store,
        recorder=_FakeRecorder(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
    )
    return svc, store, tmp


# ---------------------------------------------------------------------------
# C1 — _handle_get_calendar_link privacy gate
# ---------------------------------------------------------------------------

class TestGetCalendarLinkPrivacyGate(unittest.TestCase):
    """get_calendar_link must return ok:False when privacy_mode_enabled is True."""

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service()

    def _privacy_on(self, key, default=None):
        if key == "privacy_mode_enabled":
            return True
        return default

    def test_privacy_mode_blocks_get_calendar_link(self):
        with patch.object(self.svc, "_get_runtime_setting", side_effect=self._privacy_on):
            result = self.svc._handle_get_calendar_link({"history_item_id": "any-id"})
        self.assertFalse(result.get("ok"), "ok must be False in privacy mode")
        self.assertEqual(result.get("reason"), "privacy_mode_active",
                         "reason must be privacy_mode_active")

    def test_privacy_mode_via_handle_request(self):
        """Gate also fires when called through the dispatch table.

        handle_request wraps the handler return as {"ok": True, "result": <handler_result>},
        so the privacy gate response is nested under the "result" key.
        """
        with patch.object(self.svc, "_get_runtime_setting", side_effect=self._privacy_on):
            response = self.svc.handle_request({
                "id": "t", "method": "get_calendar_link",
                "params": {"history_item_id": "any-id"},
            })
        inner = response.get("result", {})
        self.assertFalse(inner.get("ok"), "inner ok must be False in privacy mode")
        self.assertEqual(inner.get("reason"), "privacy_mode_active")

    def test_no_privacy_mode_returns_calendar_event_key(self):
        """Without privacy mode the handler returns the calendar_event key."""
        # store has no calendar link for 'no-item' → returns ok:True, calendar_event:None
        def _privacy_off(key, default=None):
            if key == "privacy_mode_enabled":
                return False
            return default

        with patch.object(self.svc, "_get_runtime_setting", side_effect=_privacy_off):
            result = self.svc._handle_get_calendar_link({"history_item_id": "no-item"})
        self.assertIn("calendar_event", result,
                      "calendar_event key must be present when privacy mode is off")


# ---------------------------------------------------------------------------
# C2 — _handle_search_by_calendar_event privacy gate
# ---------------------------------------------------------------------------

class TestSearchByCalendarEventPrivacyGate(unittest.TestCase):
    """search_by_calendar_event must return ok:False when privacy_mode_enabled is True."""

    def setUp(self):
        self.svc, self.store, self.tmp = _make_service()

    def _privacy_on(self, key, default=None):
        if key == "privacy_mode_enabled":
            return True
        return default

    def test_privacy_mode_blocks_search_by_calendar_event(self):
        with patch.object(self.svc, "_get_runtime_setting", side_effect=self._privacy_on):
            result = self.svc._handle_search_by_calendar_event({"event_title": "Stand-up"})
        self.assertFalse(result.get("ok"), "ok must be False in privacy mode")
        self.assertEqual(result.get("reason"), "privacy_mode_active")

    def test_privacy_mode_via_handle_request(self):
        """Gate fires via dispatch table; inner result carries ok:False."""
        with patch.object(self.svc, "_get_runtime_setting", side_effect=self._privacy_on):
            response = self.svc.handle_request({
                "id": "t", "method": "search_by_calendar_event",
                "params": {"event_title": "Stand-up"},
            })
        inner = response.get("result", {})
        self.assertFalse(inner.get("ok"))
        self.assertEqual(inner.get("reason"), "privacy_mode_active")

    def test_no_privacy_mode_returns_results_key(self):
        """Without privacy mode the handler returns results list."""
        def _privacy_off(key, default=None):
            if key == "privacy_mode_enabled":
                return False
            return default

        with patch.object(self.svc, "_get_runtime_setting", side_effect=_privacy_off):
            result = self.svc._handle_search_by_calendar_event({"event_title": "Stand-up"})
        self.assertIn("results", result,
                      "results key must be present when privacy mode is off")


# ---------------------------------------------------------------------------
# C3 — porcupine_access_key in SENSITIVE_FIELDS
# ---------------------------------------------------------------------------

class TestPorcupineKeyInSensitiveFields(unittest.TestCase):
    """porcupine_access_key must be in SENSITIVE_FIELDS frozenset."""

    def test_porcupine_access_key_in_sensitive_fields(self):
        self.assertIn(
            "porcupine_access_key",
            SENSITIVE_FIELDS,
            "porcupine_access_key must be redacted (Picovoice credential)",
        )

    def test_porcupine_key_not_written_to_backup_file(self):
        """porcupine_access_key must not appear in the backup JSON on disk."""
        tmp = tempfile.mkdtemp()
        backup = SettingsBackup(backup_dir=Path(tmp))
        backup_id = backup.create_backup({
            "quality_profile": "balanced",
            "porcupine_access_key": "top-secret-picovoice-token",
        }, reason="test")
        path = Path(tmp) / f"{backup_id}.json"
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertNotIn("porcupine_access_key", data,
                         "porcupine_access_key must be redacted from backup")
        # non-sensitive key must still be present
        self.assertIn("quality_profile", data)

    def test_porcupine_key_not_in_restore(self):
        """restore_backup round-trip must not return porcupine_access_key."""
        tmp = tempfile.mkdtemp()
        backup = SettingsBackup(backup_dir=Path(tmp))
        backup_id = backup.create_backup({
            "mode": "headless",
            "porcupine_access_key": "exposed-key",
        })
        restored = backup.restore_backup(backup_id)
        self.assertNotIn("porcupine_access_key", restored)


if __name__ == "__main__":
    unittest.main()
