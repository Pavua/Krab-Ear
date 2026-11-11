"""P1 #2198 (2026-11-10): сериализация capture release/acquire и атомарный stop.

Тесты фиксируют контракт, а не текущую форму реализации:
1. release generation A не может остановить recorder, уже принадлежащий B;
2. admission stop и переход состояния выполняются в одном критическом участке;
3. повторный stop во время идущего stop не дублирует summary/history/Gateway;
4. старт replacement во время summary старого stop не затирается финальной
   записью старого stop;
5. порядок локов capture -> state, без self._lock во время физического I/O,
   и пути ошибок не оставляют _capture_lock занятым.
"""
from __future__ import annotations

import ast
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService, VoiceGatewayClient  # noqa: E402
from backend.state_store import StateStore  # noqa: E402

SERVICE_PATH = PROJECT_ROOT / "backend" / "call_assist_service.py"


class _Recorder:
    def __init__(self):
        self.is_recording = False
        self.start_calls = 0
        self.stop_calls = 0
        self.worker_alive = False

    def start(self) -> bool:
        self.start_calls += 1
        self.is_recording = True
        return True

    def stop(self, timeout_sec=None) -> None:
        self.stop_calls += 1
        self.is_recording = False
        self.worker_alive = False

    def snapshot_audio(self, max_duration_sec: float = 25.0):
        class _Buf:
            size = 0

        return _Buf(), 0.0


class _Transcriber:
    def transcribe_preview(self, audio_data, quality_profile="balanced"):
        return {"text": ""}


class _Gateway(VoiceGatewayClient):
    def __init__(self):
        self.summary_calls = 0
        self.stop_calls = 0
        self.summary_gate: threading.Event | None = None
        self.summary_entered = threading.Event()

    def start_session(self, voice_gateway_url, api_key, payload):
        return {"ok": True, "session_id": "gw_test"}

    def stop_session(self, voice_gateway_url, api_key, session_id):
        self.stop_calls += 1
        return {"ok": True}

    def post(self, voice_gateway_url, api_key, path, payload):
        if "/summary" in path:
            self.summary_calls += 1
            self.summary_entered.set()
            if self.summary_gate is not None:
                self.summary_gate.wait(5.0)
            return {"ok": True, "payload": {"summary": "итог звонка", "tasks": []}}
        return {"ok": True, "payload": {}}

    def get(self, *a, **k):
        return {"ok": True, "payload": {}}


class _GatedCore:
    """Первый физический stop зависает до release (модель pre-I/O окна)."""

    def __init__(self, recorder: _Recorder):
        self.recorder = recorder
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def stop_unmanaged_capture(self, timeout_sec=None):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            if not self.release.wait(5.0):
                raise AssertionError("test did not release the gated core")
        self.recorder.stop(timeout_sec=timeout_sec)
        return "stopped"


class _TrackedLock:
    """Обёртка над self._lock: знает владельца и может придержать поток.

    pause_thread/pause_on_nth: перед N-м входом из _handle_stop_impl указанный
    поток ставит paused и ждёт resume. На BASE второй такой вход — второй
    admission-блок (окно исходной P1-2); на кандидате — финальная запись stop.
    """

    def __init__(self):
        self._lk = threading.Lock()
        self.owner: int | None = None
        self.pause_thread: int | None = None
        self.pause_on_nth = 0
        self._nth = 0
        self.paused = threading.Event()
        self.resume = threading.Event()

    def __enter__(self):
        if (
            self.pause_thread == threading.get_ident()
            and sys._getframe(1).f_code.co_name == "_handle_stop_impl"
        ):
            self._nth += 1
            if self._nth == self.pause_on_nth:
                self.paused.set()
                if not self.resume.wait(5.0):
                    raise AssertionError("test did not resume the paused stop")
        self._lk.acquire()
        self.owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.owner = None
        self._lk.release()
        return False

    def held_by_me(self) -> bool:
        return self.owner == threading.get_ident()


