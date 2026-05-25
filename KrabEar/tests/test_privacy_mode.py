"""Tests for Phase D.5 Privacy Mode toggle.

Covers:
  - init_sentry() returns False (no-op) when privacy_mode_enabled=True.
  - init_sentry() proceeds normally when privacy_mode_enabled=False.
  - handle_translate_text() / handle_translate_selection() force
    network_mode to "offline_only" when privacy_mode_enabled=True.
  - DEFAULT_SETTINGS contains privacy_mode_enabled=False (backward compat).
"""

import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup — allow running standalone from repo root.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_KRAB_EAR = os.path.dirname(_HERE)
if _KRAB_EAR not in sys.path:
    sys.path.insert(0, _KRAB_EAR)


class TestInitSentryPrivacyMode(unittest.TestCase):
    """init_sentry() must be a no-op when privacy_mode_enabled=True."""

    def _call(self, dsn=None, settings=None):
        # Import fresh each time to avoid _sentry_initialized global pollution.
        import backend.observability as obs_module
        obs_module._sentry_initialized = False
        return obs_module.init_sentry(dsn=dsn, settings=settings)

    def test_init_sentry_no_op_when_privacy_mode(self):
        """privacy_mode=True, valid DSN → returns False, Sentry not initialised."""
        result = self._call(
            dsn="https://abc@o123.ingest.sentry.io/456",
            settings={"privacy_mode_enabled": True},
        )
        self.assertFalse(result, "Expected False (privacy mode skips Sentry)")

    def test_init_sentry_normal_when_privacy_mode_false(self):
        """privacy_mode=False, valid DSN → proceeds to Sentry init attempt (True on success)."""
        # We patch sentry_sdk so the test doesn't need the real package.
        fake_sentry = types.ModuleType("sentry_sdk")
        fake_sentry.init = MagicMock()

        with patch.dict("sys.modules", {"sentry_sdk": fake_sentry}):
            result = self._call(
                dsn="https://abc@o123.ingest.sentry.io/456",
                settings={"privacy_mode_enabled": False},
            )
        self.assertTrue(result, "Expected True when privacy mode is disabled and DSN valid")
        fake_sentry.init.assert_called_once()

    def test_init_sentry_no_dsn_still_false(self):
        """No DSN → always False regardless of privacy setting."""
        result = self._call(dsn=None, settings={"privacy_mode_enabled": False})
        self.assertFalse(result)

    def test_init_sentry_privacy_mode_true_no_dsn(self):
        """Both privacy mode and no DSN → False without touching sentry_sdk."""
        fake_sentry = types.ModuleType("sentry_sdk")
        fake_sentry.init = MagicMock()
        with patch.dict("sys.modules", {"sentry_sdk": fake_sentry}):
            result = self._call(dsn=None, settings={"privacy_mode_enabled": True})
        self.assertFalse(result)
        fake_sentry.init.assert_not_called()

    def test_init_sentry_no_settings_dict(self):
        """settings=None → privacy check skipped, normal DSN-check path."""
        result = self._call(dsn=None, settings=None)
        self.assertFalse(result)


class TestTranslationServicePrivacyMode(unittest.TestCase):
    """handle_translate_text / handle_translate_selection force offline_only on privacy."""

    def _make_service(self, privacy_mode: bool, initial_network_mode: str = "online_preferred"):
        """Build a TranslationService stub with controlled settings."""
        from backend.translation_service import TranslationService

        # Minimal translator stub that records what it was called with.
        translator = MagicMock()
        result = MagicMock()
        result.text = "translated"
        result.status = "ok"
        result.source_lang = "ru"
        result.target_lang = "es"
        result.mode = "ru_to_es"
        result.engine = "argos"
        translator.translate.return_value = result

        settings = {
            "privacy_mode_enabled": privacy_mode,
            "network_mode": initial_network_mode,
            "translation_style": "neutral",
            "translation_glossary": {},
        }

        store = MagicMock()
        store.get_history_page.return_value = ([], None)
        store.load_vocabulary.return_value = []

        service = TranslationService(
            translator=translator,
            store=store,
            cached_settings=lambda: settings,
            invalidate_settings_cache=lambda: None,
            vocabulary_store=None,
        )
        return service, translator

    def test_translate_text_forces_offline_when_privacy_mode(self):
        """network_mode=online_preferred, privacy=True → translate called with offline_only."""
        service, translator = self._make_service(
            privacy_mode=True, initial_network_mode="online_preferred"
        )
        service.handle_translate_text({
            "text": "Привет мир",
            "translation_mode": "ru_to_es",
        })
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs.get("network_mode"), "offline_only",
                         "Privacy mode must force offline_only in translate_text")

    def test_translate_text_keeps_mode_when_privacy_off(self):
        """privacy=False → network_mode preserved from settings."""
        service, translator = self._make_service(
            privacy_mode=False, initial_network_mode="online_preferred"
        )
        service.handle_translate_text({
            "text": "Hello world",
            "translation_mode": "en_to_ru",
        })
        _, kwargs = translator.translate.call_args
        # Should NOT be overridden to offline_only.
        self.assertNotEqual(kwargs.get("network_mode"), "offline_only",
                            "Privacy mode off — should not force offline_only")

    def test_translate_selection_forces_offline_when_privacy_mode(self):
        """Selection translate: privacy=True → offline_only forced."""
        service, translator = self._make_service(
            privacy_mode=True, initial_network_mode="online_preferred"
        )
        service.handle_translate_selection({
            "text": "Добрый день",
            "source_lang": "ru",
            "target_lang": "es",
        })
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs.get("network_mode"), "offline_only",
                         "Privacy mode must force offline_only in translate_selection")

    def test_translate_selection_keeps_mode_when_privacy_off(self):
        """Selection translate: privacy=False → network_mode from settings."""
        service, translator = self._make_service(
            privacy_mode=False, initial_network_mode="offline_default"
        )
        service.handle_translate_selection({
            "text": "Buenos días",
            "source_lang": "es",
            "target_lang": "ru",
        })
        _, kwargs = translator.translate.call_args
        self.assertEqual(kwargs.get("network_mode"), "offline_default",
                         "Should use settings network_mode when privacy off")


class TestDefaultSettingPrivacyMode(unittest.TestCase):
    """DEFAULT_SETTINGS must have privacy_mode_enabled=False (backward compat)."""

    def test_default_setting_is_false(self):
        from core.config import DEFAULT_SETTINGS
        self.assertIn(
            "privacy_mode_enabled",
            DEFAULT_SETTINGS,
            "privacy_mode_enabled must be present in DEFAULT_SETTINGS",
        )
        self.assertFalse(
            DEFAULT_SETTINGS["privacy_mode_enabled"],
            "Default value must be False (opt-in by user)",
        )


if __name__ == "__main__":
    unittest.main()
