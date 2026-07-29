"""M2: рубильник in-process REST присутствует в обоих источниках правды."""
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parents[1])
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.config import DEFAULT_SETTINGS, Settings  # noqa: E402


class RestInProcessSettingTest(unittest.TestCase):
    def test_pydantic_field_defaults_to_false(self):
        s = Settings()
        self.assertIs(s.REST_IN_PROCESS_ENABLED, False)

    def test_env_override_turns_it_on(self):
        old = os.environ.get("KRAB_EAR_REST_IN_PROCESS_ENABLED")
        os.environ["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = "true"
        try:
            self.assertIs(Settings().REST_IN_PROCESS_ENABLED, True)
        finally:
            if old is None:
                os.environ.pop("KRAB_EAR_REST_IN_PROCESS_ENABLED", None)
            else:
                os.environ["KRAB_EAR_REST_IN_PROCESS_ENABLED"] = old

    def test_default_settings_key_present_and_false(self):
        self.assertIn("rest_in_process_enabled", DEFAULT_SETTINGS)
        self.assertIs(DEFAULT_SETTINGS["rest_in_process_enabled"], False)


if __name__ == "__main__":
    unittest.main()
