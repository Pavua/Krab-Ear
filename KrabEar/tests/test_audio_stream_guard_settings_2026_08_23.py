"""Настройки guarded read: killswitch + клампы (спека §9, T1)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class GuardedReadSettingsTest(unittest.TestCase):
    def test_defaults_present(self):
        from core.config import DEFAULT_SETTINGS

        self.assertIs(DEFAULT_SETTINGS["audio_guarded_read_enabled"], True)
        self.assertEqual(DEFAULT_SETTINGS["audio_read_poll_sec"], 0.05)
        self.assertEqual(DEFAULT_SETTINGS["audio_stream_starve_sec"], 3.0)

    def test_poll_sec_is_range_clamped(self):
        """🔴 wait(timeout<=0) = CPU-spin: ноль/минус обязаны подниматься."""
        from backend.settings_validator import _RANGE_FIELDS

        low, high, default, kind = _RANGE_FIELDS["audio_read_poll_sec"]
        self.assertGreater(low, 0.0)
        self.assertIs(kind, float)
        self.assertLessEqual(low, default <= high and default or high)

    def test_starve_sec_is_range_clamped(self):
        from backend.settings_validator import _RANGE_FIELDS

        low, high, default, kind = _RANGE_FIELDS["audio_stream_starve_sec"]
        self.assertGreaterEqual(low, 1.0, "ниже прогрева стрима — ложные срабатывания")
        self.assertIs(kind, float)

    def test_validator_clamps_out_of_range_values(self):
        from backend.settings_validator import SettingsValidator

        res = SettingsValidator().validate({
            "audio_read_poll_sec": 0.0,          # CPU-spin, если пропустить
            "audio_stream_starve_sec": 999.0,    # «детектор», который не сработает
        })
        self.assertGreater(res.fixed["audio_read_poll_sec"], 0.0)
        self.assertLessEqual(res.fixed["audio_stream_starve_sec"], 60.0)


if __name__ == "__main__":
    unittest.main()
