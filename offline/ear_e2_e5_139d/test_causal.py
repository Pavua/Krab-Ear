"""Frozen actual-handler source profile; no Flask/server/model bootstrap.

The only timing accelerators are Future.result(wait=0) after worker start for
E2, and logical clock advancement in E5. Production clamps/locks/metadata,
complete handler body, budget scope and real executor workers are retained.
"""
import ast
import concurrent.futures
import io
import json
import logging
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]


def load_local(name, relative):
    module = ModuleType(name)
    module.__file__ = str(ROOT / relative)
    sys.modules[name] = module
    exec(compile((ROOT / relative).read_text(), module.__file__, "exec"), module.__dict__)
    return module


budget = load_local("offline_budget", "KrabEar/core/stt_budget.py")
activity = load_local("offline_activity", "KrabEar/backend/activity_diagnostics.py")
ownership = load_local("offline_ownership", "KrabEar/core/operation_ownership.py")
SOURCE = ROOT / "KrabEar/backend/rest_server.py"
TREE = ast.parse(SOURCE.read_text())
SELECT = {"transcribe_audio", "_resolve_transcribe_deadline_sec",
          "try_acquire_stt_singleflight", "release_stt_singleflight",
          "_arm_timeout_exit", "_validate_audio_magic_bytes"}


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200
        self.callbacks = []

    def call_on_close(self, callback):
        self.callbacks.append(callback)

    def close(self):
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


class Upload:
    filename = "synthetic.wav"

    def save(self, path):
        Path(path).write_bytes(b"RIFF" + b"\0" * 4 + b"WAVE" + b"\0" * 4)


class Clock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now


class FutureView:
    """Delegates every state/callback to the actual concurrent Future."""
    def __init__(self, future, harness):
        self.future = future
        self.harness = harness

    def __getattr__(self, name):
        return getattr(self.future, name)

    def result(self, timeout=None):
        self.harness.wait_budgets.append(timeout)
        hook = getattr(self.harness, "before_result", None)
        if hook is not None:
            hook(self.future)
        if self.harness.force_running_timeout and len(self.harness.futures) == 1:
            if not self.harness.started.wait(1):
                raise AssertionError("worker did not start")
            return self.future.result(timeout=0)
        return self.future.result(timeout=timeout)


