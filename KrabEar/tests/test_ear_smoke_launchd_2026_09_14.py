"""R4: launchd-обвязка Ear-smoke существует и корректна."""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "KrabEar" / "launchagents" / "ai.krab.ear.e2e-smoke.plist.template"
SCRIPT = REPO / "scripts" / "ear_e2e_smoke.py"
INSTALLER = REPO / "scripts" / "install_ear_e2e_smoke.command"


class SmokeLaunchdTest(unittest.TestCase):
    def test_template_structure(self):
        with open(TEMPLATE, "rb") as fh:
            pl = plistlib.load(fh)
        self.assertEqual(pl["Label"], "ai.krab.ear.e2e-smoke")
        self.assertEqual(pl["StartInterval"], 21600)
        self.assertFalse(pl.get("RunAtLoad", True))
        self.assertEqual(pl.get("ProcessType"), "Background")
        args = pl["ProgramArguments"]
        self.assertTrue(args[-1].endswith("scripts/ear_e2e_smoke.py"))
        self.assertIn(".venv_krab_ear", args[0])

    def test_files_present(self):
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(INSTALLER.exists())


if __name__ == "__main__":
    unittest.main()