class _TrackedCaptureLock:
    """Обёртка над _capture_lock (RLock): фиксирует ожидание и порядок локов."""

    def __init__(self, state_lock: _TrackedLock):
        self._lk = threading.RLock()
        self._state_lock = state_lock
        self.contended = threading.Event()
        self.order_violations: list[str] = []

    def __enter__(self):
        if self._state_lock.held_by_me():
            self.order_violations.append("capture acquired while state lock held")
        if not self._lk.acquire(blocking=False):
            self.contended.set()
            self._lk.acquire()
        return self

    def __exit__(self, *exc):
        self._lk.release()
        return False


class CallAssistP1RaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.store = StateStore(Path(self.tmp.name) / "data")
        self.store.initialize_startup_plaintext_policy(new_profile=True)

    def _service(self, gateway=None, core=None):
        recorder = _Recorder()
        svc = CallAssistService(
            store=self.store,
            recorder=recorder,
            transcriber=_Transcriber(),
            gateway=gateway or _Gateway(),
            recording_core=core,
        )
        return svc, recorder

    def test_release_of_generation_a_does_not_stop_generation_b(self):
        """P1-1 (#2198 thread @243): запоздавший release A не гасит capture B.

        Барьер без опроса: release A стоит внутри физического stop. Gate
        отпускается, когда start B либо завершился (BASE: B сам снимает долг A
        и запускает рекордер), либо упёрся в занятый _capture_lock (кандидат).
        """
        gateway = _Gateway()
        svc, recorder = self._service(gateway=gateway)
        core = _GatedCore(recorder)
        svc._recording_core = core
        state_lock = _TrackedLock()
        svc._lock = state_lock
        capture_lock = _TrackedCaptureLock(state_lock)
        svc._capture_lock = capture_lock  # на BASE атрибут не используется

        self.assertEqual(svc.handle_start({}).get("status"), "running")
        gen_a = svc._capture_gen

        stop_result: dict = {}

        def stop_a():
            stop_result["value"] = svc.handle_stop({})

        stop_thread = threading.Thread(target=stop_a, daemon=True)
        stop_thread.start()
        self.assertTrue(core.entered.wait(5.0), "stop A did not reach physical release")

        start_result: dict = {}
        b_progress = threading.Event()

        def start_b():
            try:
                start_result["value"] = svc.handle_start({})
            finally:
                b_progress.set()

        capture_watch = threading.Thread(
            target=lambda: capture_lock.contended.wait(5.0) and b_progress.set(),
            daemon=True,
        )
        capture_watch.start()
        start_thread = threading.Thread(target=start_b, daemon=True)
        start_thread.start()
        try:
            self.assertTrue(b_progress.wait(5.0), "start B neither finished nor waited for capture")
        finally:
            core.release.set()
            stop_thread.join(5.0)
            start_thread.join(5.0)
        self.assertFalse(stop_thread.is_alive())
        self.assertFalse(start_thread.is_alive())

        self.assertEqual(start_result["value"].get("status"), "running")
        state = svc.state
        self.assertTrue(state.get("active"))
        self.assertEqual(state.get("status"), "running")
        self.assertTrue(recorder.is_recording, "replacement recorder was stopped by old release")
        self.assertIsNotNone(svc._capture_gen)
        self.assertIsNot(svc._capture_gen, gen_a)
        self.assertEqual(capture_lock.order_violations, [])

        svc.handle_stop({})

    def test_delayed_stop_after_admission_does_not_stop_replacement(self):
        """P1-2 (#2198 thread @664): admission и переход stop атомарны.

        Stop X придерживается перед вторым входом в self._lock из
        _handle_stop_impl. Пока он стоит, stop Y и start B идут в главном
        потоке. На BASE X после паузы читает generation B и гасит её.
        """
        gateway = _Gateway()
        svc, recorder = self._service(gateway=gateway)
        state_lock = _TrackedLock()
        svc._lock = state_lock
        svc._capture_lock = _TrackedCaptureLock(state_lock)
        self.assertEqual(svc.handle_start({}).get("status"), "running")

        x_result: dict = {}
        x_ready = threading.Event()

        def stop_x():
            state_lock.pause_thread = threading.get_ident()
            state_lock.pause_on_nth = 2
            x_ready.set()
            x_result["value"] = svc.handle_stop({"auto_summary": False})

        x_thread = threading.Thread(target=stop_x, daemon=True)
        x_thread.start()
        try:
            self.assertTrue(x_ready.wait(5.0))
            self.assertTrue(state_lock.paused.wait(5.0), "stop X never reached its second lock entry")
            svc.handle_stop({"auto_summary": False})  # stop Y
            start_b = svc.handle_start({})
            self.assertEqual(start_b.get("status"), "running", start_b)
            gen_b = svc._gen
        finally:
            state_lock.resume.set()
            x_thread.join(5.0)
        self.assertFalse(x_thread.is_alive())

        state = svc.state
        self.assertTrue(state.get("active"), state)
        self.assertEqual(state.get("status"), "running", state)
        self.assertEqual(state.get("session_id"), start_b.get("session_id"))
        self.assertIs(svc._gen, gen_b)
        self.assertTrue(recorder.is_recording, "delayed stop X stopped replacement recorder")

        svc.handle_stop({})

    def test_lock_order_and_no_state_lock_during_physical_io(self):
        """capture -> state; self._lock не держится во время Core/recorder I/O."""
        svc, recorder = self._service()
        state_lock = _TrackedLock()
        svc._lock = state_lock
        capture_lock = _TrackedCaptureLock(state_lock)
        svc._capture_lock = capture_lock
        io_under_state_lock: list[str] = []

        orig_start, orig_stop = recorder.start, recorder.stop

        def start_checked():
            if state_lock.held_by_me():
                io_under_state_lock.append("recorder.start")
            return orig_start()

        def stop_checked(timeout_sec=None):
            if state_lock.held_by_me():
                io_under_state_lock.append("recorder.stop")
            return orig_stop(timeout_sec=timeout_sec)

        recorder.start, recorder.stop = start_checked, stop_checked

        class _Core:
            def stop_unmanaged_capture(self, timeout_sec=None):
                if state_lock.held_by_me():
                    io_under_state_lock.append("core.stop_unmanaged_capture")
                recorder.stop(timeout_sec=timeout_sec)
                return "stopped"

        svc._recording_core = _Core()
        for _ in range(3):
            self.assertEqual(svc.handle_start({}).get("status"), "running")
            svc.handle_stop({"auto_summary": False})

        self.assertEqual(capture_lock.order_violations, [])
        self.assertEqual(io_under_state_lock, [])

    def test_error_paths_release_capture_lock(self):
        """Сбой recorder.start и сбой физического stop не оставляют _capture_lock занятым."""
        svc, recorder = self._service()
        state_lock = _TrackedLock()
        svc._lock = state_lock
        capture_lock = _TrackedCaptureLock(state_lock)
        svc._capture_lock = capture_lock

        orig_start = recorder.start
        recorder.start = lambda: (_ for _ in ()).throw(RuntimeError("synthetic start failure"))
        failed = svc.handle_start({})
        self.assertEqual(failed.get("error"), "start_failed", failed)
        self.assertEqual(svc.state.get("status"), "idle")
        recorder.start = orig_start

        class _FlakyCore:
            def __init__(self):
                self.calls = 0

            def stop_unmanaged_capture(self, timeout_sec=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("synthetic stop failure")
                recorder.stop(timeout_sec=timeout_sec)
                return "stopped"

        core = _FlakyCore()
        svc._recording_core = core
        self.assertEqual(svc.handle_start({}).get("status"), "running")
        svc.handle_stop({"auto_summary": False})
        self.assertIsNotNone(svc._capture_gen, "cleanup debt must survive a failed physical stop")

        done: dict = {}

        def start_again():
            done["value"] = svc.handle_start({})

        t = threading.Thread(target=start_again, daemon=True)
        t.start()
        t.join(5.0)
        self.assertFalse(t.is_alive(), "capture lock leaked after failed stop")
        self.assertEqual(done["value"].get("status"), "running", done["value"])
        self.assertEqual(core.calls, 2, "next start must retry the old cleanup debt")
        self.assertTrue(recorder.is_recording)
        self.assertEqual(capture_lock.order_violations, [])

        svc.handle_stop({"auto_summary": False})

    def test_repeated_stop_during_inflight_does_not_duplicate_side_effects(self):
        gateway = _Gateway()
        gateway.summary_gate = threading.Event()
        svc, recorder = self._service(gateway=gateway)
        self.assertEqual(svc.handle_start({}).get("status"), "running")

        first: dict = {}

        def stop_first():
            first["value"] = svc.handle_stop({"auto_summary": True})

        thread = threading.Thread(target=stop_first, daemon=True)
        thread.start()
        self.assertTrue(gateway.summary_entered.wait(5.0))

        second = svc.handle_stop({"auto_summary": True})
        self.assertTrue(second.get("stop_in_progress"), second)

        gateway.summary_gate.set()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

        self.assertEqual(gateway.summary_calls, 1)
        self.assertEqual(gateway.stop_calls, 1)
        self.assertEqual(self.store.count_active_items(), 1)
        self.assertTrue(first["value"].get("summary_history_id"))

        replay = svc.handle_stop({})
        self.assertTrue(replay.get("replayed"), replay)
        self.assertEqual(replay.get("summary_history_id"), first["value"]["summary_history_id"])
        self.assertEqual(gateway.summary_calls, 1)
        self.assertEqual(self.store.count_active_items(), 1)

    def test_replacement_started_during_summary_is_not_overwritten(self):
        gateway = _Gateway()
        gateway.summary_gate = threading.Event()
        svc, recorder = self._service(gateway=gateway)
        self.assertEqual(svc.handle_start({}).get("status"), "running")

        def stop_a():
            svc.handle_stop({"auto_summary": True})

        thread = threading.Thread(target=stop_a, daemon=True)
        thread.start()
        self.assertTrue(gateway.summary_entered.wait(5.0))

        start_b = svc.handle_start({})
        self.assertEqual(start_b.get("status"), "running", start_b)
        gen_b = svc._gen

        gateway.summary_gate.set()
        thread.join(5.0)
        self.assertFalse(thread.is_alive())

        state = svc.state
        self.assertTrue(state.get("active"), state)
        self.assertEqual(state.get("status"), "running")
        self.assertEqual(state.get("session_id"), start_b.get("session_id"))
        self.assertIs(svc._gen, gen_b)
        self.assertTrue(recorder.is_recording)

        svc.handle_stop({})

    def test_stop_admission_and_transition_share_one_lock_block(self):
        tree = ast.parse(SERVICE_PATH.read_text(encoding="utf-8"))

        function = next(
            (node for node in ast.walk(tree)
             if isinstance(node, ast.FunctionDef) and node.name == "_handle_stop_impl"),
            None,
        )
        self.assertIsNotNone(function, "_handle_stop_impl not found")

        def is_lock_with(node: ast.With) -> bool:
            for item in node.items:
                expr = item.context_expr
                if (
                    isinstance(expr, ast.Attribute)
                    and expr.attr == "_lock"
                    and isinstance(expr.value, ast.Name)
                    and expr.value.id == "self"
                ):
                    return True
            return False

        def reads_active(node: ast.AST) -> bool:
            # Только вызов self._state.get("active"): Subscript ловить нельзя —
            # цель присваивания self._state["active"] = False это тот же узел
            # Subscript, и блок с одной записью считался бы «читающим».
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    if child.func.attr == "get" and isinstance(child.func.value, ast.Attribute):
                        if child.func.value.attr == "_state" and child.args:
                            if isinstance(child.args[0], ast.Constant) and child.args[0].value == "active":
                                return True
            return False

        def writes_active_false(node: ast.AST) -> bool:
            for child in ast.walk(node):
                if not isinstance(child, ast.Assign):
                    continue
                for target in child.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Attribute)
                        and target.value.attr == "_state"
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "active"
                        and isinstance(child.value, ast.Constant)
                        and child.value.value is False
                    ):
                        return True
            return False

        atomic_blocks = [
            node for node in ast.walk(function)
            if isinstance(node, ast.With)
            and is_lock_with(node)
            and reads_active(node)
            and writes_active_false(node)
        ]
        self.assertEqual(
            len(atomic_blocks),
            1,
            "stop admission read and active=False transition must share one self._lock block",
        )


if __name__ == "__main__":
    unittest.main()
