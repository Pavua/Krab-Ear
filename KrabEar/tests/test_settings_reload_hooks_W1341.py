"""Tests for W1341 fix: _fire_after_save_hooks always reloads settings.json.

W1334 F1 HIGH: 4 non-set_settings paths (apply_profile_preset, import_settings,
set_notification_preferences, restore_settings_backup) were not calling
reload_settings_from_json() before firing hooks — pydantic settings.MODEL_BALANCED
stayed stale after saves.

Fix: _reload_and_fire_hooks() is a single point of truth, called on ALL 5 paths.
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


_MINIMAL_SETTINGS: dict = {
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
}


def _make_store(settings: dict | None = None) -> MagicMock:
    store = MagicMock()
    current: dict = dict(settings or _MINIMAL_SETTINGS)
    store.load_settings.return_value = dict(current)

    def _save(s: dict) -> dict:
        current.clear()
        current.update(s)
        store.load_settings.return_value = dict(current)
        return dict(s)

    store.save_settings.side_effect = _save
    store._current = current
    return store


class TestApplyProfilePresetReloadsSettings(unittest.TestCase):
    """apply_profile_preset should call reload_settings_from_json."""

    def test_apply_profile_preset_reloads_settings(self):
        """reload_settings_from_json must be called after apply_profile_preset saves."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("backend.event_bus.bus"):
            with patch("core.config.reload_settings_from_json", return_value=3) as mock_reload:
                svc.handle_apply_profile_preset({"profile": "meeting"})

        mock_reload.assert_called_once()

    def test_apply_profile_preset_fires_hooks_after_reload(self):
        """After reload, registered hooks should be called with old/new settings."""
        store = _make_store()
        svc = SettingsService(store=store)
        hook = MagicMock()
        svc.register_after_save_hook(hook)

        reload_order: list[str] = []

        def _reload():
            reload_order.append("reload")
            return 2

        def _hook(old, new):
            reload_order.append("hook")

        hook.side_effect = _hook

        with patch("backend.event_bus.bus"):
            with patch("core.config.reload_settings_from_json", side_effect=_reload):
                svc.handle_apply_profile_preset({"profile": "default"})

        self.assertEqual(reload_order, ["reload", "hook"],
                         "reload must happen before hook fires")

    def test_apply_profile_preset_hook_receives_correct_settings(self):
        """Hooks should receive (old_settings, new_settings) with preset applied."""
        store = _make_store()
        svc = SettingsService(store=store)
        hook_args: list[tuple] = []
        svc.register_after_save_hook(lambda old, new: hook_args.append((old, new)))

        with patch("backend.event_bus.bus"):
            with patch("core.config.reload_settings_from_json", return_value=0):
                svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertEqual(len(hook_args), 1)
        old, new = hook_args[0]
        self.assertEqual(old.get("quality_profile"), "balanced")  # original
        self.assertEqual(new.get("quality_profile"), "max")        # meeting preset


