"""A5.3: privacy принимает только exact bool; обычные toggles сохраняют bool-coerce.

Старый W1174 защищал от truthy строки "false" посредством нормализации.
Теперь malformed policy отклоняется до normalizer и до любой записи.
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
from backend.state_store import StateStoreSettingsCorruptError  # noqa: E402
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

    def _save(s: dict, **kwargs) -> dict:
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


class TestPrivacyModeExactBool(unittest.TestCase):
    """Граница IPC отклоняет malformed policy, не меняя существующие значения."""

    def test_non_bool_policy_values_are_rejected_without_save(self):
        for value in ("false", "true", 0, 1, "off", "on", "0", "1", "no", "yes", None):
            with self.subTest(value=value):
                store = _make_store()
                svc = SettingsService(store=store)
                before = dict(store._current)
                with self.assertRaises(StateStoreSettingsCorruptError):
                    svc.handle_set_settings({"privacy_mode_enabled": value})
                store.save_settings.assert_not_called()
                self.assertEqual(store._current, before)

    def test_native_bool_policy_values_remain_exact_bool(self):
        for value in (False, True):
            with self.subTest(value=value):
                store = _make_store()
                svc = SettingsService(store=store)
                svc.handle_set_settings({"privacy_mode_enabled": value})
                self.assertIs(store._saved[0]["privacy_mode_enabled"], value)


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
