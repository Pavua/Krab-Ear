"""Изолированные проверки наблюдения synthetic leaf."""

import concurrent.futures
import inspect
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from core import maintenance_claim_journal as journal
from core import maintenance_leaf_observation as observation
from core import maintenance_synthetic_submit as submit


class LeafObservationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="ear-leaf-")
        self.addCleanup(self.scratch.cleanup)
        self.store = journal.create_claim_journal(Path(self.scratch.name) / "journal.db", "inst", "epoch")
        self.addCleanup(self.store.close)
        self.store._connection.execute("UPDATE control SET admission='TEST_OPEN'")
        self.store._connection.commit()

    def claim_for(self, pool):
        handle = submit.SyntheticExecutor(pool)
        grant = self.store.reserve_leaf("p", "c", "reserve", "epoch", handle.identity, "stt")["grant_id"]
        claim = self.store.claim_leaf("p", "c", "claim", "epoch", handle.identity, grant)
        permit = claim["permit"]
        return permit, (permit.journal_id, grant, claim["claim_id"], handle.identity, "stt"), handle

    def test_observes_real_future_and_wrapper_completion(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            permit, scope, handle = self.claim_for(pool)
            started, release = threading.Event(), threading.Event()

            def leaf():
                started.set()
                if not release.wait(3):
                    raise TimeoutError("leaf did not get released")
                return 42

            watch = observation.LeafObservation()
            try:
                future = watch.start(permit, scope, handle, leaf)
                self.assertTrue(started.wait(3))
                self.assertEqual(watch.status()["state"], "PENDING")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=3), 42)
            result = watch.status()
            self.assertEqual(result["state"], "OBSERVED_FINISHED")
            self.assertFalse(result["drain_authority"])
            self.assertTrue(self.store.status()["unknown"])

    def test_cancellation_before_worker_start_does_not_enter_leaf(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def blocker():
                started.set()
                if not release.wait(3):
                    raise TimeoutError("blocker did not get released")

            occupied = pool.submit(blocker)
            try:
                self.assertTrue(started.wait(3))
                permit, scope, handle = self.claim_for(pool)
                watch = observation.LeafObservation()
                future = watch.start(permit, scope, handle, lambda: calls.append("entered"))
                self.assertTrue(future.cancel())
                self.assertEqual(watch.status()["state"], "OBSERVED_CANCELLED")
                self.assertTrue(self.store.status()["unknown"])
            finally:
                release.set()
            occupied.result(timeout=3)
        self.assertEqual(calls, [])

    def test_submit_ack_lost_after_real_start_is_unknown_and_cannot_retry(self):
        started, release = threading.Event(), threading.Event()
        calls, actual = [], []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            permit, scope, handle = self.claim_for(pool)
            real_submit_once = observation.submit_once

            def leaf():
                calls.append("started")
                started.set()
                if not release.wait(3):
                    raise TimeoutError("leaf did not get released")

            def start_then_raise(*args):
                actual.append(real_submit_once(*args))
                if not started.wait(3):
                    raise TimeoutError("leaf did not start")
                raise RuntimeError("submit ack lost")

            watch = observation.LeafObservation()
            try:
                with patch.object(observation, "submit_once", side_effect=start_then_raise):
                    with self.assertRaisesRegex(RuntimeError, "submit ack lost"):
                        watch.start(permit, scope, handle, leaf)
                self.assertEqual(watch.status()["state"], "UNKNOWN")
                with self.assertRaises(submit.SubmitRefused):
                    watch.start(permit, scope, handle, leaf)
            finally:
                release.set()
            actual[0].result(timeout=3)
            self.assertEqual(watch.status()["state"], "UNKNOWN")
            self.assertTrue(self.store.status()["unknown"])
        self.assertEqual(calls, ["started"])

    def test_fast_completion_and_failed_leaf_still_do_not_release_journal(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            permit, scope, handle = self.claim_for(pool)
            watch = observation.LeafObservation()

            def leaf():
                raise ValueError("leaf failed")

            future = watch.start(permit, scope, handle, leaf)
            with self.assertRaisesRegex(ValueError, "leaf failed"):
                future.result(timeout=3)
            self.assertEqual(watch.status()["state"], "OBSERVED_FINISHED")
            self.assertTrue(self.store.status()["unknown"])

    def test_substituted_submit_cannot_bind_unrelated_future(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def substituted(fn):
                calls.append("submitted")
                unrelated = concurrent.futures.Future()
                unrelated.set_result(None)
                return unrelated

            pool.submit = substituted
            permit, scope, handle = self.claim_for(pool)
            watch = observation.LeafObservation()
            with self.assertRaises(submit.SubmitRefused):
                watch.start(permit, scope, handle, lambda: None)
            self.assertEqual(watch.status()["state"], "UNKNOWN")
        self.assertEqual(calls, [])

    def test_object_spoofing_bound_method_cannot_bind_unrelated_future(self):
        calls = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            class Spoof:
                def __init__(self):
                    self.__self__ = pool
                    self.__func__ = concurrent.futures.ThreadPoolExecutor.submit

                def __call__(self, fn):
                    calls.append("submitted")
                    future = concurrent.futures.Future()
                    future.set_result(None)
                    return future

            pool.submit = Spoof()
            permit, scope, handle = self.claim_for(pool)
            watch = observation.LeafObservation()
            with self.assertRaises(submit.SubmitRefused):
                watch.start(permit, scope, handle, lambda: None)
            self.assertEqual(watch.status()["state"], "UNKNOWN")
        self.assertEqual(calls, [])

    def test_interrupt_before_submission_retains_unknown(self):
        watch = observation.LeafObservation()
        lines, first = inspect.getsourcelines(observation.LeafObservation.start)
        target = first + next(i for i, line in enumerate(lines) if line == "        try:\n")

        def interrupt(frame, event, arg):
            if frame.f_code is observation.LeafObservation.start.__code__ and event == "line":
                if frame.f_lineno == target:
                    raise KeyboardInterrupt
            return interrupt

        previous = sys.gettrace()
        try:
            sys.settrace(interrupt)
            with self.assertRaises(KeyboardInterrupt):
                watch.start(None, None, None, None)
        finally:
            sys.settrace(previous)
        self.assertEqual(watch.status()["state"], "UNKNOWN")
