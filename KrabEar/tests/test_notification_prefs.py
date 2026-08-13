"""Tests for notification preferences management in SettingsService."""

from core.config import DEFAULT_SETTINGS
from backend.settings_service import SettingsService
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "KrabEar"))


class FakeStore:
    """Минимальный stub для StateStore."""

    def __init__(self):
        self._settings = dict(DEFAULT_SETTINGS)

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False):
        return dict(self._settings)

    def save_settings(self, settings):
        self._settings = dict(settings)
        return dict(settings)


class TestNotificationPrefsDefaults(unittest.TestCase):
    """handle_get_notification_preferences возвращает правильные дефолты."""

    def setUp(self):
        self.store = FakeStore()
        self.svc = SettingsService(self.store)

    def test_get_returns_all_fields(self):
        prefs = self.svc.handle_get_notification_preferences({})
        expected_keys = {
            "notifications_enabled",
            "notify_on_low_confidence",
            "notify_confidence_threshold",
            "notify_on_llm_failure",
            "notify_on_import_complete",
            "notify_sound_enabled",
        }
        self.assertEqual(set(prefs.keys()), expected_keys)

    def test_default_notifications_enabled_is_true(self):
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertTrue(prefs["notifications_enabled"])

    def test_default_confidence_threshold(self):
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertAlmostEqual(prefs["notify_confidence_threshold"], 0.5)

    def test_default_notify_on_llm_failure_is_true(self):
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertTrue(prefs["notify_on_llm_failure"])


class TestSetNotificationPrefs(unittest.TestCase):
    """handle_set_notification_preferences сохраняет и возвращает обновлённые значения."""

    def setUp(self):
        self.store = FakeStore()
        self.svc = SettingsService(self.store)

    def test_disable_master_switch(self):
        self.svc.handle_set_notification_preferences({"notifications_enabled": False})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertFalse(prefs["notifications_enabled"])

    def test_set_confidence_threshold(self):
        self.svc.handle_set_notification_preferences({"notify_confidence_threshold": 0.75})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertAlmostEqual(prefs["notify_confidence_threshold"], 0.75)

    def test_confidence_threshold_clamped_to_max(self):
        self.svc.handle_set_notification_preferences({"notify_confidence_threshold": 1.5})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertAlmostEqual(prefs["notify_confidence_threshold"], 1.0)

    def test_confidence_threshold_clamped_to_min(self):
        self.svc.handle_set_notification_preferences({"notify_confidence_threshold": -0.1})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertAlmostEqual(prefs["notify_confidence_threshold"], 0.0)

    def test_partial_update_preserves_other_fields(self):
        # Disable just one field; the rest should stay at defaults.
        self.svc.handle_set_notification_preferences({"notify_sound_enabled": False})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertFalse(prefs["notify_sound_enabled"])
        self.assertTrue(prefs["notifications_enabled"])
        self.assertTrue(prefs["notify_on_import_complete"])

    def test_bool_coerce_string_false(self):
        self.svc.handle_set_notification_preferences({"notify_on_low_confidence": "false"})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertFalse(prefs["notify_on_low_confidence"])

    def test_bool_coerce_string_true(self):
        # First disable, then re-enable via string "true".
        self.svc.handle_set_notification_preferences({"notifications_enabled": False})
        self.svc.handle_set_notification_preferences({"notifications_enabled": "true"})
        prefs = self.svc.handle_get_notification_preferences({})
        self.assertTrue(prefs["notifications_enabled"])

    def test_set_persists_to_store(self):
        self.svc.handle_set_notification_preferences({"notify_on_import_complete": False})
        # Reload via a fresh service pointing at same store.
        svc2 = SettingsService(self.store)
        prefs = svc2.handle_get_notification_preferences({})
        self.assertFalse(prefs["notify_on_import_complete"])


class TestNotificationPrefsInDefaultSettings(unittest.TestCase):
    """DEFAULT_SETTINGS содержит все поля уведомлений с корректными значениями."""

    def test_all_notification_fields_present(self):
        for field in (
            "notifications_enabled",
            "notify_on_low_confidence",
            "notify_confidence_threshold",
            "notify_on_llm_failure",
            "notify_on_import_complete",
            "notify_sound_enabled",
        ):
            self.assertIn(field, DEFAULT_SETTINGS, msg=f"Missing field: {field}")

    def test_confidence_threshold_default_is_float(self):
        self.assertIsInstance(DEFAULT_SETTINGS["notify_confidence_threshold"], float)


if __name__ == "__main__":
    unittest.main()
