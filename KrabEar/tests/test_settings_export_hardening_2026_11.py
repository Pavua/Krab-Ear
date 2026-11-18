"""E5 п.2: export_settings не перезаписывает чужие файлы и пишет 0600.

Раньше `handle_export_settings` открывал целевой файл в режиме "w": любой существующий файл
из allowlist (включая `~/.krab_ear_data/settings.json` / `history.ndjson`) затирался JSON-ом
настроек, а права получались по umask (0644). Теперь: O_EXCL|O_NOFOLLOW, mode 0600, каталоги
данных Ear из экспорта исключены. HOME подменяется на временный каталог — тест никогда не
пишет в реальный домашний каталог.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.settings_service import SettingsService  # noqa: E402


def _svc() -> SettingsService:
    store = MagicMock()
    store.load_settings.return_value = {"quality_profile": "balanced", "hf_token": "secret"}
    return SettingsService(store=store)


class ExportSettingsHardeningTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name).resolve() / "home"
        self.home.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # Целевые каталоги внутри временной директории (она в allowlist через gettempdir()).
        self.out_dir = Path(self._tmp.name).resolve() / "out"
        self.out_dir.mkdir()

    def test_existing_file_is_never_overwritten(self):
        victim = self.out_dir / "important.json"
        victim.write_text("KEEP-ME", encoding="utf-8")
        with self.assertRaises((RuntimeError, FileExistsError)):
            _svc().handle_export_settings({"file": str(victim)})
        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP-ME")

    def test_symlink_target_is_refused_and_victim_untouched(self):
        victim = self.out_dir / "victim.txt"
        victim.write_text("KEEP-ME", encoding="utf-8")
        link = self.out_dir / "link.json"
        link.symlink_to(victim)
        with self.assertRaises((RuntimeError, FileExistsError, OSError)):
            _svc().handle_export_settings({"file": str(link)})
        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP-ME")

    def test_new_file_has_mode_0600_and_valid_json(self):
        target = self.out_dir / "fresh.json"
        result = _svc().handle_export_settings({"file": str(target)})
        self.assertEqual(Path(result["file"]), target)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["quality_profile"], "balanced")
        self.assertNotIn("hf_token", data)

    def test_default_path_is_created_in_home_with_mode_0600(self):
        result = _svc().handle_export_settings({})
        path = Path(result["file"])
        self.assertEqual(path.parent, self.home)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ear_data_directories_are_excluded(self):
        for rel in (".krab_ear_data/settings_export.json",
                    "Library/Application Support/KrabEar/settings_export.json"):
            with self.subTest(rel=rel):
                target = self.home / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with self.assertRaises(RuntimeError):
                    _svc().handle_export_settings({"file": str(target)})
                self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
