"""Проверки ограниченного synthetic submit, без production workers."""

import concurrent.futures
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from core import maintenance_claim_journal as journal
from core import maintenance_synthetic_submit as submit


class SyntheticSubmitTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ear-submit-")
        self.addCleanup(self.scratch.cleanup)
        self.store = journal.create_claim_journal(Path(self.scratch.name) / "journal.db", "inst", "epoch")
        self.addCleanup(self.store.close)
        self.store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
        self.store._connection.commit()

    def claim_for(self, handle):
        grant = self.store.reserve_leaf("p", "client", "reserve", "epoch", handle.identity, "stt")["grant_id"]
        claim = self.store.claim_leaf("p", "client", "claim", "epoch", handle.identity, grant)
        return claim["permit"], (claim["permit"].journal_id, grant, claim["claim_id"], handle.identity, "stt")

    def test_fresh_permit_starts_one_real_leaf(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            handle = submit.SyntheticExecutor(executor)
            permit, scope = self.claim_for(handle)
            future = submit.submit_once(permit, scope, handle, lambda: 42)
            self.assertIsInstance(future, concurrent.futures.Future)
            self.assertEqual(future.result(timeout=3), 42)

    def test_wrong_scope_cannot_start_or_consume_right_scope(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            handle = submit.SyntheticExecutor(executor)
            permit, scope = self.claim_for(handle)
            with self.assertRaises(submit.SubmitRefused):
                submit.submit_once(permit, scope[:-1] + ("tts",), handle, lambda: calls.append("wrong"))
            future = submit.submit_once(permit, scope, handle, lambda: calls.append("right"))
            future.result(timeout=3)
            with self.assertRaises(submit.SubmitRefused):
                submit.submit_once(permit, scope, handle, lambda: calls.append("twice"))
        self.assertEqual(calls, ["right"])

    def test_submit_that_starts_then_raises_never_allows_retry(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def work():
            calls.append("started")
            started.set()
            if not release.wait(3):
                raise TimeoutError("test did not release worker")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            class StartsThenRaises:
                def submit(self, fn):
                    self.future = pool.submit(fn)
                    if not started.wait(3):
                        raise TimeoutError("worker did not start")
                    raise RuntimeError("submit acknowledgement lost")

            executor = StartsThenRaises()
            handle = submit.SyntheticExecutor(executor)
            permit, scope = self.claim_for(handle)
            try:
                with self.assertRaisesRegex(RuntimeError, "acknowledgement lost"):
                    submit.submit_once(permit, scope, handle, work)
                with self.assertRaises(submit.SubmitRefused):
                    submit.submit_once(permit, scope, handle, work)
            finally:
                release.set()
            executor.future.result(timeout=3)
        self.assertEqual(calls, ["started"])

    def test_broken_executor_cannot_enter_leaf_twice(self):
        calls = []

        class DuplicateExecutor:
            def submit(self, fn):
                fn()
                try:
                    fn()
                except submit.SubmitRefused:
                    pass

        handle = submit.SyntheticExecutor(DuplicateExecutor())
        permit, scope = self.claim_for(handle)
        submit.submit_once(permit, scope, handle, lambda: calls.append("entered"))
        self.assertEqual(calls, ["entered"])

    def test_executor_handle_must_match_grant_scope(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as target:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as other:
                intended = submit.SyntheticExecutor(target)
                impostor = submit.SyntheticExecutor(other)
                grant = self.store.reserve_leaf("p", "client", "reserve-2", "epoch",
                                                intended.identity, "stt")["grant_id"]
                claim = self.store.claim_leaf("p", "client", "claim-2", "epoch", intended.identity, grant)
                scope = (claim["permit"].journal_id, grant, claim["claim_id"], intended.identity, "stt")
                with self.assertRaises(submit.SubmitRefused):
                    submit.submit_once(claim["permit"], scope, impostor, lambda: calls.append("wrong"))
                future = submit.submit_once(claim["permit"], scope, intended, lambda: calls.append("right"))
                future.result(timeout=3)
        self.assertEqual(calls, ["right"])

    def test_handle_exposes_no_public_unguarded_submit(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            handle = submit.SyntheticExecutor(pool)
            with self.assertRaises(AttributeError):
                handle.submit(lambda: calls.append("unguarded"))
        self.assertEqual(calls, [])

    def test_fork_after_submit_does_not_execute_leaf_in_child(self):
        script = r'''
import os, signal
from core.maintenance_claim_journal import _SyntheticPermit
from core.maintenance_synthetic_submit import SyntheticExecutor, SubmitRefused, submit_once
read_fd, write_fd = os.pipe()
class ForkingExecutor:
    def submit(self, fn):
        pid = os.fork()
        if pid == 0:
            signal.alarm(3)
            os.close(read_fd)
            try:
                fn()
            except SubmitRefused:
                os.write(write_fd, b'R')
            finally:
                os._exit(0)
        os.close(write_fd)
        reaped = False
        try:
            result = os.read(read_fd, 2)
            _, status = os.waitpid(pid, 0)
            reaped = True
            if result != b'R' or status != 0:
                raise RuntimeError('child entered synthetic leaf')
        finally:
            if not reaped:
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
            os.close(read_fd)
handle = SyntheticExecutor(ForkingExecutor())
permit = _SyntheticPermit('journal', 'grant', 'claim', handle.identity, 'stt')
scope = ('journal', 'grant', 'claim', handle.identity, 'stt')
submit_once(permit, scope, handle, lambda: os.write(write_fd, b'X'))
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
