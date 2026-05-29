"""Tests for W1199 / W1193-F1-HIGH: init_sentry must respect privacy_mode_enabled.

Three scenarios verified:
1. init_sentry is a no-op when privacy_mode_enabled=True is in settings.
2. init_sentry proceeds normally when privacy_mode_enabled=False (or absent).
3. Runtime: setting privacy_mode_enabled=True via IPC disables Sentry SDK.
"""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ROOT = __file__
for _ in range(3):
    import os
    PROJECT_ROOT = os.path.dirname(PROJECT_ROOT)

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _reset_observability():
    """Reset module-level Sentry state between tests."""
    import backend.observability as mod  # noqa: PLC0415
    mod._sentry_initialized = False
    return mod


def _make_fake_sentry_sdk():
    """Return a minimal sentry_sdk stub with a callable init."""
    fake = types.ModuleType("sentry_sdk")
    fake.init = MagicMock()
    fake.flush = MagicMock()
    return fake


# ---------------------------------------------------------------------------
# Test 1: init_sentry no-op when privacy_mode_enabled=True
# ---------------------------------------------------------------------------

class TestInitSentryNoOpWhenPrivacyModeEnabled(unittest.TestCase):
    """W1199: init_sentry must return False and not call SDK when privacy_mode_enabled."""

    def setUp(self):
        _reset_observability()

    def test_init_sentry_no_op_when_privacy_mode_enabled(self):
        """init_sentry returns False when settings contains privacy_mode_enabled=True."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                environment="production",
                settings={"privacy_mode_enabled": True},
            )

        self.assertFalse(result, "init_sentry must return False when privacy_mode_enabled=True")
        fake_sdk.init.assert_not_called()
        self.assertFalse(
            mod._sentry_initialized,
            "_sentry_initialized must remain False when privacy_mode_enabled=True",
        )

    def test_init_sentry_no_op_privacy_mode_enabled_no_dsn(self):
        """No-op even if DSN is absent and privacy_mode_enabled=True."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn=None,
                settings={"privacy_mode_enabled": True},
            )

        self.assertFalse(result)
        fake_sdk.init.assert_not_called()

    def test_init_sentry_no_op_privacy_mode_truthy_string(self):
        """privacy_mode_enabled=True (bool) — only exact True matches; string 'True' is irrelevant."""
        import backend.observability as mod

        # The guard uses `settings.get("privacy_mode_enabled")` — it checks for
        # truthiness but the store normalises the value as a bool.  Pass exact bool.
        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                settings={"privacy_mode_enabled": True},
            )

        self.assertFalse(result)
        fake_sdk.init.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: init_sentry active when privacy_mode_enabled=False (or absent)
# ---------------------------------------------------------------------------

