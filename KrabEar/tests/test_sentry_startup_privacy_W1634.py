"""Tests for W1634 / W1631 F2 HIGH: init_sentry at startup must receive settings dict.

Two scenarios verified:
1. test_init_sentry_called_with_settings_dict_at_startup — main() passes a
   settings= dict to init_sentry() so that persisted privacy_mode is respected.
2. test_sentry_skipped_when_settings_has_privacy_mode_enabled — when settings.json
   has privacy_mode_enabled=True, Sentry SDK is NOT initialized on backend start.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

PROJECT_ROOT = __file__
for _ in range(3):
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


# ---------------------------------------------------------------------------
# Test 1: main() passes settings= dict to init_sentry
# ---------------------------------------------------------------------------

class TestInitSentryCalledWithSettingsDictAtStartup(unittest.TestCase):
    """W1634: main() must pass settings= dict to init_sentry at startup."""

    def setUp(self):
        _reset_observability()

    def test_init_sentry_called_with_settings_dict_at_startup(self):
        """main() passes a non-None settings dict to init_sentry() at startup.

        Verifies W1634 fix: before the fix, settings= was absent, so
        privacy_mode_enabled in settings.json was silently ignored at backend
        start. After the fix, init_sentry receives the dict loaded from
        StateStore.load_settings() before the call.
        """
        import backend.service as svc_mod  # noqa: PLC0415

        # Intercept init_sentry to capture keyword args.
        captured_calls: list[dict] = []

        def _fake_init_sentry(**kwargs):
            captured_calls.append(dict(kwargs))
            return False  # no-op (no DSN)

        with tempfile.TemporaryDirectory() as tmp:
            # Prepare a StateStore with an explicit settings dict that includes
            # privacy_mode_enabled so we can confirm it's passed through.
            from backend.state_store import StateStore  # noqa: PLC0415
            store = StateStore(data_dir=Path(tmp))
            store.save_settings({"privacy_mode_enabled": False, "sentry_dsn": ""})

            with (
                patch.object(svc_mod, "init_sentry", side_effect=_fake_init_sentry),
                patch.object(svc_mod, "configure_logging"),
                patch.object(svc_mod, "install_signal_handlers"),
                patch.object(svc_mod, "default_data_dir", return_value=Path(tmp)),
                patch.object(svc_mod, "default_socket_path", return_value=Path(tmp) / "test.sock"),
                patch.object(svc_mod, "get_release_string", return_value="krab-ear@test"),
                patch("sys.argv", ["main"]),
                # Prevent server.serve_forever from blocking by making IPCServer
                # raise immediately when serve_forever() is called.
                patch.object(
                    svc_mod.IPCServer, "__init__", return_value=None
                ),
                patch.object(
                    svc_mod.IPCServer, "serve_forever", side_effect=KeyboardInterrupt
                ),
                patch.object(svc_mod.IPCServer, "stop", return_value=None),
                patch.object(svc_mod.BackendService, "close", return_value=None),
            ):
                try:
                    svc_mod.main()
                except (KeyboardInterrupt, SystemExit):
                    pass

        self.assertEqual(
            len(captured_calls), 1,
            "init_sentry must be called exactly once in main()",
        )
        call_kwargs = captured_calls[0]
        self.assertIn(
            "settings",
            call_kwargs,
            "init_sentry must be called with settings= kwarg (W1634 fix)",
        )
        self.assertIsNotNone(
            call_kwargs["settings"],
            "settings= must not be None — should be the dict from StateStore",
        )
        self.assertIsInstance(
            call_kwargs["settings"],
            dict,
            "settings= must be a dict loaded from settings.json",
        )


# ---------------------------------------------------------------------------
# Test 2: Sentry skipped when settings.json has privacy_mode_enabled=True
# ---------------------------------------------------------------------------

class TestSentrySkippedWhenSettingsHasPrivacyModeEnabled(unittest.TestCase):
    """W1634: Sentry must NOT init when settings.json has privacy_mode_enabled=True."""

    def setUp(self):
        _reset_observability()

    def test_sentry_skipped_when_settings_has_privacy_mode_enabled(self):
        """Sentry SDK init() not called when settings.json has privacy_mode_enabled=True.

        Reproduces W1631 F2 HIGH: user saves privacy_mode_enabled=True to settings.json,
        restarts backend — before the fix, Sentry initialized anyway because the
        settings= dict was never passed to init_sentry(). After the fix, the
        persisted privacy mode is read from StateStore and passed to init_sentry(),
        which then skips SDK initialization.
        """
        import backend.service as svc_mod  # noqa: PLC0415

        fake_sdk = _make_fake_sentry_sdk()

        with tempfile.TemporaryDirectory() as tmp:
            # Write settings.json with privacy_mode_enabled=True AND a DSN.
            # Without the fix, Sentry would still be initialized (settings= absent).
            from backend.state_store import StateStore  # noqa: PLC0415
            store = StateStore(data_dir=Path(tmp))
            store.save_settings({
                "privacy_mode_enabled": True,
                "sentry_dsn": "https://fake@sentry.io/123",
            })

            import backend.observability as obs_mod  # noqa: PLC0415
            obs_mod._sentry_initialized = False

            with (
                patch.dict(sys.modules, {"sentry_sdk": fake_sdk}),
                patch.object(svc_mod, "configure_logging"),
                patch.object(svc_mod, "install_signal_handlers"),
                patch.object(svc_mod, "default_data_dir", return_value=Path(tmp)),
                patch.object(svc_mod, "default_socket_path", return_value=Path(tmp) / "test.sock"),
                patch.object(svc_mod, "get_release_string", return_value="krab-ear@test"),
                # Patch settings object so DSN is present (env-level DSN).
                patch.object(svc_mod.settings, "SENTRY_DSN", "https://fake@sentry.io/123"),
                patch.object(svc_mod.settings, "SENTRY_ENVIRONMENT", "test"),
                patch("sys.argv", ["main"]),
                patch.object(svc_mod.IPCServer, "__init__", return_value=None),
                patch.object(
                    svc_mod.IPCServer, "serve_forever", side_effect=KeyboardInterrupt
                ),
                patch.object(svc_mod.IPCServer, "stop", return_value=None),
                patch.object(svc_mod.BackendService, "close", return_value=None),
            ):
                try:
                    svc_mod.main()
                except (KeyboardInterrupt, SystemExit):
                    pass

        # sentry_sdk.init must NOT have been called because privacy_mode=True.
        fake_sdk.init.assert_not_called()
        self.assertFalse(
            obs_mod._sentry_initialized,
            "Sentry must not be initialized when privacy_mode_enabled=True",
        )


if __name__ == "__main__":
    unittest.main()
