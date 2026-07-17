"""Настройки C3a «Быстрые заметки»: дефолты, bool-поля, allowlist хоткея.

Спека: docs/superpowers/specs/2026-07-16-c3-quick-capture-design.md §3.3.
План: docs/superpowers/plans/2026-07-16-c3a-quick-capture.md, Task 3.
"""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DEFAULT_SETTINGS  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402


class QuickCaptureSettingsTests(unittest.TestCase):
    def test_defaults_present(self):
        self.assertIs(DEFAULT_SETTINGS["quick_capture_send_to_notes"], False)
        self.assertIs(DEFAULT_SETTINGS["quick_capture_obsidian_sync"], False)
        self.assertIs(DEFAULT_SETTINGS["quick_capture_show_panel"], False)
        self.assertEqual(DEFAULT_SETTINGS["quick_capture_hotkey"], "cmd_shift_n")

    def test_bool_fields_normalized(self):
        v = SettingsValidator()
        result = v.validate({
            "quick_capture_send_to_notes": "true",
            "quick_capture_obsidian_sync": "false",
            "quick_capture_show_panel": 1,
        })
        s = result.fixed
        self.assertIs(s["quick_capture_send_to_notes"], True)
        self.assertIs(s["quick_capture_obsidian_sync"], False)
        self.assertIs(s["quick_capture_show_panel"], True)

    def test_hotkey_allowlist_accepts_all_three_combos(self):
        v = SettingsValidator()
        for combo in ("cmd_shift_n", "cmd_opt_n", "ctrl_shift_n"):
            result = v.validate({"quick_capture_hotkey": combo})
            self.assertEqual(result.fixed["quick_capture_hotkey"], combo)
            self.assertEqual(result.warnings, [])

    def test_hotkey_invalid_value_falls_back_to_default(self):
        v = SettingsValidator()
        result = v.validate({"quick_capture_hotkey": "cmd_alt_del"})
        self.assertEqual(result.fixed["quick_capture_hotkey"], "cmd_shift_n")
        self.assertTrue(any("quick_capture_hotkey" in w for w in result.warnings))


if __name__ == "__main__":
    unittest.main()
