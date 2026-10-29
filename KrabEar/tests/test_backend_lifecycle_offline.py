"""Bounded parent полного offline import/constructor/idle-close, без runtime."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


class BackendLifecycleOffline(unittest.TestCase):
    def test_full_constructor_and_idle_close_in_disposable_process(self):
        self.run_probe("idle")

    def test_export_worker_alive_after_join_rejects_clean_close(self):
        self.run_probe("export_alive")

    def run_probe(self, scenario):
        probe = Path(__file__).with_name("backend_lifecycle_probe.py")
        with tempfile.TemporaryDirectory(prefix="ear-full-lifecycle-") as directory:
            scratch = Path(directory).resolve()
            home = scratch / "home"
            temporary = scratch / "tmp"
            home.mkdir()
            temporary.mkdir()
            data = home / "Library/Application Support/KrabEar"
            env = {
                "PATH": os.defpath, "HOME": str(home), "TMPDIR": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(scratch / "pycache"),
                "PYTHONTZPATH": "",
                "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                "KRAB_EAR_DATA_DIR": str(data), "KRAB_EAR_MLX_WHISPER_WORKER": "0",
            }
            for key in (
                "LLM_ENABLED", "STT_GIGAAM_ENABLED", "REST_IN_PROCESS_ENABLED",
                "EVENT_BRIDGE_ENABLED", "AUTO_BACKUP_ENABLED", "RECAP_EMAIL_ENABLED",
                "DISK_MONITOR_ENABLED", "RECORDING_DURATION_WATCHDOG_ENABLED",
                "STT_AUTO_SEED_HOTWORDS", "SEMANTIC_SEARCH_ENABLED", "WAKE_WORD_ENABLED",
            ):
                env["KRAB_EAR_" + key] = "false"
            process = subprocess.Popen(
                [sys.executable, "-I", "-B", str(probe), str(scratch), scenario],
                cwd=scratch, env=env, start_new_session=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            output = ""
            timed_out = False
            try:
                output, _ = process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    output, _ = process.communicate(timeout=5)
                finally:
                    try:
                        os.killpg(process.pid, 0)
                        group_empty = False
                    except ProcessLookupError:
                        group_empty = True
                    print("LIFECYCLE_PARENT_RECEIPT=" + json.dumps({
                        "pid": process.pid, "exit": process.returncode,
                        "reaped": process.returncode is not None,
                        "group_empty": group_empty, "timeout": timed_out,
                    }), flush=True)
            self.assertFalse(timed_out, output)
            self.assertTrue(group_empty, output)
            self.assertEqual(process.returncode, 0, output)
            marker = next(line for line in output.splitlines() if line.startswith("LIFECYCLE_RECEIPT="))
            receipt = json.loads(marker.partition("=")[2])
            self.assertTrue(receipt["ok"], output)
            self.assertTrue(receipt["full_module_imported"], output)
            self.assertTrue(receipt["constructor_completed"], output)
            self.assertTrue(receipt["dispatch_built"], output)
            self.assertEqual(receipt["scenario"], scenario, output)
            self.assertEqual(receipt["violations"], [], output)
            self.assertEqual(receipt["loaded_providers"], [], output)
            self.assertEqual(receipt["started_workers"], 0, output)
            self.assertEqual(receipt["python_version"][:2], [3, 12], output)
            self.assertEqual(receipt["pycache_prefix"], str(scratch / "pycache"), output)
            self.assertEqual(receipt["zoneinfo_tzpath"], [], output)
            self.assertEqual(receipt["threads_after"], 1, output)
            self.assertEqual(receipt["tests"], 1, output)
            self.assertEqual(receipt["skipped"], 0, output)
            print(marker, flush=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
