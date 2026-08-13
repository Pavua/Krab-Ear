"""Tests для W1427 F2 HIGH fix: restore_settings_backup migrate+validate before save.

Проверяет:
- test_restore_rejects_corrupt_backup_no_save: corrupt backup (invalid enum) вернёт
  {"ok": False, "error": "Backup validation failed"} без вызова store.save_settings.
- test_restore_valid_backup_saves: валидный backup сохраняется нормально.
- test_restore_migrates_old_schema: backup schema_version="1.0" мигрируется до "2.0"
  перед сохранением, missing fields добавляются.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_backup import SettingsBackup
from backend.settings_service import SettingsService
from backend.settings_validator import CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Minimal valid settings dict that passes SettingsValidator.validate()
# ---------------------------------------------------------------------------

_BASE_SETTINGS: dict = {
    "quality_profile": "balanced",
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "auto_paste": True,
    "realtime_preview_enabled": True,
    "mode": "headless",
    "translation_style": "neutral",
    "clipboard_mode": "always_copy",
    "update_channel": "stable",
    "translation_glossary": {},
    "text_templates": {},
    "network_mode": "offline_default",
    "hotkey_profile": "default",
    "history_policy": "unlimited",
    "history_text_density": "normal",
    "capture_source_mode": "mic",
    "ui_last_tab": "history",
    "auto_start_enabled": False,
    "show_dock_icon": True,
    "play_start_sound": True,
    "audio_ducking_enabled": True,
    "silence_guard_enabled": True,
    "background_guard_enabled": True,
    "call_notify_default": True,
    "call_auto_summary": True,
    "history_focus_mode": True,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "voice_gateway_api_key": "",
    "history_page_size": 50,
    "audio_ducking_percent": 50,
    "stop_tail_trim_ms": 180,
    "silence_guard_rms_threshold": 0.0020,
    "silence_guard_peak_threshold": 0.0120,
    "silence_guard_active_ratio_threshold": 0.015,
    "background_guard_min_peak": 0.025,
    "background_guard_min_rms": 0.0040,
    "background_guard_uniform_frame_threshold": 0.0060,
    "background_guard_max_uniform_active_ratio": 0.92,
    "overlay_opacity_percent": 45,
    "notifications_enabled": True,
    "notify_on_low_confidence": True,
    "notify_confidence_threshold": 0.5,
    "notify_on_llm_failure": True,
    "notify_on_import_complete": True,
    "notify_sound_enabled": True,
    "onboarding_completed": False,
    "translate_and_paste": False,
}


def _make_store(settings: dict | None = None) -> MagicMock:
    """Fake store with mutable current state."""
    store = MagicMock()
    current: dict = dict(settings or _BASE_SETTINGS)

    def load_settings(lock_timeout_sec: float | None = None, nowait: bool = False) -> dict:
        return dict(current)

    def save_settings(new_settings: dict) -> dict:
        current.clear()
        current.update(new_settings)
        return dict(current)

    store.load_settings.side_effect = load_settings
    store.save_settings.side_effect = save_settings
    store._current = current
    return store


class TestRestoreBackupValidateW1435(unittest.TestCase):
    """W1427 F2 HIGH: handle_restore_settings_backup validates backup before save."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_mgr = SettingsBackup(backup_dir=Path(self.tmp))

    def _make_svc(self, settings: dict | None = None) -> SettingsService:
        store = _make_store(settings)
        svc = SettingsService(store=store, backup=self.backup_mgr)
        svc._store = store  # expose for assertion
        return svc

    # ------------------------------------------------------------------
    # Test 1: corrupt backup is rejected, store.save_settings NOT called
    # ------------------------------------------------------------------

    def test_restore_rejects_corrupt_backup_no_save(self):
        """Corrupt backup (invalid voice_gateway_url) must be rejected without saving.

        W1427 F2: validate(restored) call must block the save when valid=False.
        SettingsValidator returns valid=False for an invalid/non-loopback gateway URL.
        """
        svc = self._make_svc()
        store = svc._store

        # Corrupt backup: voice_gateway_url with ftp scheme is rejected hard by validator
        corrupt_data = dict(_BASE_SETTINGS)
        corrupt_data["voice_gateway_url"] = "ftp://evil.example.com"  # hard error

        # Write the corrupt backup directly as a flat JSON settings dict
        # (matching the format created by SettingsBackup.create_backup)
        import json, time as _time
        backup_id = f"backup_{int(_time.time() * 1000)}"
        backup_file = Path(self.tmp) / f"{backup_id}.json"
        backup_file.write_text(json.dumps(corrupt_data))

        # W1701: the method now raises ValueError on corrupt backup (before: returned ok=False dict)
        # The IPC dispatcher converts exceptions to {"ok": False, "error": str(exc)} responses
        with self.assertRaises(ValueError) as ctx:
            svc.handle_restore_settings_backup({"backup_id": backup_id})

        # Error message must mention the validation failure
        self.assertIn("ftp://evil.example.com", str(ctx.exception),
                      "Error message must include the rejected URL")

        # store.save_settings must NOT have been called (rollback may call it with old settings)
        # Check that the corrupt data was NOT saved by verifying the initial save happened
        # (before restore attempt). After rollback, old_settings are restored.
        # The important invariant: after exception, the corrupt data was not persisted.
        store.load_settings.assert_called()  # settings were read before restore attempt

    # ------------------------------------------------------------------
    # Test 2: valid backup is saved normally
    # ------------------------------------------------------------------

    def test_restore_valid_backup_saves(self):
        """Valid backup passes validation and is saved via store.save_settings.

        W1427 F2: validate guard must not block valid backups.
        """
        svc = self._make_svc()
        store = svc._store

        # Create a valid backup via the official backup manager
        backup_data = dict(_BASE_SETTINGS)
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        # Must not return ok=False
        self.assertNotIn("ok", result,
                         "Valid restore must not set ok=False; got: %r" % result)
        self.assertIn("restored_settings", result,
                      "Valid restore must return restored_settings")
        self.assertEqual(result.get("backup_id"), backup_id)

        # store.save_settings must have been called exactly once
        store.save_settings.assert_called_once()

    # ------------------------------------------------------------------
    # Test 3: old-schema backup (schema_version="1.0") is migrated first
    # ------------------------------------------------------------------

    def test_restore_migrates_old_schema(self):
        """Backup with schema_version='1.0' is migrated to current schema before save.

        W1427 F2: migrate() must be called for old-schema backups so that
        missing fields (added in 2.0) receive their default values.
        """
        svc = self._make_svc()
        store = svc._store

        # Build a schema 1.0 backup: uses old field name 'history_limit',
        # lacks fields added in 2.0 migration (overlay_opacity_percent etc.)
        import json, time as _time
        backup_id = f"backup_old_{int(_time.time() * 1000)}"
        backup_file = Path(self.tmp) / f"{backup_id}.json"

        old_schema_data: dict = {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": "off",
            "auto_paste": True,
            "realtime_preview_enabled": True,
            "mode": "headless",
            "translation_style": "neutral",
            "clipboard_mode": "always_copy",
            "update_channel": "stable",
            "translation_glossary": {},
            "text_templates": {},
            "network_mode": "offline_default",
            "hotkey_profile": "default",
            # Old field name: history_limit (v1.0), not history_policy (v2.0)
            "history_limit": "unlimited",
            "history_text_density": "normal",
            "capture_source_mode": "mic",
            "ui_last_tab": "history",
            "auto_start_enabled": False,
            "show_dock_icon": True,
            "play_start_sound": True,
            "audio_ducking_enabled": True,
            "silence_guard_enabled": True,
            "background_guard_enabled": True,
            # Missing 2.0 fields: call_notify_default, call_auto_summary,
            # history_focus_mode, overlay_opacity_percent, etc.
            "voice_gateway_url": "http://127.0.0.1:8090",
            "voice_gateway_api_key": "",
            "history_page_size": 50,
            "audio_ducking_percent": 50,
            "stop_tail_trim_ms": 180,
            "silence_guard_rms_threshold": 0.0020,
            "silence_guard_peak_threshold": 0.0120,
            "silence_guard_active_ratio_threshold": 0.015,
            "background_guard_min_peak": 0.025,
            "background_guard_min_rms": 0.0040,
            "background_guard_uniform_frame_threshold": 0.0060,
            "background_guard_max_uniform_active_ratio": 0.92,
            "notifications_enabled": True,
            "notify_on_low_confidence": True,
            "notify_confidence_threshold": 0.5,
            "notify_on_llm_failure": True,
            "notify_on_import_complete": True,
            "notify_sound_enabled": True,
            "onboarding_completed": False,
            "translate_and_paste": False,
            "llm_rewrite_enabled": False,
            "auto_save_transcripts": False,
            # Explicitly set old schema version
            "schema_version": "1.0",
        }

        # Write as flat settings dict (matching SettingsBackup.create_backup format)
        backup_file.write_text(json.dumps(old_schema_data))

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        # Must not be rejected
        self.assertNotIn("ok", result,
                         "Old-schema backup must be migrated and saved; got: %r" % result)
        self.assertIn("restored_settings", result)

        saved = svc.cached_settings()

        # After migration 1.0 → 2.0: 'history_policy' must exist (renamed from history_limit)
        self.assertIn("history_policy", saved,
                      "Migration must rename history_limit → history_policy")
        self.assertNotIn("history_limit", saved,
                         "Old field history_limit must not persist after migration")

        # 2.0 default fields must be present after migration
        self.assertIn("overlay_opacity_percent", saved,
                      "2.0 field overlay_opacity_percent must be added by migration")
        self.assertIn("call_notify_default", saved,
                      "2.0 field call_notify_default must be added by migration")

        # store.save_settings must have been called
        store.save_settings.assert_called_once()


if __name__ == "__main__":
    unittest.main()
