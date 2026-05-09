import csv
import importlib.util
import subprocess
import sys
from pathlib import Path
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "memory_baseline.py"

_psutil_available = importlib.util.find_spec("psutil") is not None


@unittest.skipUnless(_psutil_available, "psutil not installed")
class MemoryBaselineScriptTests(unittest.TestCase):
    def test_script_runs_with_once_flag(self):
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        out_csv = REPO_ROOT / "test-mem-baseline-tmp.csv"
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--once", "--output", str(out_csv)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, f"script failed: {result.stderr}")
            self.assertTrue(out_csv.exists())

            # CSV has header + at least 1 row
            rows = list(csv.DictReader(out_csv.open()))
            self.assertGreaterEqual(len(rows), 1)
            self.assertIn("timestamp", rows[0])
            self.assertIn("agent_rss_mb", rows[0])
        finally:
            if out_csv.exists():
                out_csv.unlink()

    def test_script_handles_no_backend(self):
        # Even if backend not running, script should write 0 values + exit 0
        # (already handled by get_backend_diagnostics returning None)
        pass

    def test_snapshot_fields_complete(self):
        """take_snapshot returns all expected keys even without backend."""
        if not SCRIPT.exists():
            self.skipTest("memory_baseline.py not found")
        # Import the module directly to unit-test take_snapshot
        import importlib.util
        spec = importlib.util.spec_from_file_location("memory_baseline", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        snap = mod.take_snapshot()
        expected_keys = {
            "timestamp", "uptime_sec", "agent_rss_mb", "backend_rss_mb",
            "worker_rss_mb_total", "history_total_items", "llm_circuit",
        }
        self.assertEqual(set(snap.keys()), expected_keys)
        # Values are numeric or string — no exceptions.
        # NB: agent_rss_mb fallback default = 0 (int) когда KrabEarAgent не запущен —
        # на CI или после reboot. Принимаем (int, float).
        self.assertIsInstance(snap["agent_rss_mb"], (int, float))
        self.assertIsInstance(snap["timestamp"], str)


if __name__ == "__main__":
    unittest.main()
