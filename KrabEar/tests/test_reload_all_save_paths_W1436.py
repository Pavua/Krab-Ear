"""W1436 — reload_settings_from_json called in all 5 save paths.

Tests:
- test_apply_profile_preset_reloads_settings
- test_import_settings_reloads_settings
- test_set_notification_preferences_reloads_settings
- test_restore_settings_backup_reloads_settings
- test_set_settings_still_reloads_settings  (regression: original path still reloads)
- test_reload_error_does_not_block_hooks    (exception swallowed, hooks still run)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402


_BASE_SETTINGS: dict = {
    "quality_profile": "balanced",
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "auto_paste": True,
    "translate_and_paste": False,
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
    "stt_hotwords": [],
    "stt_hotwords_enabled": True,
    "onboarding_completed": False,
}


def _make_store(extra: dict | None = None) -> MagicMock:
    """Create a fake store with in-memory settings persistence."""
    store = MagicMock()
    current: dict = dict(_BASE_SETTINGS)
    if extra:
        current.update(extra)

    store.load_settings.return_value = dict(current)

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        return dict(s)

    store.save_settings.side_effect = _save
    return store


def _make_backup(restore_data: dict | None = None) -> MagicMock:
    backup = MagicMock()
    backup.restore_backup.return_value = dict(restore_data or _BASE_SETTINGS)
    backup.create_backup.return_value = "backup-001"
    return backup


def _make_svc(store: MagicMock | None = None, backup: MagicMock | None = None) -> SettingsService:
    store = store or _make_store()
    backup = backup or _make_backup()
    svc = SettingsService(store=store, backup=backup)
    # Patch the internal validator so import_settings doesn't reject our keys.
    vr = MagicMock()
    vr.valid = True
    vr.errors = []
    vr.warnings = []
    vr.fixed = dict(_BASE_SETTINGS)
    svc._validator = MagicMock()
    svc._validator.validate.return_value = vr
    return svc


_RELOAD_TARGET = "core.config.reload_settings_from_json"


class TestApplyProfilePresetReloadsSettings(unittest.TestCase):
    """apply_profile_preset must call reload_settings_from_json before hooks."""

    def test_reload_called(self):
        svc = _make_svc()
        with patch(_RELOAD_TARGET, return_value=3) as mock_reload:
            svc.handle_apply_profile_preset({"profile": "default"})
        mock_reload.assert_called_once()

    def test_reload_called_before_hooks(self):
        """Verify call order: save → reload → hooks."""
        call_order: list[str] = []
        svc = _make_svc()

        def _hook(old, new):  # noqa: ANN001
            call_order.append("hook")

        svc.register_after_save_hook(_hook)

        def _reload():
            call_order.append("reload")
            return 1

        with patch(_RELOAD_TARGET, side_effect=_reload):
            svc.handle_apply_profile_preset({"profile": "default"})

        self.assertEqual(call_order, ["reload", "hook"])

    def test_reload_exception_swallowed(self):
        """reload failure must not prevent hooks or return value."""
        hook = MagicMock()
        svc = _make_svc()
        svc.register_after_save_hook(hook)
        with patch(_RELOAD_TARGET, side_effect=RuntimeError("boom")):
            result = svc.handle_apply_profile_preset({"profile": "default"})
        hook.assert_called_once()
        self.assertIsInstance(result, dict)


class TestImportSettingsReloadsSettings(unittest.TestCase):
    """handle_import_settings must call reload_settings_from_json before hooks."""

    def _write_json(self, tmp_dir: str, data: dict) -> str:
        p = Path(tmp_dir) / "import.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)

    def test_reload_called(self):
        svc = _make_svc()
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"audio_ducking_enabled": False})
            with patch(_RELOAD_TARGET, return_value=1) as mock_reload:
                svc.handle_import_settings({"file": path})
        mock_reload.assert_called_once()

    def test_reload_called_before_hooks(self):
        call_order: list[str] = []
        svc = _make_svc()

        def _hook(old, new):  # noqa: ANN001
            call_order.append("hook")

        svc.register_after_save_hook(_hook)

        def _reload():
            call_order.append("reload")
            return 1

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"audio_ducking_enabled": False})
            with patch(_RELOAD_TARGET, side_effect=_reload):
                svc.handle_import_settings({"file": path})

        self.assertEqual(call_order, ["reload", "hook"])

    def test_reload_exception_swallowed(self):
        hook = MagicMock()
        svc = _make_svc()
        svc.register_after_save_hook(hook)
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_json(tmp, {"audio_ducking_enabled": False})
            with patch(_RELOAD_TARGET, side_effect=RuntimeError("boom")):
                result = svc.handle_import_settings({"file": path})
        hook.assert_called_once()
        self.assertIn("imported", result)


class TestSetNotificationPreferencesReloadsSettings(unittest.TestCase):
    """handle_set_notification_preferences must call reload_settings_from_json."""

    def test_reload_called(self):
        svc = _make_svc()
        with patch(_RELOAD_TARGET, return_value=2) as mock_reload:
            svc.handle_set_notification_preferences({"notifications_enabled": False})
        mock_reload.assert_called_once()

    def test_reload_called_before_hooks(self):
        call_order: list[str] = []
        svc = _make_svc()

        def _hook(old, new):  # noqa: ANN001
            call_order.append("hook")

        svc.register_after_save_hook(_hook)

        def _reload():
            call_order.append("reload")
            return 1

        with patch(_RELOAD_TARGET, side_effect=_reload):
            svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertEqual(call_order, ["reload", "hook"])

    def test_reload_exception_swallowed(self):
        hook = MagicMock()
        svc = _make_svc()
        svc.register_after_save_hook(hook)
        with patch(_RELOAD_TARGET, side_effect=ValueError("oops")):
            result = svc.handle_set_notification_preferences({"notifications_enabled": False})
        hook.assert_called_once()
        self.assertIsInstance(result, dict)


class TestRestoreSettingsBackupReloadsSettings(unittest.TestCase):
    """handle_restore_settings_backup must call reload_settings_from_json."""

    def test_reload_called(self):
        svc = _make_svc(backup=_make_backup())
        with patch(_RELOAD_TARGET, return_value=5) as mock_reload:
            svc.handle_restore_settings_backup({"backup_id": "backup-001"})
        mock_reload.assert_called_once()

    def test_reload_called_before_hooks(self):
        call_order: list[str] = []
        svc = _make_svc(backup=_make_backup())

        def _hook(old, new):  # noqa: ANN001
            call_order.append("hook")

        svc.register_after_save_hook(_hook)

        def _reload():
            call_order.append("reload")
            return 1

        with patch(_RELOAD_TARGET, side_effect=_reload):
            svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertEqual(call_order, ["reload", "hook"])

    def test_reload_exception_swallowed(self):
        hook = MagicMock()
        svc = _make_svc(backup=_make_backup())
        svc.register_after_save_hook(hook)
        with patch(_RELOAD_TARGET, side_effect=OSError("disk")):
            result = svc.handle_restore_settings_backup({"backup_id": "backup-001"})
        hook.assert_called_once()
        self.assertIn("restored_settings", result)


class TestSetSettingsStillReloads(unittest.TestCase):
    """Regression: handle_set_settings must still reload (original path)."""

    def test_reload_called(self):
        svc = _make_svc()
        with patch(_RELOAD_TARGET, return_value=1) as mock_reload:
            svc.handle_set_settings({"audio_ducking_enabled": False})
        mock_reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
