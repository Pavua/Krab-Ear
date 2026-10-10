"""Битый settings.json не равен privacy OFF: общий load_settings fail-closed по приватности.

Раньше при нечитаемом JSON / дубликате ключа load_settings отдавал DEFAULT_SETTINGS
с privacy_mode_enabled=False, и все privacy-гейты (читают через cached_settings)
открывались. Теперь неизвестность трактуется как privacy ON — тот же принцип, что
у SettingsService fallback при контенции лока. Отсутствующий файл — новый профиль,
privacy остаётся по дефолту.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import DEFAULT_SETTINGS  # noqa: E402
from backend.state_store import StateStore  # noqa: E402


class CorruptSettingsPrivacyFailClosedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")

    def _load_with(self, text: str) -> dict:
        self.store.settings_path.write_text(text, encoding="utf-8")
        return self.store.load_settings()

    def test_default_privacy_is_off_so_the_test_is_meaningful(self) -> None:
        self.assertFalse(DEFAULT_SETTINGS["privacy_mode_enabled"])

    def test_invalid_json_forces_privacy_on(self) -> None:
        self.assertTrue(self._load_with("NOT_JSON{{{")["privacy_mode_enabled"])

    def test_empty_file_forces_privacy_on(self) -> None:
        self.assertTrue(self._load_with("")["privacy_mode_enabled"])

    def test_duplicate_key_forces_privacy_on(self) -> None:
        text = '{"privacy_mode_enabled": true, "privacy_mode_enabled": false}'
        self.assertTrue(self._load_with(text)["privacy_mode_enabled"])

    def test_corrupt_fallback_still_carries_all_default_keys(self) -> None:
        loaded = self._load_with("NOT_JSON{{{")
        for key in DEFAULT_SETTINGS:
            self.assertIn(key, loaded)

    def test_missing_file_is_new_profile_and_keeps_default(self) -> None:
        self.assertFalse(self.store.load_settings()["privacy_mode_enabled"])

    def test_valid_file_with_explicit_off_is_honoured(self) -> None:
        self.assertFalse(self._load_with('{"privacy_mode_enabled": false}')["privacy_mode_enabled"])


if __name__ == "__main__":
    unittest.main()
