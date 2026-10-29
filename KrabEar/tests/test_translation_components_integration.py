"""Компонентная приёмка translator/settings/cache в изолированном дочернем процессе."""

import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class PersistedTranslationIntegration(unittest.TestCase):
    probe_name = "translation_components_probe.py"
    expected_tests = 9

    def test_real_components_in_disposable_process(self):
        probe = Path(__file__).with_name(self.probe_name)
        with tempfile.TemporaryDirectory(prefix="ear-translation-components-") as directory:
            scratch = Path(directory).resolve()
            (scratch / "home").mkdir()
            (scratch / "tmp").mkdir()
            env = {"PATH": os.defpath, "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
                   "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
                   "VECLIB_MAXIMUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
            process = subprocess.Popen([sys.executable, "-I", "-B", str(probe), str(scratch)],
                                       cwd=scratch, env=env, start_new_session=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output = ""
            timed_out = False
            try:
                output, _ = process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Собственная группа очищается также при KeyboardInterrupt и ошибке проверки.
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
                    print("COMPONENT_PARENT_RECEIPT=" + json.dumps(dict(
                        pid=process.pid, exit=process.returncode, reaped=process.returncode is not None,
                        group_empty=group_empty, timeout=timed_out)), flush=True)
            self.assertFalse(timed_out, "component child timed out: " + output)
            self.assertTrue(group_empty, output)
            self.assertEqual(process.returncode, 0, output)
            marker = next(line for line in output.splitlines() if line.startswith("COMPONENT_RECEIPT="))
            receipt = json.loads(marker.partition("=")[2])
            self.assertEqual(receipt["tests"], self.expected_tests, output)
            self.assertEqual(receipt["skipped"], 0, output)
            self.assertEqual(receipt["violations"], [], output)
            self.assertFalse(receipt["backend_service_imported"], output)
            self.assertEqual(receipt["threads"], 1, output)
            print(marker)


class ComponentChildLifecycle(unittest.TestCase):
    def exercise_exit(self, interrupted):
        original_popen = subprocess.Popen
        original_communicate = original_popen.communicate
        children = []
        first = [True]
        output = io.StringIO()
        reaped_before_fixture_cleanup = False

        def sleeper(_command, **kwargs):
            child = original_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], **kwargs)
            children.append(child)
            return child

        def interrupt_once(child, *, timeout=None):
            if first[0]:
                first[0] = False
                if interrupted:
                    raise KeyboardInterrupt("fixture interruption")
                return original_communicate(child, timeout=0.01)
            return original_communicate(child, timeout=timeout)

        try:
            with redirect_stdout(output), patch.object(original_popen, "communicate", new=interrupt_once), \
                    patch.object(subprocess, "Popen", new=sleeper):
                error = KeyboardInterrupt if interrupted else AssertionError
                with self.assertRaises(error):
                    PersistedTranslationIntegration().test_real_components_in_disposable_process()
                reaped_before_fixture_cleanup = children[0].returncode is not None
        finally:
            # Даже RED старого cleanup не оставляет тестовый sleeper владельцу.
            for child in children:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                original_communicate(child, timeout=5)
        self.assertTrue(reaped_before_fixture_cleanup, "wrapper left child unreaped before fixture cleanup")
        receipts = [line for line in output.getvalue().splitlines() if line.startswith("COMPONENT_PARENT_RECEIPT=")]
        self.assertEqual(len(receipts), 1, "wrapper must report cleanup before raising")
        receipt = json.loads(receipts[0].partition("=")[2])
        self.assertTrue(receipt["reaped"])
        self.assertTrue(receipt["group_empty"])
        self.assertEqual(receipt["timeout"], not interrupted)

    def test_timeout_reports_reaped_group(self):
        self.exercise_exit(interrupted=False)

    def test_interruption_reports_reaped_group(self):
        self.exercise_exit(interrupted=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
