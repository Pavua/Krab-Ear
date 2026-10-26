"""Batch shutdown: actual методы, stdlib threads; без BackendService ctor/ML.

AST сохраняет тела методов без переписывания. Изолированы только тяжёлые
imports и leaf-зависимости (engine, capture, filesystem metadata). Каждый
реальный тестовый thread освобождается и join-ится даже при RED assertion.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import logging
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_class(path, class_name, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    owner = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    methods = [n for n in owner.body if isinstance(n, ast.FunctionDef) and (
        n.name in names or n.name.startswith("_batch_")
    )]
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("_SHUTDOWN_") for t in node.targets
        ):
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    # Малый join budget только у изолированного экземпляра функций теста.
    namespace["_SHUTDOWN_BATCH_TIMEOUT_SEC"] = 0.02
    owner.body = methods
    owner.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), owner], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    result = namespace[class_name]
    result._lifecycle_init_lock = threading.Lock()
    return result


def _job_tracker():
    namespace = {"__name__": "isolated_job_tracker"}
    path = ROOT / "backend/job_tracker.py"
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    return namespace["JobTracker"]()


class BatchShutdownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.threads = []
        self.releases = []
        self.addCleanup(self._cleanup_workers)
        self.native_calls = []
        self.close_calls = []
        self.thread_type = threading.Thread
        self.info = lambda _path: types.SimpleNamespace(duration=0.01)

        def make_thread(*args, **kwargs):
            thread = self.thread_type(*args, **kwargs)
            self.threads.append(thread)
            return thread

        def import_leaf(name, *args, **kwargs):
            if name == "soundfile":
                return types.SimpleNamespace(info=lambda path: self.info(path))
            return builtins.__import__(name, *args, **kwargs)

        thread_api = types.SimpleNamespace(
            Thread=make_thread, RLock=threading.RLock, Lock=threading.Lock,
            Event=threading.Event, current_thread=threading.current_thread,
        )
        self.namespace = {
            "__builtins__": dict(vars(builtins), __import__=import_leaf),
            "threading": thread_api, "tempfile": tempfile, "time": time, "Path": Path,
            "logger": logging.getLogger("batch_shutdown_test"),
            "event_bus": types.SimpleNamespace(emit=lambda *_args: None),
            "bump_stt_activity": lambda: None,
            "stt_budget": types.SimpleNamespace(
                BATCH="batch", stt_budget_scope=lambda *_a, **_kw: contextlib.nullcontext(),
            ),
        }
        core_type = _load_class(
            ROOT / "backend/recording_core_service.py", "RecordingCoreService", {
                "handle_transcribe_paths_async", "_transcribe_paths_core",
                "close_background_workers", "_ensure_recording_lifecycle_state",
                "_safe_callback", "_is_path_allowed", "_collect_audio_paths",
            }, self.namespace,
        )
        self.core = core_type.__new__(core_type)
        self.core._job_tracker = _job_tracker()
        self.core._abort_recording_workers_locked = lambda: True
        self.core._settings_svc = types.SimpleNamespace(cached_settings=lambda: {})
        self.core.store = types.SimpleNamespace(data_dir=Path(self.tmp.name))
        self.core.vocabulary = types.SimpleNamespace(load=lambda: [])
        self.core._extract_transcribed_text = lambda result: result.get("text", "")
        self.core._extract_transcribed_error = lambda result: result.get("error", "")
        engine = types.SimpleNamespace(
            set_quality_profile=lambda _quality: None,
            transcribe=lambda *a, **kw: self.native_calls.append((a, kw)) or {"text": ""},
        )
        transcriber = types.SimpleNamespace(
            engine=engine, close=lambda: self.close_calls.append("close") or True,
        )
        self.core.transcriber = transcriber
        service_type = _load_class(
            ROOT / "backend/service.py", "BackendService", {"close"},
            {"logger": self.namespace["logger"]},
        )
        self.service = service_type.__new__(service_type)
        self.service.transcriber = transcriber
        self.service._recording_core_svc = self.core
        self.service._memory_conductor = types.SimpleNamespace(stop=lambda: None)
        self.service._meeting_svc = types.SimpleNamespace(
            begin_shutdown=lambda: None, close=lambda: True,
        )
        self.path = Path(self.tmp.name) / "sample.wav"
        self.path.write_bytes(b"fake audio: never decoded")

    def _cleanup_workers(self):
        for release in self.releases:
            release.set()
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(2.0)
                self.assertFalse(thread.is_alive(), "test left a worker alive")

    def _barrier(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def block():
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test barrier was not released")
            return []

        return entered, release, block

    def _start(self):
        return self.core.handle_transcribe_paths_async({"paths": [str(self.path)]})["job_id"]

    def _join(self):
        for thread in list(self.threads):
            if thread.ident is not None:
                thread.join(1.0)
                self.assertFalse(thread.is_alive())

    def _caller(self, function):
        result = []

        def run():
            try:
                result.append(function())
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return thread, result

    def test_delayed_vocabulary_is_owned_cancelled_and_joined_before_engine_close(self):
        entered, release, block = self._barrier()
        self.core.vocabulary.load = block
        job_id = self._start()
        self.assertTrue(entered.wait(1.0))
        started = time.monotonic()
        self.assertFalse(self.service.close())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(self.close_calls, [])
        self.assertTrue(any(t.is_alive() for t in self.core._batch_workers.values()))
        release.set()
        self._join()
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.core._job_tracker.get(job_id)["status"], "cancelled")
        self.assertTrue(self.service.close())
        self.assertEqual(self.close_calls, ["close"])
        self.assertEqual(self.core._batch_workers, {})

    def test_close_rejects_late_admission_without_a_job_or_thread(self):
        self.assertTrue(self.core.close_background_workers())
        with self.assertRaises(RuntimeError):
            self._start()
        self.assertEqual(self.core._job_tracker._jobs, {})
        self.assertEqual(self.threads, [])

    def test_shutdown_during_path_preparation_rejects_admission(self):
        entered, release, block = self._barrier()
        collect = self.core._collect_audio_paths

        def delayed(*args, **kwargs):
            block()
            return collect(*args, **kwargs)

        self.core._collect_audio_paths = delayed
        caller, result = self._caller(self._start)
        self.assertTrue(entered.wait(1.0))
        self.assertTrue(self.core.close_background_workers())
        release.set()
        caller.join(1.0)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], RuntimeError)
        self.assertEqual(self.core._job_tracker._jobs, {})

    def test_unfinished_start_is_owned_and_blocks_shutdown(self):
        entered, release, block = self._barrier()

        class DelayedStart(threading.Thread):
            def start(self):
                block()
                super().start()

        self.thread_type = DelayedStart
        caller, result = self._caller(self._start)
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(self.core.close_background_workers(lifecycle_lock_timeout_sec=0.01))
        release.set()
        caller.join(1.0)
        self._join()
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], str)
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.core.close_background_workers())

    def test_thread_start_failure_terminates_job_but_retains_unknown_handle(self):
        class FailedStart(threading.Thread):
            def start(self):
                raise RuntimeError("synthetic start failure")

        self.thread_type = FailedStart
        with self.assertRaises(RuntimeError):
            self._start()
        jobs = list(self.core._job_tracker._jobs.values())
        self.assertEqual([j["status"] for j in jobs], ["failed"])
        self.assertEqual(len(self.core._batch_workers), 1)
        self.assertFalse(self.service.close())
        self.assertEqual(self.close_calls, [])

    def test_thread_creation_failure_terminates_job_without_native_work(self):
        def failed_factory(*_args, **_kwargs):
            raise RuntimeError("synthetic thread allocation failure")

        self.thread_type = failed_factory
        with self.assertRaises(RuntimeError):
            self._start()
        jobs = list(self.core._job_tracker._jobs.values())
        self.assertEqual([j["status"] for j in jobs], ["failed"])
        self.assertEqual(self.native_calls, [])
        self.assertEqual(getattr(self.core, "_batch_workers", {}), {})
        self.assertTrue(self.service.close())

    def test_start_raises_after_launch_retains_live_worker(self):
        entered, release, block = self._barrier()
        self.core.vocabulary.load = block

        class StartedThenFailed(threading.Thread):
            def start(inner_self):
                super().start()
                self.assertTrue(entered.wait(1.0))
                raise RuntimeError("synthetic post-launch failure")

        self.thread_type = StartedThenFailed
        with self.assertRaises(RuntimeError):
            self._start()
        self.assertFalse(self.service.close())
        self.assertEqual(self.close_calls, [])
        release.set()
        self._join()
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.service.close())

    def test_interrupted_start_before_thread_ident_is_not_false_idle(self):
        self._assert_interrupted_start(after_ident=False, interruption=KeyboardInterrupt)

    def test_interrupted_start_after_ident_before_started_is_not_false_idle(self):
        self._assert_interrupted_start(after_ident=True, interruption=KeyboardInterrupt)

    def test_ordinary_exception_during_pending_bootstrap_retains_unknown_handle(self):
        self._assert_interrupted_start(after_ident=True, interruption=RuntimeError)

    def _assert_interrupted_start(self, *, after_ident, interruption):
        entered, release, block = self._barrier()
        exited = threading.Event()

        class PendingBootstrap(threading.Thread):
            def _set_ident(inner_self):
                super()._set_ident()
                if after_ident:
                    block()

            def _bootstrap_inner(inner_self):
                try:
                    if not after_ident:
                        block()
                    super()._bootstrap_inner()
                finally:
                    exited.set()

            def start(inner_self):
                # CPython start ждёт _started; сигнал может прервать это
                # ожидание до установки ident уже созданным native thread.
                def interrupted_wait(*_args, **_kwargs):
                    self.assertTrue(entered.wait(1.0))
                    raise interruption("synthetic interrupted start")

                inner_self._started.wait = interrupted_wait
                super().start()

        self.thread_type = PendingBootstrap
        try:
            with self.assertRaises(interruption):
                self._start()
            self.assertFalse(self.service.close())
            self.assertEqual(self.close_calls, [])
        finally:
            release.set()
            self.assertTrue(exited.wait(1.0))
            self._join()
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.service.close())

    def test_metadata_delay_does_not_enter_native_after_cancellation(self):
        entered, release, block = self._barrier()

        def info(_path):
            block()
            return types.SimpleNamespace(duration=0.01)

        self.info = info
        self._start()
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(self.core.close_background_workers())
        release.set()
        self._join()
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.core.close_background_workers())

    def test_entered_native_call_retains_owner_and_cancels_remaining_files(self):
        entered, release, block = self._barrier()

        def transcribe(*args, **kwargs):
            self.native_calls.append((args, kwargs))
            block()
            return {"text": ""}

        self.core.transcriber.engine.transcribe = transcribe
        second = Path(self.tmp.name) / "second.wav"
        second.write_bytes(b"fake: never decoded")
        job_id = self.core.handle_transcribe_paths_async({
            "paths": [str(self.path), str(second)],
        })["job_id"]
        self.assertTrue(entered.wait(1.0))
        self.assertFalse(self.service.close())
        self.assertEqual(self.close_calls, [])
        self.assertEqual(self.core._job_tracker.get(job_id)["status"], "running")
        release.set()
        self._join()
        self.assertEqual(len(self.native_calls), 1)
        self.assertEqual(self.core._job_tracker.get(job_id)["status"], "cancelled")
        self.assertTrue(self.service.close())

    def test_multiple_workers_share_join_budget_and_remain_owned_for_retry(self):
        for _ in range(3):
            entered, _release, block = self._barrier()
            self.core.vocabulary.load = block
            self._start()
            self.assertTrue(entered.wait(1.0))
        waits = []
        for thread in self.core._batch_workers.values():
            join = thread.join

            def tracked_join(timeout=None, real_join=join):
                waits.append(timeout)
                real_join(timeout)

            thread.join = tracked_join
        self.assertFalse(self.core.close_background_workers(batch_timeout_sec=0.1))
        # Настоящие join, но без хрупкого wall-clock порога на загруженном CI.
        self.assertGreaterEqual(len(waits), 3)
        self.assertLessEqual(sum(waits), 0.11)
        self.assertEqual(len(self.core._batch_workers), 3)
        for release in self.releases:
            release.set()
        self._join()
        self.assertTrue(self.core.close_background_workers())
        self.assertEqual(self.core._batch_workers, {})
        self.assertEqual(self.native_calls, [])

    def test_worker_exception_finishes_job_and_does_not_poison_drain(self):
        def fail():
            raise RuntimeError("synthetic vocabulary failure")

        self.core.vocabulary.load = fail
        job_id = self._start()
        self._join()
        self.assertEqual(self.core._job_tracker.get(job_id)["status"], "failed")
        self.assertTrue(self.service.close())
        self.assertEqual(self.close_calls, ["close"])

    def test_completed_batch_preserves_normal_flow_and_reaps_handles(self):
        job_id = self._start()
        self._join()
        self.assertEqual(self.core._job_tracker.get(job_id)["status"], "done")
        self.assertEqual(len(self.native_calls), 1)
        self.assertTrue(self.service.close())
        self.assertEqual(getattr(self.core, "_batch_workers", {}), {})

    def test_worker_reentrant_close_reports_live_self_without_join_error(self):
        results = []
        self.core.vocabulary.load = lambda: results.append(self.core.close_background_workers()) or []
        self._start()
        self._join()
        self.assertEqual(results, [False])
        self.assertEqual(self.native_calls, [])
        self.assertTrue(self.core.close_background_workers())

    def test_recording_drain_false_preserves_transcriber(self):
        self.core._abort_recording_workers_locked = lambda: False
        self.assertFalse(self.service.close())
        self.assertEqual(self.close_calls, [])

    def test_recording_drain_exception_preserves_transcriber(self):
        def fail():
            raise RuntimeError("synthetic capture close failure")

        self.core._abort_recording_workers_locked = fail
        self.assertFalse(self.service.close())
        self.assertEqual(self.close_calls, [])


if __name__ == "__main__":
    unittest.main()