class TestInitSentryActiveWhenPrivacyModeDisabled(unittest.TestCase):
    """W1199: init_sentry must proceed when privacy_mode_enabled is False or absent."""

    def setUp(self):
        _reset_observability()

    def test_init_sentry_active_when_privacy_mode_disabled(self):
        """init_sentry returns True when privacy_mode_enabled=False."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/123",
                settings={"privacy_mode_enabled": False},
            )

        self.assertTrue(result, "init_sentry must succeed when privacy_mode_enabled=False")
        fake_sdk.init.assert_called_once()

    def test_init_sentry_active_when_settings_empty(self):
        """init_sentry proceeds normally when settings={} (key absent)."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/123",
                settings={},
            )

        self.assertTrue(result)
        fake_sdk.init.assert_called_once()

    def test_init_sentry_active_when_settings_none(self):
        """init_sentry proceeds normally when settings=None (legacy callers)."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/123",
                settings=None,
            )

        self.assertTrue(result)
        fake_sdk.init.assert_called_once()

    def test_init_sentry_active_when_no_settings_kwarg(self):
        """init_sentry proceeds when called without settings= (backward compat)."""
        import backend.observability as mod

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(dsn="https://fake@sentry.io/123")

        self.assertTrue(result)
        fake_sdk.init.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: Runtime disable — setting privacy_mode_enabled=True via IPC
# ---------------------------------------------------------------------------

class TestRuntimeEnablePrivacyDisablesSentry(unittest.TestCase):
    """W1199: setting privacy_mode_enabled=True at runtime must flush+disable Sentry."""

    def _make_minimal_store(self, settings_dict):
        """Return a mock StateStore that returns the given settings dict."""
        store = MagicMock()
        store.load_settings.return_value = dict(settings_dict)
        store.save_settings.return_value = dict(settings_dict)
        return store

    def test_runtime_enable_privacy_disables_sentry(self):
        """When privacy_mode_enabled flips True, SDK is flushed and flag reset."""
        import backend.observability as _obs

        # Pretend Sentry was previously initialized — set directly on the real module.
        _obs._sentry_initialized = True

        fake_sdk = _make_fake_sentry_sdk()

        from backend.settings_service import SettingsService
        from backend.settings_backup import SettingsBackup

        # Start with privacy_mode_enabled=False in store
        initial_settings = {"privacy_mode_enabled": False, "mode": "headless",
                            "quality_profile": "balanced", "cleanup_profile": "soft",
                            "translation_mode": "off", "translation_style": "neutral",
                            "clipboard_mode": "always_copy", "update_channel": "stable",
                            "translation_glossary": {}, "text_templates": {},
                            "network_mode": "offline_default", "hotkey_profile": "default",
                            "history_policy": "unlimited", "history_text_density": "normal",
                            "capture_source_mode": "mic", "ui_last_tab": "history",
                            "auto_start_enabled": False, "show_dock_icon": True,
                            "auto_paste": True, "play_start_sound": True,
                            "realtime_preview_enabled": True, "translate_and_paste": False,
                            "onboarding_completed": False, "audio_ducking_enabled": True,
                            "silence_guard_enabled": True, "background_guard_enabled": True,
                            "call_notify_default": True, "call_auto_summary": True,
                            "history_focus_mode": True,
                            "voice_gateway_url": "http://127.0.0.1:8090",
                            "voice_gateway_api_key": ""}

        store = self._make_minimal_store(initial_settings)
        store.save_settings.side_effect = lambda s: dict(s)

        backup = MagicMock(spec=SettingsBackup)
        backup.create_backup.return_value = "backup_id_1"

        svc = SettingsService(store=store, backup=backup)

        # Inject fake sentry_sdk and suppress reload_settings_from_json import
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                svc.handle_set_settings({"privacy_mode_enabled": True})

        # flush should have been called
        fake_sdk.flush.assert_called_once_with(timeout=2)
        # SDK re-init with dsn=None
        fake_sdk.init.assert_called_once_with(dsn=None)
        # Module flag reset — read directly from the real module
        self.assertFalse(_obs._sentry_initialized)

    def test_runtime_privacy_already_enabled_no_double_flush(self):
        """If privacy_mode was already True, no flush on redundant set_settings call."""
        import backend.observability as _obs
        _obs._sentry_initialized = False  # Sentry not active

        fake_sdk = _make_fake_sentry_sdk()

        from backend.settings_service import SettingsService
        from backend.settings_backup import SettingsBackup

        # privacy_mode already True in store — flip condition NOT triggered
        initial_settings = {"privacy_mode_enabled": True, "mode": "headless",
                            "quality_profile": "balanced", "cleanup_profile": "soft",
                            "translation_mode": "off", "translation_style": "neutral",
                            "clipboard_mode": "always_copy", "update_channel": "stable",
                            "translation_glossary": {}, "text_templates": {},
                            "network_mode": "offline_default", "hotkey_profile": "default",
                            "history_policy": "unlimited", "history_text_density": "normal",
                            "capture_source_mode": "mic", "ui_last_tab": "history",
                            "auto_start_enabled": False, "show_dock_icon": True,
                            "auto_paste": True, "play_start_sound": True,
                            "realtime_preview_enabled": True, "translate_and_paste": False,
                            "onboarding_completed": False, "audio_ducking_enabled": True,
                            "silence_guard_enabled": True, "background_guard_enabled": True,
                            "call_notify_default": True, "call_auto_summary": True,
                            "history_focus_mode": True,
                            "voice_gateway_url": "http://127.0.0.1:8090",
                            "voice_gateway_api_key": ""}

        store = self._make_minimal_store(initial_settings)
        store.save_settings.side_effect = lambda s: dict(s)

        backup = MagicMock(spec=SettingsBackup)
        backup.create_backup.return_value = "backup_id_2"

        svc = SettingsService(store=store, backup=backup)

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                svc.handle_set_settings({"privacy_mode_enabled": True})

        # No flush/init calls — privacy_mode was already True (no transition)
        fake_sdk.flush.assert_not_called()
        fake_sdk.init.assert_not_called()

    def test_runtime_privacy_mode_not_in_params_no_disable(self):
        """If privacy_mode_enabled is not in the params dict, Sentry is untouched."""
        import backend.observability as _obs
        _obs._sentry_initialized = True  # Sentry is active

        fake_sdk = _make_fake_sentry_sdk()

        from backend.settings_service import SettingsService
        from backend.settings_backup import SettingsBackup

        initial_settings = {"privacy_mode_enabled": False, "mode": "headless",
                            "quality_profile": "balanced", "cleanup_profile": "soft",
                            "translation_mode": "off", "translation_style": "neutral",
                            "clipboard_mode": "always_copy", "update_channel": "stable",
                            "translation_glossary": {}, "text_templates": {},
                            "network_mode": "offline_default", "hotkey_profile": "default",
                            "history_policy": "unlimited", "history_text_density": "normal",
                            "capture_source_mode": "mic", "ui_last_tab": "history",
                            "auto_start_enabled": False, "show_dock_icon": True,
                            "auto_paste": True, "play_start_sound": True,
                            "realtime_preview_enabled": True, "translate_and_paste": False,
                            "onboarding_completed": False, "audio_ducking_enabled": True,
                            "silence_guard_enabled": True, "background_guard_enabled": True,
                            "call_notify_default": True, "call_auto_summary": True,
                            "history_focus_mode": True,
                            "voice_gateway_url": "http://127.0.0.1:8090",
                            "voice_gateway_api_key": ""}

        store = self._make_minimal_store(initial_settings)
        store.save_settings.side_effect = lambda s: dict(s)

        backup = MagicMock(spec=SettingsBackup)
        backup.create_backup.return_value = "backup_id_3"

        svc = SettingsService(store=store, backup=backup)

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                # Only change a non-privacy setting — privacy_mode_enabled NOT in params
                svc.handle_set_settings({"quality_profile": "max"})

        # Sentry not touched — only a non-privacy setting changed
        fake_sdk.flush.assert_not_called()
        fake_sdk.init.assert_not_called()
        # Flag remains True (unchanged)
        self.assertTrue(_obs._sentry_initialized)


# ---------------------------------------------------------------------------
# W1601 tests: _sentry_initialized cleared when privacy_mode toggled ON after init
# ---------------------------------------------------------------------------

class TestPrivacyModeToggleAfterInitClearsSentryFlag(unittest.TestCase):
    """W1601 / W1599 F1 HIGH: init_sentry must clear _sentry_initialized when
    privacy_mode_enabled=True is passed AFTER Sentry was previously initialised."""

    def setUp(self):
        _reset_observability()

    def test_privacy_mode_toggle_after_init_clears_sentry_initialized(self):
        """init_sentry clears _sentry_initialized when privacy mode is toggled ON
        while _sentry_initialized is already True (W1599 F1 HIGH scenario)."""
        import backend.observability as mod

        # Simulate Sentry having been initialised in a prior call.
        mod._sentry_initialized = True

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                settings={"privacy_mode_enabled": True},
            )

        self.assertFalse(result, "init_sentry must return False when privacy_mode_enabled=True")
        self.assertFalse(
            mod._sentry_initialized,
            "_sentry_initialized MUST be False after privacy_mode toggle ON (W1601 fix)",
        )
        # The SDK init must NOT have been called.
        fake_sdk.init.assert_not_called()

    def test_capture_exception_no_op_after_privacy_toggle_on(self):
        """capture_exception becomes a no-op once privacy_mode clears the flag.

        Scenario:
          1. Sentry initialised → _sentry_initialized = True.
          2. init_sentry called with privacy_mode_enabled=True → clears flag.
          3. capture_exception() → must NOT call sentry_sdk.capture_exception.
        """
        import backend.observability as mod

        # Step 1: simulate prior init.
        mod._sentry_initialized = True

        fake_sdk = _make_fake_sentry_sdk()
        fake_sdk.push_scope = MagicMock()
        # Make push_scope a context manager that returns a mock scope.
        mock_scope = MagicMock()
        fake_sdk.push_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        fake_sdk.push_scope.return_value.__exit__ = MagicMock(return_value=False)
        fake_sdk.capture_exception = MagicMock()

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            # Step 2: toggle privacy ON — must clear the flag.
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                settings={"privacy_mode_enabled": True},
            )
            self.assertFalse(result)
            self.assertFalse(mod._sentry_initialized)

            # Step 3: capture_exception must be a no-op.
            mod.capture_exception(RuntimeError("should not be sent"))

        fake_sdk.capture_exception.assert_not_called()

    def test_add_breadcrumb_no_op_after_privacy_toggle_on(self):
        """add_breadcrumb becomes a no-op once privacy_mode clears the flag."""
        import backend.observability as mod

        mod._sentry_initialized = True

        fake_sdk = _make_fake_sentry_sdk()
        fake_sdk.add_breadcrumb = MagicMock()

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                settings={"privacy_mode_enabled": True},
            )
            self.assertFalse(mod._sentry_initialized)

            mod.add_breadcrumb(
                category="ipc",
                message="should_not_be_recorded",
            )

        fake_sdk.add_breadcrumb.assert_not_called()

    def test_privacy_mode_on_when_already_false_stays_false(self):
        """If _sentry_initialized was already False, privacy_mode=True is still a no-op
        (no double-clear side effects)."""
        import backend.observability as mod

        # Flag is already False — this is the normal "never initialised" path.
        self.assertFalse(mod._sentry_initialized)

        fake_sdk = _make_fake_sentry_sdk()
        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            result = mod.init_sentry(
                dsn="https://fake@sentry.io/999",
                settings={"privacy_mode_enabled": True},
            )

        self.assertFalse(result)
        self.assertFalse(mod._sentry_initialized)
        fake_sdk.init.assert_not_called()


if __name__ == "__main__":
    unittest.main()
