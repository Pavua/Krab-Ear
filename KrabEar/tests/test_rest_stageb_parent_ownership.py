"""Cleanup не сигналит уже собранному child/группе с неизвестным владельцем."""
import contextlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch

import rest_stt_stageb_parent as parent


class ReapedChildCleanupTests(unittest.TestCase):
    def run_parent(self, group_exists):
        child_receipt = {"full_rest_import": True, "http_cases": 8, "worker_starts": 8,
                         "violations": [], "failures": [],
                         "scope": "real Flask upload/executor/error mapping; synthetic transcriber leaf; no ASR/runtime proof"}
        output = ("REST_STAGEB_RECEIPT=" + json.dumps(child_receipt) + "\n").encode()

        class Reaped:
            pid = 999999
            returncode = 0

            def communicate(self, timeout):
                return output, None

            def poll(self):
                return self.returncode

        sent = []

        def killpg(pid, sig):
            self.assertEqual(pid, Reaped.pid)
            if sig:
                sent.append(sig)
            elif not group_exists:
                raise ProcessLookupError

        with tempfile.TemporaryDirectory(prefix="ear-parent-ownership-") as directory:
            with patch.object(parent.subprocess, "Popen", return_value=Reaped()), \
                 patch.object(parent.os, "killpg", side_effect=killpg), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = parent.run(directory)
            receipt = json.loads((parent.Path(directory) / "STAGEB_RECEIPT.json").read_text())
        return result, receipt, sent

    def test_reaped_child_is_not_signalled(self):
        result, receipt, sent = self.run_parent(group_exists=False)
        self.assertEqual(sent, [], "PID может быть переиспользован после reap")
        self.assertEqual(result, 0)
        self.assertTrue(receipt["pgid_empty"])

    def test_unverified_group_is_reported_without_killing(self):
        result, receipt, sent = self.run_parent(group_exists=True)
        self.assertEqual(sent, [], "Неизвестную surviving/reused группу нельзя убивать")
        self.assertEqual(result, 1)
        self.assertFalse(receipt["pgid_empty"])
        self.assertIsNotNone(receipt["cleanup_error"])


if __name__ == "__main__":
    unittest.main()
