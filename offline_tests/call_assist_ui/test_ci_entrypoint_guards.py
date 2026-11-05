"""Check that the standalone Swift harness cannot silently skip assertions."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


HERE = Path(__file__).resolve().parent
HARNESS = HERE / "test_response_state_swift.py"
SOURCE = HERE.parents[1] / "native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController+CallAssist.swift"
LAUNCH = r'''
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import patch
result = SimpleNamespace(returncode=0, stdout="PASS\n", stderr="")
with patch("subprocess.run", return_value=result):
    runpy.run_path(sys.argv[1], run_name="__main__")
'''


class EntrypointGuardTests(unittest.TestCase):
    def test_optimized_python_fails_before_harness(self):
        env = os.environ.copy()
        env.pop("CALL_ASSIST_UI_TEST_SOURCE", None)
        result = subprocess.run(
            [sys.executable, "-O", "-c", LAUNCH, str(HARNESS)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("optimized Python", result.stderr)
        self.assertNotIn("PASS state", result.stdout)

    def test_source_override_fails_before_harness(self):
        env = os.environ.copy()
        env["CALL_ASSIST_UI_TEST_SOURCE"] = str(SOURCE)
        result = subprocess.run(
            [sys.executable, "-c", LAUNCH, str(HARNESS)],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source override", result.stderr)
        self.assertNotIn("PASS state", result.stdout)


if __name__ == "__main__":
    unittest.main()
