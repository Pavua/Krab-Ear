"""R1: launchd-обвязка ежедневного сканера надёжности существует и корректна."""
from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEMPLATE = REPO / "KrabEar" / "launchagents" / "ai.krab.ear.reliability.plist.template"
SCRIPT = REPO / "scripts" / "reliability_scan.py"
INSTALLER = REPO / "scripts" / "install_reliability_scanner.command"


class ReliabilityLaunchdTest(unittest.TestCase):
    def test_template_structure(self):
        with open(TEMPLATE, "rb") as fh:
            pl = plistlib.load(fh)
        self.assertEqual(pl["Label"], "ai.krab.ear.reliability")
        self.assertEqual(pl["StartCalendarInterval"], {"Hour": 6, "Minute": 0})
        self.assertFalse(pl.get("RunAtLoad", True))
        self.assertFalse(pl.get("KeepAlive", True))
        self.assertEqual(pl.get("ProcessType"), "Background")
        self.assertTrue(pl.get("LowPriorityIO"))
        args = pl["ProgramArguments"]
        script_args = [a for a in args if a.endswith("scripts/reliability_scan.py")]
        self.assertEqual(len(script_args), 1)
        self.assertIn("--once", args)
        self.assertIn(".venv_krab_ear", args[0])
        rendered = Path(script_args[0].replace("__PROJECT_ROOT__", str(REPO)))
        self.assertTrue(rendered.exists())

    def test_files_present(self):
        self.assertTrue(SCRIPT.exists())
        self.assertTrue(INSTALLER.exists())

    def test_installer_mentions_lint_and_label(self):
        body = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("plutil -lint", body)
        self.assertIn("ai.krab.ear.reliability", body)
        # verify StartCalendarInterval идёт по реальному формату launchd.
        self.assertIn('"Hour" => 6', body)
        self.assertIn('"Minute" => 0', body)


if __name__ == "__main__":
    unittest.main()
