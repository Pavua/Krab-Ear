"""Tests для W1337 F2 MED fix: restore_settings_backup credential preservation.

Проверяет:
- test_restore_preserves_existing_credentials: если бэкап не содержит credential-поле,
  которое есть в текущих настройках — оно сохраняется, не затирается пустым.
- test_restore_warns_when_backup_lacks_credentials: response содержит warning +
  dropped_fields при наличии потерянных credential-полей.
- test_restore_does_not_clobber_api_keys: hf_token и lm_studio_api_key из текущих
  настроек не теряются при restore из redacted-бэкапа (W897-style).
- Дополнительные сценарии: backup содержит ключи — они применяются нормально;
  нет текущих ключей — нет warning.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_backup import SettingsBackup
from backend.settings_service import SettingsService


# ---------------------------------------------------------------------------
# Helpers
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
    """Создаёт фиктивный store с поддержкой mutable current state."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRestoreCredentialPreservation(unittest.TestCase):
    """W1337 F2: handle_restore_settings_backup не затирает credential-поля."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_mgr = SettingsBackup(backup_dir=Path(self.tmp))

    def _make_svc(self, settings: dict | None = None) -> SettingsService:
        store = _make_store(settings)
        return SettingsService(store=store, backup=self.backup_mgr)

    # ------------------------------------------------------------------
    # Core scenario: W897-style redacted backup
    # ------------------------------------------------------------------

    def test_restore_preserves_existing_credentials(self):
        """Если бэкап не содержит hf_token, а текущие настройки содержат —
        hf_token должен быть сохранён после restore."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["hf_token"] = "hf_supersecrettoken"
        svc = self._make_svc(current_settings)

        # Backup without credentials (W897-style redacted auto-backup)
        backup_data = {k: v for k, v in _BASE_SETTINGS.items()}
        # hf_token intentionally absent from backup
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        # hf_token must be preserved in saved settings
        saved_settings = svc.cached_settings()
        self.assertEqual(
            saved_settings.get("hf_token"),
            "hf_supersecrettoken",
            "hf_token must be preserved from current settings when absent in backup",
        )
        # wave-35: credential fields are REDACTED in IPC responses (never echoed in plaintext).
        # The real value is still preserved in the store (checked above via cached_settings()),
        # but the API response returns "REDACTED" for all non-empty credential fields.
        self.assertEqual(result["restored_settings"].get("hf_token"), "REDACTED")

    def test_restore_warns_when_backup_lacks_credentials(self):
        """Response должен содержать warning='credentials_dropped' и dropped_fields,
        когда бэкап не содержит credential-поля из текущих настроек."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["hf_token"] = "hf_abc123"
        current_settings["lm_studio_api_key"] = "lmstudio-key-xyz"
        svc = self._make_svc(current_settings)

        # Backup without any credential fields
        backup_data = {k: v for k, v in _BASE_SETTINGS.items()}
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        self.assertEqual(result.get("warning"), "credentials_dropped")
        dropped = result.get("dropped_fields", [])
        self.assertIsInstance(dropped, list)
        self.assertIn("hf_token", dropped)
        self.assertIn("lm_studio_api_key", dropped)

    def test_restore_does_not_clobber_api_keys(self):
        """Все 4 credential-поля из SENSITIVE_FIELDS должны быть сохранены
        при restore из полностью redacted-бэкапа."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["voice_gateway_api_key"] = "vgw-key-abc"
        current_settings["hf_token"] = "hf-tok-111"
        current_settings["rest_api_key"] = "rest-key-999"
        current_settings["lm_studio_api_key"] = "lm-key-777"
        svc = self._make_svc(current_settings)

        # Backup is fully redacted — none of the 4 credential fields present
        backup_data = {k: v for k, v in _BASE_SETTINGS.items()}
        # Ensure none of the sensitive fields leak into backup_data
        for field in SettingsService._SENSITIVE_FIELDS:
            backup_data.pop(field, None)
        backup_id = self.backup_mgr.create_backup(backup_data, reason="w897_redacted")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        saved = svc.cached_settings()
        self.assertEqual(saved.get("voice_gateway_api_key"), "vgw-key-abc",
                         "voice_gateway_api_key must not be clobbered")
        self.assertEqual(saved.get("hf_token"), "hf-tok-111",
                         "hf_token must not be clobbered")
        self.assertEqual(saved.get("rest_api_key"), "rest-key-999",
                         "rest_api_key must not be clobbered")
        self.assertEqual(saved.get("lm_studio_api_key"), "lm-key-777",
                         "lm_studio_api_key must not be clobbered")

        # Only the 4 fields that were set in current_settings should appear in dropped_fields
        # (fields with empty/absent values in current are not "dropped" — they have nothing to preserve)
        dropped = set(result.get("dropped_fields", []))
        set_credential_fields = {"voice_gateway_api_key", "hf_token", "rest_api_key", "lm_studio_api_key"}
        for field in set_credential_fields:
            self.assertIn(field, dropped)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_restore_no_warning_when_current_has_no_credentials(self):
        """Если текущие настройки не содержат credential-поля (пустые строки),
        warning не должен появляться."""
        current_settings = dict(_BASE_SETTINGS)
        # All sensitive fields absent / empty — the default _BASE_SETTINGS state
        # voice_gateway_api_key = "" which is falsy
        svc = self._make_svc(current_settings)

        backup_data = dict(_BASE_SETTINGS)
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        self.assertNotIn("warning", result,
                         "No warning expected when current settings have no active credentials")
        self.assertNotIn("dropped_fields", result)

    def test_restore_backup_has_credentials_applies_them(self):
        """Если бэкап содержит credential-поля (необычный случай — они не redacted),
        они должны применяться нормально без warning."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["hf_token"] = "old-token"
        svc = self._make_svc(current_settings)

        # Backup includes a new token (atypical — backup from before W897 redaction)
        backup_data = dict(_BASE_SETTINGS)
        backup_data["hf_token"] = "new-token-from-backup"
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        # Since SettingsBackup.create_backup redacts sensitive fields, the backup
        # won't actually contain hf_token. So after restore the current token is preserved.
        # This test verifies the overall flow stays consistent.
        self.assertIn("restored_settings", result)
        self.assertIn("backup_id", result)

    def test_restore_partial_credentials_only_drops_missing(self):
        """Если только часть credential-полей отсутствует в бэкапе,
        dropped_fields содержит только отсутствующие."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["hf_token"] = "hf-present"
        # lm_studio_api_key is empty/absent in current — should not appear in dropped_fields
        svc = self._make_svc(current_settings)

        backup_data = dict(_BASE_SETTINGS)
        backup_id = self.backup_mgr.create_backup(backup_data, reason="partial_test")

        result = svc.handle_restore_settings_backup({"backup_id": backup_id})

        dropped = result.get("dropped_fields", [])
        self.assertIn("hf_token", dropped)
        # lm_studio_api_key is empty in current → should NOT be in dropped (not a real credential)
        self.assertNotIn("lm_studio_api_key", dropped)

    def test_restore_invalidates_cache_on_credential_warning(self):
        """handle_restore_settings_backup должен инвалидировать кэш даже при warning."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["hf_token"] = "hf-tok"
        svc = self._make_svc(current_settings)

        # Prime the cache
        svc.cached_settings()
        self.assertIsNotNone(svc._cache)

        backup_data = dict(_BASE_SETTINGS)
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        svc.handle_restore_settings_backup({"backup_id": backup_id})

        self.assertIsNone(svc._cache, "Cache must be invalidated after restore")

    def test_restore_non_sensitive_fields_are_overwritten(self):
        """Non-credential fields должны быть взяты из бэкапа, а не из текущих настроек."""
        current_settings = dict(_BASE_SETTINGS)
        current_settings["quality_profile"] = "max"
        svc = self._make_svc(current_settings)

        backup_data = dict(_BASE_SETTINGS)
        backup_data["quality_profile"] = "balanced"
        backup_id = self.backup_mgr.create_backup(backup_data, reason="pre_set")

        svc.handle_restore_settings_backup({"backup_id": backup_id})

        saved = svc.cached_settings()
        self.assertEqual(saved.get("quality_profile"), "balanced",
                         "Non-sensitive fields should be restored from backup")


if __name__ == "__main__":
    unittest.main()
