"""Настройки C2b «спикеры-лайт»: дефолты, клампы, рубильник.

Спека: docs/superpowers/specs/2026-07-10-c2-live-meeting-overlay-design.md §2.8 + §2.5a.
"""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import DEFAULT_SETTINGS  # noqa: E402
from backend.settings_validator import SettingsValidator  # noqa: E402


class MeetingSpeakerSettingsTests(unittest.TestCase):
    def test_defaults_present(self):
        self.assertEqual(DEFAULT_SETTINGS["meeting_diar_interval_sec"], 90.0)
        self.assertEqual(DEFAULT_SETTINGS["meeting_diar_window_sec"], 90.0)
        self.assertEqual(DEFAULT_SETTINGS["meeting_speaker_match_threshold"], 0.72)
        self.assertIs(DEFAULT_SETTINGS["meeting_live_speakers_enabled"], True)

    def test_range_clamping(self):
        v = SettingsValidator()
        result = v.validate({
            "meeting_diar_interval_sec": 1.0,          # ниже минимума 60
            "meeting_diar_window_sec": 999.0,          # выше максимума 180
            "meeting_speaker_match_threshold": 0.1,    # ниже минимума 0.5
        })
        s = result.fixed
        self.assertEqual(s["meeting_diar_interval_sec"], 60.0)
        self.assertEqual(s["meeting_diar_window_sec"], 180.0)
        self.assertEqual(s["meeting_speaker_match_threshold"], 0.5)

    def test_bool_field_normalized(self):
        v = SettingsValidator()
        result = v.validate({"meeting_live_speakers_enabled": "false"})
        s = result.fixed
        self.assertIs(s["meeting_live_speakers_enabled"], False)


if __name__ == "__main__":
    unittest.main()