class Harness:
    def __init__(self, scratch, force_running_timeout=False, logical_clock=False):
        self.force_running_timeout = force_running_timeout
        self.started = threading.Event()
        self.release_worker = threading.Event()
        self.pools = []
        self.futures = []
        self.wait_budgets = []
        self.exit_codes = []
        self.calls = 0
        self.worker_alive_on_second_entry = False
        self.clock = Clock() if logical_clock else time
        self.queue_budgets = []
        self.worker_budget_remainders = []
        self.normalized = 0
        store = SimpleNamespace(load_vocabulary=lambda: [], load_settings=lambda **kw: {})
        engine = SimpleNamespace(normalize_audio=self.normalize)
        deps = SimpleNamespace(store=store, engine=engine,
                               transcriber=SimpleNamespace(transcribe=self.transcribe),
                               metrics=SimpleNamespace(record=lambda *a, **k: None))
        self.ns = dict(__name__="offline_handler", _Any=object,
                       Response=Response, jsonify=Response,
                       time=self.clock, math=math, threading=threading, os=os, uuid=uuid,
                       concurrent=SimpleNamespace(futures=SimpleNamespace(
                           ThreadPoolExecutor=self.pool, TimeoutError=concurrent.futures.TimeoutError)),
                       logger=logging.getLogger("offline"), stt_budget=budget,
                       _deps=lambda: deps, _privacy_mode_enabled=lambda: False,
                       _call_profile_privacy_enabled=lambda: False,
                       _TRANSCRIBE_TIMEOUT_SEC=600, _DEADLINE_SEC_MIN=budget.MIN_USEFUL_ATTEMPT_SEC * 2,
                       _DEADLINE_SEC_MAX=120.0, _MAX_AUDIO_DURATION_SEC=3600,
                       _STT_SINGLEFLIGHT=threading.Lock(), _TRANSCRIBE_TIMEOUT_EXIT_CODE=70,
                       _STT_POISONED=threading.Event(),
                       _REST_ACTIVITY=activity.ActivityReservations(),
                       _REST_OPERATIONS=ownership.OwnedOperations(),
                       TEMP_DIR=Path(scratch), ALLOWED_EXTENSIONS={".wav"},
                       VALID_QUALITY={"balanced"}, VALID_CLEANUP={"soft"}, VALID_DOMAIN={"casual"},
                       secure_filename=lambda name: name,
                       request=SimpleNamespace(form={"deadline_sec": "10", "persist_history": "false"},
                                               files={"file": Upload()}), g=SimpleNamespace(),
                       _exit_poisoned_rest_process=self.exit_codes.append)
        nodes = []
        for node in TREE.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in SELECT:
                # Only framework registration/auth decorators are removed. Body unchanged.
                node = ast.parse(ast.unparse(node)).body[0]
                node.decorator_list = []
                nodes.append(node)
        assert {n.name for n in nodes} == SELECT
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + nodes,
                            type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(SOURCE), "exec"), self.ns)

    def normalize(self, path):
        self.normalized += 1

    def transcribe(self, path, **kwargs):
        self.calls += 1
        if self.force_running_timeout and self.calls == 1:
            self.started.set()
            if not self.release_worker.wait(2):
                raise AssertionError("test failed to release worker")
        elif self.force_running_timeout:
            self.worker_alive_on_second_entry = not self.futures[0].done()
        if isinstance(self.clock, Clock):
            # The real ContextVar scope opens only in this actual executor thread.
            current = budget._current.get()
            assert current is not None
            self.worker_budget_remainders.append(current.deadline_monotonic - time.monotonic())
            self.clock.now += 5.0
        return {"text": "synthetic", "engine": "stub"}

    def pool(self, max_workers):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                    thread_name_prefix="ear-causal")
        self.pools.append(pool)
        harness = self

        class PoolView:
            def submit(self, *args, **kwargs):
                future = pool.submit(*args, **kwargs)
                harness.futures.append(future)
                return FutureView(future, harness)

            def shutdown(self, *args, **kwargs):
                return pool.shutdown(*args, **kwargs)

        return PoolView()

    def invoke(self):
        response = self.ns["transcribe_audio"]()
        if isinstance(response, tuple):
            response, status = response
            response.status_code = status
        return response

    def cleanup(self):
        self.release_worker.set()
        for event in getattr(self, "extra_release_events", []):
            event.set()
        for pool in self.pools:
            pool.shutdown(wait=True, cancel_futures=True)
        assert all(f.done() for f in self.futures)


