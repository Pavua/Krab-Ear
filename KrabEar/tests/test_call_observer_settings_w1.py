"""Call Observer w1: два ключа настроек наблюдателя звонков."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DEFAULT_SETTINGS


class CallObserverSettingsTest(unittest.TestCase):
    def test_default_settings_keys(self):
        self.assertIs(DEFAULT_SETTINGS["call_observer_hud_enabled"], True)
        self.assertIs(DEFAULT_SETTINGS["call_observer_autoplay_audio"], False)

    def test_keys_are_not_sensitive(self):
        """Булы обязаны доходить до Swift через get_settings нередактированными."""
        from backend.settings_backup import SENSITIVE_FIELDS
        self.assertNotIn("call_observer_hud_enabled", SENSITIVE_FIELDS)
        self.assertNotIn("call_observer_autoplay_audio", SENSITIVE_FIELDS)


if __name__ == "__main__":
    unittest.main()
