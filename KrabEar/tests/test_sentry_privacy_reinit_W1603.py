"""Tests for W1603 / W1599 F2 MED: Sentry re-initialization when privacy_mode toggles OFF.

Three scenarios verified:
1. test_sentry_reinitialized_when_privacy_off_after_on  — toggle ON then OFF; SDK.init called.
2. test_sentry_idempotent_when_already_initialized      — privacy OFF, flag already True; no double-init.
3. test_sentry_skipped_when_no_dsn                     — privacy OFF but no DSN; no init called.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
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
    fake.add_breadcrumb = MagicMock()
    fake.capture_exception = MagicMock()
    return fake


def _make_minimal_store(settings_dict):
    """Return a mock StateStore that returns the given settings dict."""
    store = MagicMock()
    store.load_settings.return_value = dict(settings_dict)
    store.save_settings.side_effect = lambda s: dict(s)
    return store


# Minimal settings dict that passes SettingsService validation.
_BASE_SETTINGS = {
    "privacy_mode_enabled": False,
    "sentry_dsn": "https://fake@sentry.io/999",
    "mode": "headless",
    "quality_profile": "balanced",
    "cleanup_profile": "soft",
    "translation_mode": "off",
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
    "auto_paste": True,
    "play_start_sound": True,
    "realtime_preview_enabled": True,
    "translate_and_paste": False,
    "onboarding_completed": False,
    "audio_ducking_enabled": True,
    "silence_guard_enabled": True,
    "background_guard_enabled": True,
    "call_notify_default": True,
    "call_auto_summary": True,
    "history_focus_mode": True,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "voice_gateway_api_key": "",
}


class TestSentryReinitWhenPrivacyOff(unittest.TestCase):
    """W1603 / W1599 F2 MED: Sentry must be re-initialized when privacy_mode toggles OFF."""

    def setUp(self):
        _reset_observability()

    def _make_svc(self, initial_settings):
        from backend.settings_service import SettingsService  # noqa: PLC0415
        from backend.settings_backup import SettingsBackup  # noqa: PLC0415
        store = _make_minimal_store(initial_settings)
        backup = MagicMock(spec=SettingsBackup)
        backup.create_backup.return_value = "backup_id"
        return SettingsService(store=store, backup=backup)

    # -----------------------------------------------------------------------
    # Test 1: Re-init when privacy toggles OFF after ON
    # -----------------------------------------------------------------------

    def test_sentry_reinitialized_when_privacy_off_after_on(self):
        """W1603: _on_privacy_mode_off hook calls init_sentry when privacy_mode FALSE→TRUE→FALSE."""
        import backend.observability as obs  # noqa: PLC0415
        from backend.service import BackendService  # noqa: PLC0415
        from backend.state_store import StateStore  # noqa: PLC0415

        fake_sdk = _make_fake_sentry_sdk()

        # Build a minimal BackendService to get the hook registered.
        # We use a temp dir StateStore so tests don't touch the real store.
        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(data_dir=Path(tmpdir))
            # Start with privacy_mode=True (Sentry was disabled).
            initial = {**_BASE_SETTINGS, "privacy_mode_enabled": True}
            store.save_settings(initial)
            obs._sentry_initialized = False  # flag already cleared (W1601 did this)

            svc = BackendService(store=store)

            with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                    # Toggle privacy OFF — the hook should call init_sentry.
                    svc._settings_svc.handle_set_settings({"privacy_mode_enabled": False})

            # SDK.init must have been called once (re-initialization).
            fake_sdk.init.assert_called_once()
            # _sentry_initialized must be True now.
            self.assertTrue(obs._sentry_initialized)

    # -----------------------------------------------------------------------
    # Test 2: Idempotent — no double-init when already initialized
    # -----------------------------------------------------------------------

    def test_sentry_idempotent_when_already_initialized(self):
        """W1603: If _sentry_initialized is already True, privacy OFF toggle does not call sdk.init again."""
        import backend.observability as obs  # noqa: PLC0415
        from backend.service import BackendService  # noqa: PLC0415
        from backend.state_store import StateStore  # noqa: PLC0415

        fake_sdk = _make_fake_sentry_sdk()

        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(data_dir=Path(tmpdir))
            initial = {**_BASE_SETTINGS, "privacy_mode_enabled": True}
            store.save_settings(initial)

            svc = BackendService(store=store)

            # Simulate Sentry already initialized (e.g. re-init happened earlier).
            obs._sentry_initialized = True

            with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                    svc._settings_svc.handle_set_settings({"privacy_mode_enabled": False})

            # SDK.init must NOT be called — flag was already True (idempotency).
            fake_sdk.init.assert_not_called()
            # Flag stays True.
            self.assertTrue(obs._sentry_initialized)

    # -----------------------------------------------------------------------
    # Test 3: No re-init when DSN is absent
    # -----------------------------------------------------------------------

    def test_sentry_skipped_when_no_dsn(self):
        """W1603: If sentry_dsn is empty, re-init is skipped even when privacy toggles OFF."""
        import backend.observability as obs  # noqa: PLC0415
        from backend.service import BackendService  # noqa: PLC0415
        from backend.state_store import StateStore  # noqa: PLC0415

        fake_sdk = _make_fake_sentry_sdk()

        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(data_dir=Path(tmpdir))
            # Start with privacy ON and no DSN.
            initial = {**_BASE_SETTINGS, "privacy_mode_enabled": True, "sentry_dsn": ""}
            store.save_settings(initial)
            obs._sentry_initialized = False

            svc = BackendService(store=store)

            with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                    svc._settings_svc.handle_set_settings({"privacy_mode_enabled": False})

            # No DSN → no init.
            fake_sdk.init.assert_not_called()
            self.assertFalse(obs._sentry_initialized)

    # -----------------------------------------------------------------------
    # Test 4: No re-init when privacy stays OFF (no transition)
    # -----------------------------------------------------------------------

    def test_no_reinit_when_privacy_was_already_off(self):
        """W1603: Hook is a no-op when privacy_mode was already False (no transition)."""
        import backend.observability as obs  # noqa: PLC0415
        from backend.service import BackendService  # noqa: PLC0415
        from backend.state_store import StateStore  # noqa: PLC0415

        fake_sdk = _make_fake_sentry_sdk()

        import tempfile  # noqa: PLC0415
        with tempfile.TemporaryDirectory() as tmpdir:
            store = StateStore(data_dir=Path(tmpdir))
            # Privacy already OFF — no transition should occur.
            initial = {**_BASE_SETTINGS, "privacy_mode_enabled": False}
            store.save_settings(initial)
            obs._sentry_initialized = False

            svc = BackendService(store=store)

            with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
                with patch("core.config.reload_settings_from_json", return_value=0, create=True):
                    # Set some unrelated setting — privacy stays OFF.
                    svc._settings_svc.handle_set_settings({"quality_profile": "balanced"})

            # No transition TRUE→FALSE → no re-init.
            fake_sdk.init.assert_not_called()

    # -----------------------------------------------------------------------
    # Test 5: Unit-level hook logic (pure, no BackendService)
    # -----------------------------------------------------------------------

    def test_hook_logic_unit_toggle_on_to_off_with_dsn(self):
        """W1603: _on_privacy_mode_off hook logic in isolation — calls init_sentry when transitioning OFF."""
        import backend.observability as obs  # noqa: PLC0415

        obs._sentry_initialized = False
        fake_sdk = _make_fake_sentry_sdk()

        # Simulate the hook directly.
        def _on_privacy_mode_off(old: dict, new: dict) -> None:
            old_privacy = bool(old.get("privacy_mode_enabled", False))
            new_privacy = bool(new.get("privacy_mode_enabled", False))
            if old_privacy and not new_privacy:
                from backend.observability import init_sentry, is_sentry_initialized  # noqa: PLC0415
                if not is_sentry_initialized():
                    dsn = str(new.get("sentry_dsn", "")).strip()
                    if dsn:
                        init_sentry(dsn=dsn, settings=new)

        old_settings = {"privacy_mode_enabled": True, "sentry_dsn": "https://fake@sentry.io/1"}
        new_settings = {"privacy_mode_enabled": False, "sentry_dsn": "https://fake@sentry.io/1"}

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            _on_privacy_mode_off(old_settings, new_settings)

        fake_sdk.init.assert_called_once()
        self.assertTrue(obs._sentry_initialized)

    def test_hook_logic_unit_toggle_off_to_on_no_init(self):
        """W1603: Hook must NOT call init_sentry when transitioning ON (privacy enabling)."""
        import backend.observability as obs  # noqa: PLC0415

        obs._sentry_initialized = False
        fake_sdk = _make_fake_sentry_sdk()

        def _on_privacy_mode_off(old: dict, new: dict) -> None:
            old_privacy = bool(old.get("privacy_mode_enabled", False))
            new_privacy = bool(new.get("privacy_mode_enabled", False))
            if old_privacy and not new_privacy:
                from backend.observability import init_sentry, is_sentry_initialized  # noqa: PLC0415
                if not is_sentry_initialized():
                    dsn = str(new.get("sentry_dsn", "")).strip()
                    if dsn:
                        init_sentry(dsn=dsn, settings=new)

        # Transition is OFF → ON (privacy enabling), not ON → OFF.
        old_settings = {"privacy_mode_enabled": False, "sentry_dsn": "https://fake@sentry.io/1"}
        new_settings = {"privacy_mode_enabled": True, "sentry_dsn": "https://fake@sentry.io/1"}

        with patch.dict(sys.modules, {"sentry_sdk": fake_sdk}):
            _on_privacy_mode_off(old_settings, new_settings)

        fake_sdk.init.assert_not_called()
        self.assertFalse(obs._sentry_initialized)


if __name__ == "__main__":
    unittest.main()
