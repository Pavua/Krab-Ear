"""W1308 — SettingsService fires _after_save_hooks on all 5 save paths.

Tests:
- test_handle_set_settings_still_fires_hooks        (regression: original path still works)
- test_apply_profile_preset_fires_hooks
- test_import_settings_fires_hooks
- test_set_notification_preferences_fires_hooks
- test_restore_settings_backup_fires_hooks
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    """Create a fake SettingsBackup that returns restore_data on restore_backup()."""
    backup = MagicMock()
    backup.restore_backup.return_value = dict(restore_data or _BASE_SETTINGS)
    backup.create_backup.return_value = "backup-001"
    return backup


class TestHandleSetSettingsStillFiresHooks(unittest.TestCase):
    """Regression: handle_set_settings must still fire hooks (original path)."""

    def test_hook_called_with_old_and_new(self):
        store = _make_store()
        svc = SettingsService(store=store)
        calls: list[tuple] = []

        def hook(old, new):
            calls.append((dict(old), dict(new)))

        svc.register_after_save_hook(hook)

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_set_settings({"quality_profile": "max"})

        self.assertEqual(len(calls), 1)
        old, new = calls[0]
        self.assertEqual(old["quality_profile"], "balanced")
        self.assertEqual(new["quality_profile"], "max")

    def test_multiple_hooks_all_called(self):
        store = _make_store()
        svc = SettingsService(store=store)
        fired: list[str] = []

        svc.register_after_save_hook(lambda o, n: fired.append("hook1"))
        svc.register_after_save_hook(lambda o, n: fired.append("hook2"))

        with patch("core.config.reload_settings_from_json", return_value=0):
            svc.handle_set_settings({"auto_paste": False})

        self.assertEqual(fired, ["hook1", "hook2"])

    def test_failing_hook_does_not_prevent_return(self):
        store = _make_store()
        svc = SettingsService(store=store)
        fired: list[str] = []

        def bad_hook(old, new):
            raise RuntimeError("boom")

        svc.register_after_save_hook(bad_hook)
        svc.register_after_save_hook(lambda o, n: fired.append("ok"))

        with patch("core.config.reload_settings_from_json", return_value=0):
            result = svc.handle_set_settings({"auto_paste": False})

        self.assertIsNotNone(result)
        self.assertIn("ok", fired)


class TestApplyProfilePresetFiresHooks(unittest.TestCase):
    """handle_apply_profile_preset must fire _after_save_hooks."""

    def test_hook_called_once(self):
        store = _make_store()
        svc = SettingsService(store=store)
        calls: list[tuple] = []

        def hook(old, new):
            calls.append((dict(old), dict(new)))

        svc.register_after_save_hook(hook)

        with patch("backend.event_bus.bus") as _mock_bus:
            svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertEqual(len(calls), 1)

    def test_hook_receives_correct_preset_values(self):
        store = _make_store()
        svc = SettingsService(store=store)
        results: list[dict] = []

        svc.register_after_save_hook(lambda o, n: results.append(dict(n)))

        with patch("backend.event_bus.bus") as _mock_bus:
            svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertEqual(len(results), 1)
        # meeting preset sets quality_profile=max
        self.assertEqual(results[0]["quality_profile"], "max")
        self.assertEqual(results[0]["active_preset"], "meeting")

    def test_hook_old_is_settings_before_preset(self):
        store = _make_store()
        svc = SettingsService(store=store)
        olds: list[dict] = []

        svc.register_after_save_hook(lambda o, n: olds.append(dict(o)))

        with patch("backend.event_bus.bus") as _mock_bus:
            svc.handle_apply_profile_preset({"profile": "meeting"})

        self.assertEqual(len(olds), 1)
        # Before applying meeting preset, quality_profile was balanced
        self.assertEqual(olds[0]["quality_profile"], "balanced")

    def test_no_hooks_registered_still_succeeds(self):
        store = _make_store()
        svc = SettingsService(store=store)

        with patch("backend.event_bus.bus") as _mock_bus:
            result = svc.handle_apply_profile_preset({"profile": "default"})

        self.assertIsNotNone(result)


class TestImportSettingsFiresHooks(unittest.TestCase):
    """handle_import_settings must fire _after_save_hooks."""

    def _make_json_file(self, data: dict) -> Path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(data, tmp)
        tmp.close()
        return Path(tmp.name)

    def test_hook_called_once(self):
        store = _make_store()
        svc = SettingsService(store=store)
        calls: list[tuple] = []

        def hook(old, new):
            calls.append((dict(old), dict(new)))

        svc.register_after_save_hook(hook)
        json_file = self._make_json_file({"audio_ducking_enabled": False})

        try:
            svc.handle_import_settings({"file": str(json_file)})
        finally:
            json_file.unlink(missing_ok=True)

        self.assertEqual(len(calls), 1)

    def test_hook_new_contains_imported_value(self):
        store = _make_store()
        svc = SettingsService(store=store)
        news: list[dict] = []

        svc.register_after_save_hook(lambda o, n: news.append(dict(n)))
        json_file = self._make_json_file({"audio_ducking_enabled": False})

        try:
            svc.handle_import_settings({"file": str(json_file)})
        finally:
            json_file.unlink(missing_ok=True)

        self.assertEqual(len(news), 1)
        self.assertFalse(news[0].get("audio_ducking_enabled"))

    def test_hook_old_is_pre_import_settings(self):
        store = _make_store()
        svc = SettingsService(store=store)
        olds: list[dict] = []

        svc.register_after_save_hook(lambda o, n: olds.append(dict(o)))
        json_file = self._make_json_file({"audio_ducking_enabled": False})

        try:
            svc.handle_import_settings({"file": str(json_file)})
        finally:
            json_file.unlink(missing_ok=True)

        self.assertEqual(len(olds), 1)
        # Before import audio_ducking_enabled was True
        self.assertTrue(olds[0].get("audio_ducking_enabled"))

    def test_sensitive_field_skipped_but_hook_still_fires(self):
        """Sensitive fields are skipped but hooks must still be called."""
        store = _make_store()
        svc = SettingsService(store=store)
        fired: list[bool] = []

        svc.register_after_save_hook(lambda o, n: fired.append(True))
        # hf_token is in _SENSITIVE_FIELDS
        json_file = self._make_json_file({"hf_token": "secret"})

        try:
            svc.handle_import_settings({"file": str(json_file)})
        finally:
            json_file.unlink(missing_ok=True)

        self.assertEqual(fired, [True])


class TestSetNotificationPreferencesFiresHooks(unittest.TestCase):
    """handle_set_notification_preferences must fire _after_save_hooks."""

    def test_hook_called_once(self):
        store = _make_store()
        svc = SettingsService(store=store)
        calls: list[tuple] = []

        def hook(old, new):
            calls.append((dict(old), dict(new)))

        svc.register_after_save_hook(hook)
        svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertEqual(len(calls), 1)

    def test_hook_new_reflects_updated_pref(self):
        store = _make_store()
        svc = SettingsService(store=store)
        news: list[dict] = []

        svc.register_after_save_hook(lambda o, n: news.append(dict(n)))
        svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertEqual(len(news), 1)
        self.assertFalse(news[0].get("notifications_enabled"))

    def test_hook_old_reflects_prior_pref(self):
        store = _make_store()
        svc = SettingsService(store=store)
        olds: list[dict] = []

        svc.register_after_save_hook(lambda o, n: olds.append(dict(o)))
        svc.handle_set_notification_preferences({"notifications_enabled": False})

        self.assertEqual(len(olds), 1)
        self.assertTrue(olds[0].get("notifications_enabled"))

    def test_threshold_update_fires_hook(self):
        store = _make_store()
        svc = SettingsService(store=store)
        fired: list[bool] = []

        svc.register_after_save_hook(lambda o, n: fired.append(True))
        svc.handle_set_notification_preferences({"notify_confidence_threshold": 0.8})

        self.assertEqual(fired, [True])


class TestRestoreSettingsBackupFiresHooks(unittest.TestCase):
    """handle_restore_settings_backup must fire _after_save_hooks."""

    def test_hook_called_once(self):
        store = _make_store()
        restore_data = dict(_BASE_SETTINGS)
        restore_data["quality_profile"] = "max"
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=store, backup=backup)
        calls: list[tuple] = []

        def hook(old, new):
            calls.append((dict(old), dict(new)))

        svc.register_after_save_hook(hook)
        svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertEqual(len(calls), 1)

    def test_hook_new_is_restored_settings(self):
        store = _make_store()
        restore_data = dict(_BASE_SETTINGS)
        restore_data["quality_profile"] = "max"
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=store, backup=backup)
        news: list[dict] = []

        svc.register_after_save_hook(lambda o, n: news.append(dict(n)))
        svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["quality_profile"], "max")

    def test_hook_old_is_pre_restore_settings(self):
        store = _make_store()
        restore_data = dict(_BASE_SETTINGS)
        restore_data["quality_profile"] = "max"
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=store, backup=backup)
        olds: list[dict] = []

        svc.register_after_save_hook(lambda o, n: olds.append(dict(o)))
        svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertEqual(len(olds), 1)
        # Before restore quality_profile was balanced
        self.assertEqual(olds[0]["quality_profile"], "balanced")

    def test_no_hooks_registered_still_returns_result(self):
        store = _make_store()
        backup = _make_backup()
        svc = SettingsService(store=store, backup=backup)

        result = svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertIn("restored_settings", result)
        self.assertEqual(result["backup_id"], "backup-001")

    def test_failing_hook_does_not_prevent_restore(self):
        store = _make_store()
        restore_data = dict(_BASE_SETTINGS)
        backup = _make_backup(restore_data=restore_data)
        svc = SettingsService(store=store, backup=backup)
        fired: list[str] = []

        svc.register_after_save_hook(lambda o, n: (_ for _ in ()).throw(RuntimeError("bad")))
        svc.register_after_save_hook(lambda o, n: fired.append("ok"))

        result = svc.handle_restore_settings_backup({"backup_id": "backup-001"})

        self.assertIn("restored_settings", result)
        self.assertIn("ok", fired)


class TestFireAfterSaveHooksMethod(unittest.TestCase):
    """Direct tests for the _fire_after_save_hooks private method."""

    def test_no_hooks_no_error(self):
        store = _make_store()
        svc = SettingsService(store=store)
        # Should not raise even with no hooks registered
        svc._fire_after_save_hooks({}, {})

    def test_single_hook_receives_both_dicts(self):
        store = _make_store()
        svc = SettingsService(store=store)
        received: list[tuple] = []

        svc.register_after_save_hook(lambda o, n: received.append((o, n)))
        old = {"a": 1}
        new = {"a": 2}
        svc._fire_after_save_hooks(old, new)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], {"a": 1})
        self.assertEqual(received[0][1], {"a": 2})

    def test_exception_in_hook_swallowed_and_rest_run(self):
        store = _make_store()
        svc = SettingsService(store=store)
        fired: list[str] = []

        svc.register_after_save_hook(lambda o, n: (_ for _ in ()).throw(ValueError("bad")))
        svc.register_after_save_hook(lambda o, n: fired.append("ran"))

        svc._fire_after_save_hooks({}, {})

        self.assertEqual(fired, ["ran"])


if __name__ == "__main__":
    unittest.main()