class TestImportSettingsReloadsSettings(unittest.TestCase):
    """import_settings should call reload_settings_from_json."""

    def _write_import_file(self, data: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(data, tmp)
        tmp.close()
        return tmp.name

    def test_import_settings_reloads_settings(self):
        """reload_settings_from_json must be called after import_settings saves."""
        store = _make_store()
        svc = SettingsService(store=store)
        tmp = self._write_import_file({"auto_paste": False})

        with patch("core.config.reload_settings_from_json", return_value=1) as mock_reload:
            svc.handle_import_settings({"file": tmp})

        mock_reload.assert_called_once()

    def test_import_settings_fires_hooks(self):
        """Hooks should fire after import_settings."""
        store = _make_store()
        svc = SettingsService(store=store)
        hook = MagicMock()
        svc.register_after_save_hook(hook)
        tmp = self._write_import_file({"quality_profile": "max"})

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_import_settings({"file": tmp})

        hook.assert_called_once()
        old, new = hook.call_args[0]
        self.assertEqual(old.get("quality_profile"), "balanced")
        self.assertEqual(new.get("quality_profile"), "max")

    def test_import_settings_reload_before_hook(self):
        """reload_settings_from_json must run before hook fires."""
        store = _make_store()
        svc = SettingsService(store=store)
        order: list[str] = []
        svc.register_after_save_hook(lambda old, new: order.append("hook"))
        tmp = self._write_import_file({"auto_paste": False})

        def _reload():
            order.append("reload")
            return 0

        with patch("core.config.reload_settings_from_json", side_effect=_reload):
            svc.handle_import_settings({"file": tmp})

        self.assertEqual(order, ["reload", "hook"])


class TestSetNotificationPrefsReloadsSettings(unittest.TestCase):
    """set_notification_preferences should call reload_settings_from_json."""

    def test_set_notification_prefs_reloads_settings(self):
        """reload_settings_from_json must be called after set_notification_preferences saves."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("core.config.reload_settings_from_json", return_value=1) as mock_reload:
            svc.handle_set_notification_preferences({"notifications_enabled": False})

        mock_reload.assert_called_once()

    def test_set_notification_prefs_fires_hooks(self):
        """Hooks should fire after set_notification_preferences."""
        store = _make_store()
        svc = SettingsService(store=store)
        hook = MagicMock()
        svc.register_after_save_hook(hook)

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_set_notification_preferences({"notifications_enabled": False})

        hook.assert_called_once()
        old, new = hook.call_args[0]
        self.assertTrue(old.get("notifications_enabled"))   # was True
        self.assertFalse(new.get("notifications_enabled"))  # now False

    def test_set_notification_prefs_reload_before_hook(self):
        """reload_settings_from_json must run before hook fires."""
        store = _make_store()
        svc = SettingsService(store=store)
        order: list[str] = []
        svc.register_after_save_hook(lambda old, new: order.append("hook"))

        def _reload():
            order.append("reload")
            return 0

        with patch("core.config.reload_settings_from_json", side_effect=_reload):
            svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertEqual(order, ["reload", "hook"])


class TestRestoreBackupReloadsSettings(unittest.TestCase):
    """restore_settings_backup should call reload_settings_from_json."""

    def _make_backup(self) -> MagicMock:
        backup = MagicMock()
        backup.list_backups.return_value = [
            {"backup_id": "bk1", "ts": "2026-01-01T00:00:00Z", "reason": "manual"}
        ]
        restored = dict(_MINIMAL_SETTINGS)
        restored["quality_profile"] = "max"
        backup.restore_backup.return_value = restored
        return backup

    def test_restore_backup_reloads_settings(self):
        """reload_settings_from_json must be called after restore_settings_backup saves."""
        store = _make_store()
        svc = SettingsService(store=store, backup=self._make_backup())

        with patch("core.config.reload_settings_from_json", return_value=2) as mock_reload:
            svc.handle_restore_settings_backup({"backup_id": "bk1"})

        mock_reload.assert_called_once()

    def test_restore_backup_fires_hooks(self):
        """Hooks should fire after restore_settings_backup."""
        store = _make_store()
        svc = SettingsService(store=store, backup=self._make_backup())
        hook = MagicMock()
        svc.register_after_save_hook(hook)

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_restore_settings_backup({"backup_id": "bk1"})

        hook.assert_called_once()
        old, new = hook.call_args[0]
        self.assertEqual(old.get("quality_profile"), "balanced")  # original
        self.assertEqual(new.get("quality_profile"), "max")        # restored value

    def test_restore_backup_reload_before_hook(self):
        """reload_settings_from_json must run before hook fires."""
        store = _make_store()
        svc = SettingsService(store=store, backup=self._make_backup())
        order: list[str] = []
        svc.register_after_save_hook(lambda old, new: order.append("hook"))

        def _reload():
            order.append("reload")
            return 0

        with patch("core.config.reload_settings_from_json", side_effect=_reload):
            svc.handle_restore_settings_backup({"backup_id": "bk1"})

        self.assertEqual(order, ["reload", "hook"])


class TestSetSettingsStillReloadsRegression(unittest.TestCase):
    """Regression: set_settings must still reload (not broken by refactor)."""

    def test_set_settings_still_reloads(self):
        """reload_settings_from_json must still be called from handle_set_settings."""
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("core.config.reload_settings_from_json", return_value=1) as mock_reload:
            svc.handle_set_settings({"quality_profile": "max"})

        mock_reload.assert_called_once()

    def test_set_settings_still_fires_hooks(self):
        """Hooks must still fire from handle_set_settings."""
        store = _make_store()
        svc = SettingsService(store=store)
        hook = MagicMock()
        svc.register_after_save_hook(hook)

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_set_settings({"quality_profile": "max"})

        hook.assert_called_once()

    def test_set_settings_reload_before_hook(self):
        """reload_settings_from_json must run before hook fires in set_settings."""
        store = _make_store()
        svc = SettingsService(store=store)
        order: list[str] = []
        svc.register_after_save_hook(lambda old, new: order.append("hook"))

        def _reload():
            order.append("reload")
            return 0

        with patch("core.config.reload_settings_from_json", side_effect=_reload):
            svc.handle_set_settings({"auto_paste": False})

        self.assertEqual(order, ["reload", "hook"])

    def test_all_five_paths_call_reload_exactly_once_each(self):
        """Each of the 5 write paths must call reload_settings_from_json exactly once."""
        import json as _json
        import tempfile

        # We need a backup mock for restore path
        backup = MagicMock()
        backup.restore_backup.return_value = dict(_MINIMAL_SETTINGS)

        store = _make_store()
        svc = SettingsService(store=store, backup=backup)

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        _json.dump({"auto_paste": False}, tmp)
        tmp.close()

        paths = [
            ("set_settings", lambda: svc.handle_set_settings({"quality_profile": "max"})),
            ("apply_profile_preset", lambda: svc.handle_apply_profile_preset({"profile": "meeting"})),
            ("import_settings", lambda: svc.handle_import_settings({"file": tmp.name})),
            ("set_notification_prefs", lambda: svc.handle_set_notification_preferences({"notifications_enabled": False})),
            ("restore_backup", lambda: svc.handle_restore_settings_backup({"backup_id": "bk1"})),
        ]

        for name, fn in paths:
            with self.subTest(path=name):
                with patch("backend.event_bus.bus"):
                    with patch("core.config.reload_settings_from_json", return_value=0) as mock_reload:
                        fn()
                mock_reload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
