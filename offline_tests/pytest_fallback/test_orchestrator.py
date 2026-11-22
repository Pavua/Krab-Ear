"""Synthetic subprocess tests: no models, application profiles or network."""
import collections
import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fallback_under_test", ROOT / "scripts/run_pytest_fallback.py")
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fallback-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.case_number = 0

    def make_file(self, name):
        p = self.root / "inputs" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# synthetic\n")
        return str(p)

    def case(self, scenarios, log_dir=None, factory=None, **kw):
        files = [self.make_file(name) for name in scenarios]
        configs = dict(zip(files, scenarios.values()))
        output = io.StringIO()

        def build(file, seconds):
            attempt = 0 if seconds == 90 else 1
            self.calls.append((file, seconds))
            if attempt:
                self.assertNotIn("::error::", output.getvalue())
            cfg = configs[file]
            code = cfg[attempt]
            delay = cfg[2] if len(cfg) > 2 and not attempt else 0.01
            body = "import os,sys,time; print('synthetic pid',os.getpid(),flush=True); time.sleep(%r); sys.exit(%r)" % (delay, code)
            return [sys.executable, str(ROOT / "scripts/run_isolated_pytest.py"),
                    sys.executable, "-u", "-c", body]

        with contextlib.redirect_stdout(output):
            rc, run_dir = helper.run_fallback(
                files, log_dir or self.root / "logs", build_command=factory or build, budget=5, **kw
            )
        self.case_number += 1
        artifact = os.environ.get("FALLBACK_TEST_OUTPUT")
        if artifact:
            target = Path(artifact) / self._testMethodName / str(self.case_number)
            target.mkdir(parents=True, exist_ok=True)
            (target / "stdout.txt").write_text(output.getvalue())
            if run_dir is not None:
                shutil.copytree(run_dir, target / "run", dirs_exist_ok=True)
        return rc, Path(run_dir) if run_dir is not None else None, output.getvalue()

    def summary(self, root):
        return json.loads((root / "summary.json").read_text())

    def test_zero_is_green_without_retry(self):
        rc, root, output = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 0)
        self.assertEqual([x[1] for x in self.calls], [90])
        self.assertNotIn("::error::", output)
        self.assertEqual(self.summary(root)["retry"], [])

    def test_ordinary_fail_stays_red_without_retry(self):
        rc, root, output = self.case({"test_fail.py": (1, 0)})
        self.assertEqual(rc, 1)
        self.assertEqual([x[1] for x in self.calls], [90])
        self.assertIn("FAIL(1)", output)
        self.assertEqual(self.summary(root)["retry"], [])

    def test_124_to_zero_recovers_and_keeps_both_logs(self):
        rc, root, output = self.case({"test_slow.py": (124, 0)})
        self.assertEqual(rc, 0)
        self.assertEqual([x[1] for x in self.calls], [90, 180])
        self.assertNotIn("::error::", output)
        data = self.summary(root)
        self.assertEqual(data["parallel"][0]["rc"], 124)
        self.assertEqual(data["retry"][0]["rc"], 0)
        self.assertNotEqual(data["parallel"][0]["pid"], data["retry"][0]["pid"])
        for result in data["parallel"] + data["retry"]:
            self.assertIn("synthetic pid", (root / result["log"]).read_text())

    def test_124_to_124_remains_red(self):
        rc, root, output = self.case({"test_hung.py": (124, 124)})
        self.assertEqual(rc, 1)
        self.assertEqual(self.summary(root)["retry"][0]["rc"], 124)
        self.assertIn("FAIL(124)", output)

    def test_124_to_other_failure_remains_red(self):
        rc, root, output = self.case({"test_bad.py": (124, 3)})
        self.assertEqual(rc, 1)
        self.assertEqual(self.summary(root)["retry"][0]["rc"], 3)
        self.assertIn("FAIL(3)", output)
        self.assertNotIn("FAIL(124)", output)

    def test_multiple_timeouts_retry_after_barrier_and_sequentially(self):
        rc, root, _ = self.case({
            "test_a.py": (124, 0), "test_b.py": (0, 0, 0.15), "test_c.py": (124, 0)
        })
        self.assertEqual(rc, 0)
        data = self.summary(root)
        self.assertEqual(len(data["retry"]), 2)
        self.assertGreaterEqual(data["retry"][0]["started"], max(x["finished"] for x in data["parallel"]))
        self.assertGreaterEqual(data["retry"][1]["started"], data["retry"][0]["finished"])
        self.assertEqual([Path(x["file"]).name for x in data["retry"]], ["test_a.py", "test_c.py"])

    def test_ordinary_failure_not_lost_when_timeout_recovers(self):
        rc, root, output = self.case({"test_fail.py": (1, 0), "test_slow.py": (124, 0)})
        self.assertEqual(rc, 1)
        self.assertEqual(collections.Counter(x[1] for x in self.calls), {90: 2, 180: 1})
        self.assertIn("FAIL(1)", output)
        self.assertNotIn("FAIL(124)", output)
        self.assertEqual(len(self.summary(root)["retry"]), 1)

    def test_same_basename_has_distinct_logs(self):
        rc, root, _ = self.case({"a/test_same.py": (0, 0), "b/test_same.py": (0, 0)})
        self.assertEqual(rc, 0)
        data = self.summary(root)["parallel"]
        self.assertEqual(len({x["log"] for x in data}), 2)

    def test_unavailable_log_directory_is_orchestration_failure(self):
        blocked = self.root / "blocked"
        blocked.write_text("not a directory")
        rc, _, output = self.case({"test_ok.py": (0, 0)}, log_dir=blocked)
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("::error::", output)

    def test_launch_error_is_orchestration_failure(self):
        rc, root, output = self.case({"test_ok.py": (0, 0)}, factory=lambda *_: [str(self.root / "missing")])
        self.assertEqual(rc, 2)
        self.assertIn("::error::", output)
        self.assertTrue(self.summary(root)["errors"])

    def test_log_write_error_terminates_owned_process(self):
        with mock.patch.object(helper, "_write_all", side_effect=OSError("synthetic disk full")):
            rc, root, _ = self.case({"test_ok.py": (0, 0, 1)})
        self.assertEqual(rc, 2)
        for row in self.summary(root)["parallel"]:
            if row.get("pid"):
                self.assertFalse(Path("/proc", str(row["pid"])).exists())

    def test_missing_status_is_not_green(self):
        original = helper._atomic_json

        def omit(path, data):
            if path.name.endswith("-parallel.json"):
                return
            return original(path, data)

        with mock.patch.object(helper, "_atomic_json", side_effect=omit):
            rc, _, _ = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)

    def test_corrupt_status_is_not_green(self):
        original = helper._atomic_json

        def corrupt(path, data):
            original(path, data)
            if path.name.endswith("-parallel.json"):
                path.write_text("{")

        with mock.patch.object(helper, "_atomic_json", side_effect=corrupt):
            rc, _, _ = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)

    def test_status_write_failure_keeps_original_test_error(self):
        original = helper._atomic_json

        def fail(path, data):
            if path.name.endswith("-parallel.json"):
                raise OSError("synthetic status failure")
            return original(path, data)

        with mock.patch.object(helper, "_atomic_json", side_effect=fail):
            rc, root, output = self.case({"test_fail.py": (1, 0)})
        self.assertEqual(rc, 2)
        self.assertIn("FAIL(1)", output)
        self.assertTrue(list(root.glob("*-parallel.log")))

    def test_summary_write_failure_is_not_green(self):
        original = helper._atomic_json

        def fail(path, data):
            if path.name == "summary.json":
                raise OSError("synthetic summary failure")
            return original(path, data)

        with mock.patch.object(helper, "_atomic_json", side_effect=fail):
            rc, _, output = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)
        self.assertIn("::error::", output)

    def test_partial_queue_submission_is_not_green(self):
        original = helper.ThreadPoolExecutor.submit
        count = 0

        def partial(pool, *a, **kw):
            nonlocal count
            count += 1
            if count == 3:
                raise RuntimeError("synthetic partial submit")
            return original(pool, *a, **kw)

        with mock.patch.object(helper.ThreadPoolExecutor, "submit", new=partial):
            rc, root, _ = self.case({f"test_{i}.py": (0, 0, 0.1) for i in range(5)})
        self.assertEqual(rc, 2)
        self.assertEqual(len(json.loads((root / "manifest.json").read_text())["files"]), 5)
        self.assertLess(len(self.summary(root)["parallel"]), 5)

    def test_budget_expiry_is_orchestration_failure(self):
        file = self.make_file("test_sleep.py")

        def command(*_):
            return [sys.executable, str(ROOT / "scripts/run_isolated_pytest.py"),
                    sys.executable, "-c", "import time;time.sleep(10)"]

        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            rc, _ = helper.run_fallback([file], self.root / "logs", build_command=command, budget=0.1)
        self.assertEqual(rc, 2)
        self.assertLess(time.monotonic() - started, 5)

    def test_duplicate_input_is_rejected_before_launch(self):
        file = self.make_file("test_duplicate.py")
        calls = []
        with contextlib.redirect_stdout(io.StringIO()):
            rc, _ = helper.run_fallback([file, file], self.root / "logs",
                                        build_command=lambda *a: calls.append(a), budget=5)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_production_command_keeps_limits_and_isolation(self):
        for seconds in (90, 180):
            command = helper.pytest_command("synthetic test.py", seconds)
            self.assertEqual(command[:5], [sys.executable, str(ROOT / "scripts/run_isolated_pytest.py"),
                                           "timeout", str(seconds), sys.executable])
            self.assertIn("--timeout=75", command)
            self.assertIn("synthetic test.py", command)

    def test_concurrent_invocations_do_not_overwrite_statuses(self):
        file = self.make_file("test_same.py")

        def command(*_):
            return [sys.executable, "-c", "print('synthetic');raise SystemExit(0)"]

        def run():
            return helper.run_fallback([file], self.root / "logs", build_command=command, workers=1, budget=5)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual([x[0] for x in results], [0, 0])
        self.assertNotEqual(results[0][1], results[1][1])
        for _, root in results:
            self.assertEqual(len(self.summary(Path(root))["parallel"]), 1)

    def test_log_close_failure_is_not_green(self):
        original = Path.open

        class CloseFailure:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __getattr__(self, name):
                return getattr(self.stream, name)

            def __exit__(self, *args):
                self.stream.close()
                raise OSError("synthetic log close failure")

        def opened(path, mode="r", *args, **kw):
            stream = original(path, mode, *args, **kw)
            return CloseFailure(stream) if mode == "xb" else stream

        with mock.patch.object(Path, "open", new=opened):
            rc, _, _ = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)

    def test_nonfinite_budget_is_rejected(self):
        file = self.make_file("test_budget.py")
        for budget in (float("nan"), float("inf")):
            with self.subTest(budget=budget), contextlib.redirect_stdout(io.StringIO()):
                rc, _ = helper.run_fallback(
                    [file], self.root / "logs",
                    build_command=lambda *_: [sys.executable, "-c", "raise SystemExit(0)"],
                    budget=budget,
                )
                self.assertEqual(rc, 2)

    def test_log_integrity_mismatch_is_not_green(self):
        original = helper._atomic_json

        def corrupt_log(path, data):
            original(path, data)
            if path.name.endswith("-parallel.json"):
                (path.parent / data["log"]).write_bytes(b"truncated")

        with mock.patch.object(helper, "_atomic_json", side_effect=corrupt_log):
            rc, _, _ = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)

    def test_wrong_status_identity_is_not_green(self):
        original = helper._atomic_json

        def corrupt_identity(path, data):
            if path.name.endswith("-parallel.json"):
                data = dict(data, file="wrong task")
            original(path, data)

        with mock.patch.object(helper, "_atomic_json", side_effect=corrupt_identity):
            rc, _, _ = self.case({"test_ok.py": (0, 0)})
        self.assertEqual(rc, 2)

    def test_workflow_checks_helper_exit_with_set_plus_e(self):
        workflow = (ROOT / ".github/workflows/krabear-ci.yml").read_text()
        block = workflow.split("      - name: Run backend/unit tests", 1)[1]
        block = block.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        import textwrap
        shell = textwrap.dedent(block)
        for code in (0, 1, 2, 127):
            with self.subTest(helper_rc=code):
                case = self.root / f"workflow-{code}"
                (case / "KrabEar/tests").mkdir(parents=True)
                (case / "KrabEar/tests/test_space case.py").write_text("# synthetic\n")
                (case / "bin").mkdir()
                stub = case / "bin/python3"
                stub.write_text(
                    "#!" + sys.executable + "\n"
                    "import json,os,sys\n"
                    "args=sys.argv[1:]\n"
                    "if args[0]=='scripts/run_isolated_pytest.py':\n"
                    " sys.exit(1 if args[2]=='600' else 0)\n"
                    "if args[0]=='scripts/run_pytest_fallback.py':\n"
                    " open(os.environ['CALL_RECEIPT'],'w').write(json.dumps(args))\n"
                    f" sys.exit({code})\n"
                    "sys.exit(98)\n"
                )
                stub.chmod(0o700)
                receipt = case / "receipt.json"
                env = dict(PATH=str(case / "bin") + ":/usr/bin:/bin",
                           HOME=str(case / "home"), RUNNER_TEMP=str(case / "temp"),
                           CALL_RECEIPT=str(receipt))
                (case / "temp").mkdir()
                result = subprocess.run(["bash", "-c", shell], cwd=case, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0 if code == 0 else 1, result.stdout + result.stderr)
                self.assertTrue(receipt.exists(), result.stdout)
                self.assertEqual(json.loads(receipt.read_text())[-1],
                                 "KrabEar/tests/test_space case.py")


if __name__ == "__main__":
    unittest.main()
