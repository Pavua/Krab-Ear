"""Parity harness изолирует lease без реальных backend, процессов и файлов владельца."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "scripts/pre_merge_py312_check.sh"


class Py312HarnessLeaseIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.bin_dir = self.scratch / "bin"
        self.venv_dir = self.scratch / "venv"
        self.bin_dir.mkdir()
        (self.venv_dir / "bin").mkdir(parents=True)
        (self.scratch / "tmp").mkdir()
        self.records = self.scratch / "pytest-records.jsonl"
        self.sentinel = self.scratch / "caller-owned-lease.json"
        self.sentinel.write_text("caller-owned-sentinel", encoding="utf-8")
        python_source = textwrap.dedent('''\
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            if args == ["--version"]:
                print("Python 3.12.fixture")
                raise SystemExit(0)
            if args[:1] == ["-c"]:
                raise SystemExit(1 if "import mlx" in args[1] else 0)
            if args[:2] == ["-m", "pytest"]:
                lease = os.environ.get("KRAB_EAR_BRAIN_LEASE_PATH")
                with open(os.environ["FAKE_PYTEST_RECORDS"], "a") as fh:
                    fh.write(json.dumps({"lease": lease, "args": args}) + "\\n")
                if lease:
                    Path(lease).write_text("synthetic-test-lease", encoding="utf-8")
                raise SystemExit(int(os.environ.get("FAKE_PYTEST_EXIT", "0")))
            raise SystemExit(42)
        ''')
        self.fake_python = self.venv_dir / "bin" / "python"
        self._executable(self.fake_python, python_source)
        self._executable(self.bin_dir / "ps", textwrap.dedent('''\
            import os
            from pathlib import Path
            counter = Path(os.environ["FAKE_PS_COUNTER"])
            count = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(count))
            if os.environ.get("FAKE_WORKER_LEAK") == "1" and count > 1:
                print("33333 /synthetic/gigaam_worker.py")
        '''))
        self.env = os.environ.copy()
        self.env.update({
            "HARNESS_VENV": str(self.venv_dir),
            "PY312": str(self.fake_python),
            "TMPDIR": str(self.scratch / "tmp"),
            "PATH": str(self.bin_dir) + os.pathsep + self.env.get("PATH", ""),
            "FAKE_PYTEST_RECORDS": str(self.records),
            "FAKE_PS_COUNTER": str(self.scratch / "ps-counter"),
            "KRAB_EAR_BRAIN_LEASE_PATH": str(self.sentinel),
        })
        for key in ("REBUILD", "FAKE_PYTEST_EXIT", "FAKE_WORKER_LEAK"):
            self.env.pop(key, None)

    def _executable(self, target: Path, program: str) -> None:
        target.write_text(f"#!{sys.executable}\n" + program, encoding="utf-8")
        target.chmod(0o700)

    def _run(self, files=None):
        result = subprocess.run(
            ["/bin/bash", str(HARNESS), *(files or ["KrabEar/tests/test_error_bus.py"])],
            cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20,
        )
        records = [json.loads(line) for line in self.records.read_text().splitlines()]
        return result, records

    def test_inherited_caller_lease_is_not_used_or_modified(self) -> None:
        result, records = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(records[0]["lease"], str(self.sentinel))
        self.assertEqual(self.sentinel.read_text(), "caller-owned-sentinel")

    def test_absent_override_gets_owned_temporary_lease(self) -> None:
        self.env.pop("KRAB_EAR_BRAIN_LEASE_PATH")
        result, records = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNotNone(records[0]["lease"])
        lease = Path(records[0]["lease"])
        self.assertTrue(lease.is_absolute())
        self.assertTrue(lease.resolve().is_relative_to((self.scratch / "tmp").resolve()))
        self.assertFalse(lease.exists(), "harness must clean its own lease after exit")

    def test_relative_tmpdir_still_gives_absolute_owned_lease(self) -> None:
        self.env["TMPDIR"] = os.path.relpath(self.scratch / "tmp", ROOT)
        result, records = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lease = Path(records[0]["lease"])
        self.assertTrue(lease.is_absolute())
        self.assertTrue(lease.resolve().is_relative_to((self.scratch / "tmp").resolve()))

    def test_each_named_file_gets_a_distinct_lease(self) -> None:
        result, records = self._run([
            "KrabEar/tests/test_error_bus.py", "KrabEar/tests/test_disk_monitor.py",
        ])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["lease"], records[1]["lease"])

    def test_failed_pytest_still_fails_harness(self) -> None:
        self.env["FAKE_PYTEST_EXIT"] = "7"
        result, _ = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL", result.stdout + result.stderr)

    def test_new_worker_still_fails_harness(self) -> None:
        self.env["FAKE_WORKER_LEAK"] = "1"
        result, _ = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("НОВЫЙ WORKER", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
