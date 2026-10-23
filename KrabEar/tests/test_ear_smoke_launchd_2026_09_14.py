"""R4: launchd-обвязка Ear-smoke существует и корректна."""
from __future__ import annotations

import importlib.util
import plistlib
import subprocess
from unittest.mock import patch
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


class SmokeCheckerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("ear_smoke_tested", SCRIPT)
        cls.smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.smoke)

    def test_process_query_error_does_not_trigger_recovery(self):
        result = subprocess.CompletedProcess([], 2, stdout="", stderr="query failed")
        with patch.object(self.smoke.subprocess, "run", return_value=result) as run:
            ok, _ = self.smoke.check_agent({})
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 1, "ошибка проверки не разрешает recovery")

    def test_valid_process_queries_keep_exact_count(self):
        cases = [(1, "", 0), (0, "123\n", 1), (0, "123\n456\n", 2)]
        for code, stdout, expected in cases:
            with self.subTest(code=code, count=expected):
                result = subprocess.CompletedProcess([], code, stdout=stdout)
                with patch.object(self.smoke.subprocess, "run", return_value=result):
                    self.assertEqual(self.smoke.agent_count(), expected)

    def test_single_recovered_agent_remains_successful(self):
        with patch.object(self.smoke, "agent_count", side_effect=[0, 1]), \
             patch.object(self.smoke.subprocess, "run"):
            ctx = {}
            self.assertTrue(self.smoke.check_agent(ctx)[0])
            self.assertTrue(ctx["recovered"])

    def test_recovery_requires_exactly_one_agent(self):
        with patch.object(self.smoke, "agent_count", side_effect=[0, 2]), \
             patch.object(self.smoke.subprocess, "run"):
            ctx = {}
            ok, _ = self.smoke.check_agent(ctx)
        self.assertFalse(ok)
        self.assertFalse(ctx.get("recovered", False))

    def test_invalid_pid_output_is_unknown(self):
        result = subprocess.CompletedProcess([], 0, stdout="not-a-pid\n")
        with patch.object(self.smoke.subprocess, "run", return_value=result):
            self.assertEqual(self.smoke.agent_count(), -1)

    def test_failed_rpc_payloads_cannot_be_healthy(self):
        cases = [
            (self.smoke.check_diagnostics, dict.fromkeys(self.smoke.REQUIRED_DIAG, {})),
            (self.smoke.check_audio, {"count": 1}),
            (self.smoke.check_settings, dict.fromkeys(self.smoke.REQUIRED_SETTINGS, "x")),
        ]
        for check, payload in cases:
            with self.subTest(check=check.__name__), \
                 patch.object(self.smoke, "ipc", return_value={"ok": False, "result": payload}):
                self.assertFalse(check({})[0])

    def test_successful_rpc_payloads_remain_healthy(self):
        cases = [
            (self.smoke.check_diagnostics, dict.fromkeys(self.smoke.REQUIRED_DIAG, {})),
            (self.smoke.check_audio, {"count": 1}),
            (self.smoke.check_settings, dict.fromkeys(self.smoke.REQUIRED_SETTINGS, "x")),
        ]
        for check, payload in cases:
            with self.subTest(check=check.__name__), \
                 patch.object(self.smoke, "ipc", return_value={"ok": True, "result": payload}):
                self.assertTrue(check({})[0])


if __name__ == "__main__":
    unittest.main()
