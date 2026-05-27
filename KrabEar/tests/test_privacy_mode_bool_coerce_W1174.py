"""W1174: privacy_mode_enabled bool-coerce fix tests.

Covers W1168 F4 HIGH — privacy_mode_enabled was missing from:
  - SettingsService.handle_set_settings bool-coerce block
  - SettingsValidator._BOOL_FIELDS

Result of that bug: client sending JSON "false" (string) → settings.json
stored "false" (string) → Python truthy → privacy mode stuck permanently ON
with no way to turn it off via API.

Test cases:
  - test_privacy_mode_enabled_false_string_coerces_to_false
  - test_privacy_mode_enabled_true_string_coerces_to_true
  - test_privacy_mode_enabled_in_bool_fields_validator
  - test_privacy_mode_enabled_numeric_zero_coerces_to_false
  - test_privacy_mode_enabled_numeric_one_coerces_to_true
  - test_privacy_mode_enabled_off_string_coerces_to_false
  - test_privacy_mode_enabled_on_string_coerces_to_true
  - test_llm_rewrite_enabled_false_string_coerces_to_false
  - test_auto_save_transcripts_false_string_coerces_to_false
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402


def _make_store(extra: dict | None = None) -> MagicMock:
    """Minimal store stub that satisfies SettingsService."""
    current: dict = {
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
        "stt_hotwords_enabled": True,
        "stt_hotwords": [],
        "privacy_mode_enabled": False,
        "llm_rewrite_enabled": False,
        "auto_save_transcripts": False,
    }
    if extra:
        current.update(extra)

    store = MagicMock()
    store.load_settings.return_value = dict(current)

    saved_holder: list[dict] = []

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        saved_holder.clear()
        saved_holder.append(dict(s))
        return dict(s)

    store.save_settings.side_effect = _save
    store._saved = saved_holder
    store._current = current
    return store


class TestPrivacyModeBoolCoerce(unittest.TestCase):
    """Test that privacy_mode_enabled is properly coerced in handle_set_settings."""

    def _set_and_read(self, value) -> bool:
        """Call handle_set_settings with privacy_mode_enabled=value and return stored bool."""
        store = _make_store()
        svc = SettingsService(store=store)
        # Prime cache
        svc.cached_settings()
        svc.handle_set_settings({"privacy_mode_enabled": value})
        saved = store._saved[0]
        return saved["privacy_mode_enabled"]

    def test_privacy_mode_enabled_false_string_coerces_to_false(self):
        """String 'false' must be coerced to False — the core bug from W1168 F4."""
        result = self._set_and_read("false")
        self.assertIs(result, False)
        self.assertIsInstance(result, bool)

    def test_privacy_mode_enabled_true_string_coerces_to_true(self):
        """String 'true' must be coerced to True."""
        result = self._set_and_read("true")
        self.assertIs(result, True)
        self.assertIsInstance(result, bool)

    def test_privacy_mode_enabled_numeric_zero_coerces_to_false(self):
        """Integer 0 must coerce to False."""
        result = self._set_and_read(0)
        self.assertIs(result, False)

    def test_privacy_mode_enabled_numeric_one_coerces_to_true(self):
        """Integer 1 must coerce to True."""
        result = self._set_and_read(1)
        self.assertIs(result, True)

    def test_privacy_mode_enabled_off_string_coerces_to_false(self):
        """String 'off' must coerce to False."""
        result = self._set_and_read("off")
        self.assertIs(result, False)

    def test_privacy_mode_enabled_on_string_coerces_to_true(self):
        """String 'on' must coerce to True."""
        result = self._set_and_read("on")
        self.assertIs(result, True)

    def test_privacy_mode_enabled_zero_string_coerces_to_false(self):
        """String '0' must coerce to False."""
        result = self._set_and_read("0")
        self.assertIs(result, False)

    def test_privacy_mode_enabled_one_string_coerces_to_true(self):
        """String '1' must coerce to True."""
        result = self._set_and_read("1")
        self.assertIs(result, True)

    def test_privacy_mode_enabled_bool_false_stays_false(self):
        """Native bool False must stay False."""
        result = self._set_and_read(False)
        self.assertIs(result, False)

    def test_privacy_mode_enabled_bool_true_stays_true(self):
        """Native bool True must stay True."""
        result = self._set_and_read(True)
        self.assertIs(result, True)

    def test_privacy_mode_enabled_no_string_is_not_truthy(self):
        """String 'no' must coerce to False, not be truthy."""
        result = self._set_and_read("no")
        self.assertIs(result, False)

    def test_privacy_mode_enabled_yes_string_coerces_to_true(self):
        """String 'yes' must coerce to True."""
        result = self._set_and_read("yes")
        self.assertIs(result, True)


class TestPrivacyModeBoolInValidator(unittest.TestCase):
    """Test that privacy_mode_enabled is in SettingsValidator._BOOL_FIELDS."""

    def test_privacy_mode_enabled_in_bool_fields_validator(self):
        """privacy_mode_enabled must appear in _BOOL_FIELDS in settings_validator module."""
        from backend import settings_validator as sv_module
        self.assertIn(
            "privacy_mode_enabled",
            sv_module._BOOL_FIELDS,
            "privacy_mode_enabled must be in _BOOL_FIELDS so the validator coerces it",
        )

    def test_privacy_mode_enabled_default_is_false_in_validator(self):
        """The default for privacy_mode_enabled in _BOOL_FIELDS must be False."""
        from backend import settings_validator as sv_module
        self.assertIs(
            sv_module._BOOL_FIELDS["privacy_mode_enabled"],
            False,
        )

    def test_stt_hotwords_enabled_in_bool_fields_validator(self):
        """stt_hotwords_enabled must appear in _BOOL_FIELDS."""
        from backend import settings_validator as sv_module
        self.assertIn("stt_hotwords_enabled", sv_module._BOOL_FIELDS)

    def test_validator_coerces_privacy_mode_false_string(self):
        """SettingsValidator.validate() must coerce privacy_mode_enabled='false' → False."""
        validator = SettingsValidator()
        result = validator.validate({"privacy_mode_enabled": "false"})
        self.assertIs(result.fixed["privacy_mode_enabled"], False)
        self.assertIsInstance(result.fixed["privacy_mode_enabled"], bool)

    def test_validator_coerces_privacy_mode_true_string(self):
        """SettingsValidator.validate() must coerce privacy_mode_enabled='true' → True."""
        validator = SettingsValidator()
        result = validator.validate({"privacy_mode_enabled": "true"})
        self.assertIs(result.fixed["privacy_mode_enabled"], True)

    def test_validator_coerces_privacy_mode_zero_int(self):
        """SettingsValidator.validate() must coerce privacy_mode_enabled=0 → False."""
        validator = SettingsValidator()
        result = validator.validate({"privacy_mode_enabled": 0})
        self.assertIs(result.fixed["privacy_mode_enabled"], False)

    def test_validator_coerces_privacy_mode_one_int(self):
        """SettingsValidator.validate() must coerce privacy_mode_enabled=1 → True."""
        validator = SettingsValidator()
        result = validator.validate({"privacy_mode_enabled": 1})
        self.assertIs(result.fixed["privacy_mode_enabled"], True)


class TestOtherBoolFieldCoerce(unittest.TestCase):
    """Test llm_rewrite_enabled and auto_save_transcripts coercion in handle_set_settings."""

    def _set_and_read_field(self, field: str, value) -> bool:
        store = _make_store()
        svc = SettingsService(store=store)
        svc.cached_settings()
        svc.handle_set_settings({field: value})
        return store._saved[0][field]

    def test_llm_rewrite_enabled_false_string_coerces_to_false(self):
        """String 'false' for llm_rewrite_enabled must coerce to False."""
        result = self._set_and_read_field("llm_rewrite_enabled", "false")
        self.assertIs(result, False)
        self.assertIsInstance(result, bool)

    def test_llm_rewrite_enabled_true_string_coerces_to_true(self):
        """String 'true' for llm_rewrite_enabled must coerce to True."""
        result = self._set_and_read_field("llm_rewrite_enabled", "true")
        self.assertIs(result, True)

    def test_auto_save_transcripts_false_string_coerces_to_false(self):
        """String 'false' for auto_save_transcripts must coerce to False."""
        result = self._set_and_read_field("auto_save_transcripts", "false")
        self.assertIs(result, False)
        self.assertIsInstance(result, bool)

    def test_auto_save_transcripts_true_string_coerces_to_true(self):
        """String 'true' for auto_save_transcripts must coerce to True."""
        result = self._set_and_read_field("auto_save_transcripts", "true")
        self.assertIs(result, True)


if __name__ == "__main__":
    unittest.main()