class CausalProfile(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.harnesses = []

    def tearDown(self):
        for harness in self.harnesses:
            harness.cleanup()
        self.scratch.cleanup()

    def harness(self, **kwargs):
        value = Harness(self.scratch.name, **kwargs)
        self.harnesses.append(value)
        return value

    def test_e2_timeout_must_fence_second_admission_until_worker_drains(self):
        h = self.harness(force_running_timeout=True)
        first = h.invoke()
        self.assertEqual(first.status_code, 504)
        self.assertFalse(h.futures[0].done())
        self.assertEqual(h.exit_codes, [])  # Response deliberately not closed yet.
        self.assertEqual(len(first.callbacks), 1)
        # Probe actual lock state without spending another physical 10s. The
        # ordinary handler still supplies its real clamped admission budget.
        original_acquire = h.ns["try_acquire_stt_singleflight"]
        admission_budgets = []

        def probe_acquire(timeout):
            admission_budgets.append(timeout)
            return original_acquire(0.0)

        h.ns["try_acquire_stt_singleflight"] = probe_acquire
        second = h.invoke()
        print(json.dumps({"witness": "E2", "first_status": first.status_code,
                          "second_status": second.status_code,
                          "first_worker_still_running": not h.futures[0].done(),
                          "second_worker_entered_while_first_running": h.worker_alive_on_second_entry,
                          "exit_callback_pending": len(first.callbacks) == 1,
                          "exit_invocations": len(h.exit_codes)}), flush=True)
        self.assertEqual(second.status_code, 503,
                         "second handler admitted while first timed-out worker remains running")
        self.assertEqual(admission_budgets, [10.0])
        self.assertFalse(h.worker_alive_on_second_entry)
        self.assertFalse(h.futures[0].done())
        h.cleanup()
        self.assertFalse(h.ns["_STT_SINGLEFLIGHT"].locked())
        # A second long STT must not enter after first Future drains while its
        # old response still carries a live process-exit callback.
        entered_after_drain = threading.Event()
        release_after_drain = threading.Event()
        h.extra_release_events = [release_after_drain]
        late_exit_with_second_running = []

        def forbidden_long_stt(*args, **kwargs):
            entered_after_drain.set()
            if not release_after_drain.wait(2):
                raise AssertionError("test failed to release second held worker")
            return {"text": "synthetic"}

        def late_close_if_wrongly_admitted(future):
            self.assertTrue(entered_after_drain.wait(1))
            first.close()
            late_exit_with_second_running.append(future.running() and h.exit_codes == [70])
            release_after_drain.set()

        h.before_result = late_close_if_wrongly_admitted
        h.ns["_deps"]().transcriber.transcribe = forbidden_long_stt
        recovered = h.invoke()
        print(json.dumps({"witness": "E2_LATE_CLOSE", "after_drain_status": recovered.status_code,
                          "second_worker_entered": entered_after_drain.is_set(),
                          "exit_while_second_running": any(late_exit_with_second_running)}), flush=True)
        self.assertEqual(recovered.status_code, 503)
        self.assertFalse(entered_after_drain.is_set())
        self.assertEqual(len(h.futures), 1)
        self.assertEqual(h.exit_codes, [])
        first.close()  # Actual arm_timeout_exit callback is retained unchanged.
        self.assertEqual(h.exit_codes, [70])
        self.assertFalse(entered_after_drain.is_set())
        self.assertTrue(h.ns["_STT_POISONED"].is_set())
        self.assertFalse(h.ns["try_acquire_stt_singleflight"](0.0))

    def test_e5_characterize_ordinary_two_phase_budget_without_new_contract(self):
        h = self.harness(logical_clock=True)
        real_acquire = h.ns["try_acquire_stt_singleflight"]

        def queued_acquire(timeout):
            h.queue_budgets.append(timeout)
            h.clock.now += 6.0  # Deterministic admission wait seam; lock itself stays real.
            return real_acquire(timeout)

        h.ns["try_acquire_stt_singleflight"] = queued_acquire
        started = h.clock.monotonic()
        response = h.invoke()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(h.queue_budgets, [10.0])
        self.assertEqual(h.wait_budgets, [10.0])
        self.assertEqual(h.clock.monotonic() - started, 11.0)
        self.assertGreater(h.worker_budget_remainders[0], 9.0)
        # Characterization only. Docs name Future.result budget; this is not
        # sufficient evidence that ordinary whole-request <=10 is promised.

    def test_actual_minimum_clamp_and_invalid_deadline_controls(self):
        h = self.harness()
        resolve = h.ns["_resolve_transcribe_deadline_sec"]
        self.assertEqual(budget.MIN_USEFUL_ATTEMPT_SEC, 5.0)
        self.assertEqual(resolve("0.05"), (10.0, None))
        self.assertEqual(resolve("10"), (10.0, None))
        self.assertEqual(resolve(None), (None, None))
        for raw in ["nan", "inf", "0", "-1", "121"]:
            self.assertIsNotNone(resolve(raw)[1])

    def test_success_and_completed_error_release_slot_without_exit(self):
        h = self.harness()
        self.assertEqual(h.invoke().status_code, 200)
        self.assertTrue(h.ns["try_acquire_stt_singleflight"](0.0))
        h.ns["release_stt_singleflight"]()

        def fail(*args, **kwargs):
            raise ValueError("synthetic completed error")

        h.ns["_deps"]().transcriber.transcribe = fail
        self.assertEqual(h.invoke().status_code, 500)
        self.assertTrue(h.ns["try_acquire_stt_singleflight"](0.0))
        h.ns["release_stt_singleflight"]()
        self.assertTrue(all(f.done() for f in h.futures))
        self.assertEqual(h.exit_codes, [])

    def test_cancelled_pending_future_releases_slot_without_exit(self):
        h = self.harness()
        pending = concurrent.futures.Future()

        class PendingPool:
            def submit(self, *args, **kwargs):
                h.futures.append(pending)
                return self

            def __getattr__(self, name):
                return getattr(pending, name)

            def result(self, timeout=None):
                h.wait_budgets.append(timeout)
                return pending.result(timeout=0.0)

            def shutdown(self, wait=True, **kwargs):
                pending.cancel()

        h.ns["concurrent"].futures.ThreadPoolExecutor = lambda **kw: PendingPool()
        response = h.invoke()
        self.assertEqual(response.status_code, 504)
        self.assertTrue(pending.cancelled())
        self.assertEqual(h.calls, 0)
        self.assertTrue(h.ns["try_acquire_stt_singleflight"](0.0))
        h.ns["release_stt_singleflight"]()
        self.assertEqual(response.callbacks, [])
        self.assertEqual(h.exit_codes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
