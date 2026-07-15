"""Настройки/validator/error-код wake-word watchdog (спека 2026-07-15)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.error_codes import ERROR_REGISTRY  # noqa: E402
from backend.settings_validator import _BOOL_FIELDS, _RANGE_FIELDS  # noqa: E402
from core.config import DEFAULT_SETTINGS  # noqa: E402


class DefaultsTests(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIs(DEFAULT_SETTINGS["wake_word_watchdog_enabled"], True)
        self.assertEqual(DEFAULT_SETTINGS["wake_word_stale_sec"], 30.0)

    def test_validator_fields(self):
        self.assertIn("wake_word_watchdog_enabled", _BOOL_FIELDS)
        self.assertEqual(
            _RANGE_FIELDS["wake_word_stale_sec"], (10.0, 120.0, 30.0, float),
        )


class ErrorCodeTests(unittest.TestCase):
    def test_registry_entry(self):
        entry = ERROR_REGISTRY["audio.wakeword_wedged"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertIn("Wake word", entry["user_msg_ru"])
        self.assertEqual(entry["dedupe_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
