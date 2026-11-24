"""KEG2-09: барьер «проверка покоя → остановка backend».

Снимок get_recording_state/get_meeting_live_state не барьер: между ним и
kickstart новая запись стартует свободно. Тесты фиксируют атомарность
``MaintenanceFence.close_if_quiet`` относительно стартов из ДРУГОГО потока и
подключение fence в трёх путях старта (запись, встреча, CallAssist).
"""
from __future__ import annotations

import ast
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.maintenance_fence import (  # noqa: E402
    DEFAULT_LEASE_SEC,
    MAX_LEASE_SEC,
    MIN_LEASE_SEC,
    REASON_MAINTENANCE,
    MaintenanceFence,
    activity_busy_reasons,
)

_WAIT = 10.0


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _idle() -> list[str]:
    return []


# ---------------------------------------------------------------------------
# MaintenanceFence — unit
# ---------------------------------------------------------------------------


class FenceUnitTest(unittest.TestCase):
    def test_close_when_idle_blocks_admission_until_reopen(self):
        fence = MaintenanceFence()
        res = fence.close_if_quiet(_idle)
        self.assertTrue(res["ok"])
        self.assertEqual(res["lease_sec"], DEFAULT_LEASE_SEC)
        self.assertIsNone(fence.admit("recording"))
        self.assertTrue(fence.reopen(res["token"]))
        ticket = fence.admit("recording")
        self.assertIsNotNone(ticket)
        fence.release(ticket)

    def test_lease_expiry_reopens_admission(self):
        clock = _Clock()
        fence = MaintenanceFence(clock=clock)
        res = fence.close_if_quiet(_idle, lease_sec=60)
        self.assertTrue(res["ok"])
        clock.now += 59
        self.assertIsNone(fence.admit("recording"))
        clock.now += 2
        ticket = fence.admit("recording")
        self.assertIsNotNone(ticket, "истёкший lease обязан открыть допуск")
        fence.release(ticket)
        self.assertFalse(fence.reopen(res["token"]), "токен истёкшего lease недействителен")

    def test_reopen_rejects_foreign_token_and_already_closed(self):
        fence = MaintenanceFence()
        res = fence.close_if_quiet(_idle)
        self.assertFalse(fence.reopen("chuzhoy"))
        self.assertFalse(fence.reopen(None))
        again = fence.close_if_quiet(_idle)
        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "already_closed")
        self.assertIsNone(fence.admit("meeting"))
        self.assertTrue(fence.reopen(res["token"]))

    def test_probe_busy_or_failure_keeps_fence_open(self):
        fence = MaintenanceFence()
        busy = fence.close_if_quiet(lambda: ["recording_busy"])
        self.assertFalse(busy["ok"])
        self.assertEqual(busy["busy_reasons"], ["recording_busy"])

        def boom():
            raise RuntimeError("snapshot failed")

        unknown = fence.close_if_quiet(boom)
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["busy_reasons"], ["activity_unknown"])
        garbage = fence.close_if_quiet(lambda: "idle")  # не список — неизвестно
        self.assertFalse(garbage["ok"])
        ticket = fence.admit("recording")
        self.assertIsNotNone(ticket, "неудачный close не должен оставлять fence закрытым")
        fence.release(ticket)

    def test_lease_is_clamped(self):
        for raw, expected in ((1, MIN_LEASE_SEC), (10 ** 6, MAX_LEASE_SEC),
                              ("x", DEFAULT_LEASE_SEC), (float("nan"), DEFAULT_LEASE_SEC)):
            fence = MaintenanceFence()
            self.assertEqual(fence.close_if_quiet(_idle, lease_sec=raw)["lease_sec"], expected)

    def test_inflight_start_from_other_thread_makes_close_busy(self):
        """Старт, допущенный ДО close, но ещё не видимый пробе, — busy, не idle."""
        fence = MaintenanceFence()
        admitted = threading.Event()
        finish = threading.Event()

        def start():
            with fence.admission("recording") as ok:
                assert ok
                admitted.set()
                finish.wait(_WAIT)

        worker = threading.Thread(target=start)
        worker.start()
        try:
            self.assertTrue(admitted.wait(_WAIT))
            # Проба честно говорит idle (сессия ещё не опубликована) — именно
            # здесь снимок-подход давал TOCTOU.
            res = fence.close_if_quiet(_idle)
            self.assertFalse(res["ok"])
            self.assertEqual(res["busy_reasons"], ["starting_recording"])
        finally:
            finish.set()
            worker.join(_WAIT)
        self.assertTrue(fence.close_if_quiet(_idle)["ok"])

    def test_admission_during_probe_waits_and_is_refused_when_close_succeeds(self):
        fence = MaintenanceFence()
        in_probe = threading.Event()
        release_probe = threading.Event()
        outcome: dict = {}

        def slow_idle_probe():
            in_probe.set()
            release_probe.wait(_WAIT)
            return []

        closer = threading.Thread(target=lambda: outcome.update(close=fence.close_if_quiet(slow_idle_probe)))
        closer.start()
        self.assertTrue(in_probe.wait(_WAIT))
        starter = threading.Thread(target=lambda: outcome.update(ticket=fence.admit("call_assist")))
        starter.start()
        starter.join(0.2)
        self.assertTrue(starter.is_alive(), "допуск во время пробы обязан ждать, а не проходить")
        release_probe.set()
        closer.join(_WAIT)
        starter.join(_WAIT)
        self.assertTrue(outcome["close"]["ok"])
        self.assertIsNone(outcome["ticket"])

    def test_admission_during_probe_passes_when_probe_reports_busy(self):
        fence = MaintenanceFence()
        in_probe = threading.Event()
        release_probe = threading.Event()
        outcome: dict = {}

        def slow_busy_probe():
            in_probe.set()
            release_probe.wait(_WAIT)
            return ["meeting_busy"]

        closer = threading.Thread(target=lambda: outcome.update(close=fence.close_if_quiet(slow_busy_probe)))
        closer.start()
        self.assertTrue(in_probe.wait(_WAIT))
        starter = threading.Thread(target=lambda: outcome.update(ticket=fence.admit("recording")))
        starter.start()
        release_probe.set()
        closer.join(_WAIT)
        starter.join(_WAIT)
        self.assertFalse(outcome["close"]["ok"])
        self.assertIsNotNone(outcome["ticket"])
        fence.release(outcome["ticket"])

    def test_admission_wait_is_bounded(self):
        fence = MaintenanceFence(admit_wait_sec=0.05)
        in_probe = threading.Event()
        release_probe = threading.Event()

        def stuck_probe():
            in_probe.set()
            release_probe.wait(_WAIT)
            return []

        closer = threading.Thread(target=lambda: fence.close_if_quiet(stuck_probe))
        closer.start()
        try:
            self.assertTrue(in_probe.wait(_WAIT))
            self.assertIsNone(fence.admit("recording"), "зависшая проба — отказ старта, не вечное ожидание")
        finally:
            release_probe.set()
            closer.join(_WAIT)

    def test_activity_busy_reasons_is_fail_closed(self):
        class Snap:
            def __init__(self, value):
                self.value = value

            def activity_snapshot(self):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        idle = Snap({"known": True, "state": "idle"})
        self.assertEqual(activity_busy_reasons(("recording", idle)), [])
        self.assertEqual(
            activity_busy_reasons(
                ("recording", Snap({"known": True, "state": "busy"})),
                ("meeting", Snap({"known": False, "state": "unknown"})),
                ("call_assist", Snap(RuntimeError("x"))),
                ("other", None),
            ),
            ["recording_busy", "meeting_unknown", "call_assist_unknown", "other_unavailable"],
        )


# ---------------------------------------------------------------------------
# Интеграция: три пути старта
# ---------------------------------------------------------------------------


class _GateRecorder:
    """Shared recorder: start можно задержать барьером из теста."""

    sample_rate = 16000
    channels = 1

    def __init__(self) -> None:
        self.is_recording = False
        self.start_calls = 0
        self.in_start = threading.Event()
        self.release_start = threading.Event()
        self.release_start.set()

    def start(self, spill=None):
        self.start_calls += 1
        self.in_start.set()
        self.release_start.wait(_WAIT)
        if self.is_recording:
            return False
        self.is_recording = True
        return True

    def stop(self, timeout_sec=3.0, trim_tail_ms=0):
        import numpy as np

        was = self.is_recording
        self.is_recording = False
        if not was:
            return None
        return np.zeros(16000, dtype=np.float32), 1.0

    def snapshot_rms(self):
        return 0.0

    def snapshot_audio(self, max_duration_sec=12.0):
        import numpy as np

        return np.zeros(16000, dtype=np.float32), 1.0

    def get_duration_sec(self):
        return 0.0

    def activity_snapshot(self):
        state = "busy" if self.is_recording else "idle"
        return {"known": True, "state": state, "busy_reasons": ["capturing"] if self.is_recording else []}


class StartPathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_ctx.cleanup)
        self.tmp = Path(self._tmp_ctx.name)
        self.recorder = _GateRecorder()
        self.fence = MaintenanceFence()
        self._services = []

    def tearDown(self):
        self.recorder.release_start.set()
        for svc in self._services:
            closer = getattr(svc, "close_background_workers", None)
            if callable(closer):
                closer()

    def _core(self):
        from test_recording_stop_gate import _make_service

        core = _make_service(self.tmp)
        core.recorder = self.recorder
        core._maintenance_fence = self.fence
        self._services.append(core)
        return core

    def _call_assist(self, core):
        from test_call_assist_ownership_20260916 import FakeStore, _make_call

        call = _make_call(FakeStore(), self.recorder, core)
        call._maintenance_fence = self.fence
        self._services.append(call)
        return call

    def _probe(self, core, call=None):
        components = [("recording", core)]
        if call is not None:
            components.append(("call_assist", call))
        return lambda: activity_busy_reasons(*components)

    def test_recording_start_refused_while_fence_closed(self):
        core = self._core()
        res = self.fence.close_if_quiet(self._probe(core))
        self.assertTrue(res["ok"], res)
        started = core.handle_start_recording({"source": "dictation"})
        self.assertEqual(started["status"], REASON_MAINTENANCE)
        self.assertFalse(started["is_recording"])
        self.assertEqual(self.recorder.start_calls, 0)
        self.assertTrue(self.fence.reopen(res["token"]))
        started = core.handle_start_recording({"source": "dictation"})
        self.assertEqual(started["status"], "recording")
        core.handle_stop_recording({"generation_token": started["generation_token"]})

    def test_recording_start_racing_close_from_other_thread(self):
        """Старт в полёте (recorder.start ещё идёт) → close обязан отказать."""
        core = self._core()
        self.recorder.release_start.clear()
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(core.handle_start_recording({"source": "dictation"})))
        worker.start()
        try:
            self.assertTrue(self.recorder.in_start.wait(_WAIT))
            busy = self.fence.close_if_quiet(self._probe(core))
            self.assertFalse(busy["ok"], "close посреди старта записи = потерянная диктовка")
            self.assertIn("starting_recording", busy["busy_reasons"])
        finally:
            self.recorder.release_start.set()
            worker.join(_WAIT)
        self.assertEqual(result["status"], "recording")
        # Старт завершился — запись видна пробе, close по-прежнему отказывает.
        still = self.fence.close_if_quiet(self._probe(core))
        self.assertFalse(still["ok"])
        self.assertIn("recording_busy", still["busy_reasons"])
        core.handle_stop_recording({"generation_token": result["generation_token"]})

    def test_call_assist_start_refused_while_fence_closed(self):
        core = self._core()
        call = self._call_assist(core)
        res = self.fence.close_if_quiet(self._probe(core, call))
        self.assertTrue(res["ok"], res)
        started = call.handle_start({})
        self.assertFalse(started["ok"])
        self.assertEqual(started["error"], REASON_MAINTENANCE)
        self.assertEqual(self.recorder.start_calls, 0, "CallAssist не должен трогать recorder под fence")
        self.assertFalse(call.state.get("active"))

    def test_call_assist_start_racing_close_from_other_thread(self):
        core = self._core()
        call = self._call_assist(core)
        self.recorder.release_start.clear()
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(call.handle_start({})))
        worker.start()
        try:
            self.assertTrue(self.recorder.in_start.wait(_WAIT))
            busy = self.fence.close_if_quiet(self._probe(core, call))
            self.assertFalse(busy["ok"])
            self.assertIn("starting_call_assist", busy["busy_reasons"])
        finally:
            self.recorder.release_start.set()
            worker.join(_WAIT)
        self.assertTrue(result.get("ok"), result)
        after = self.fence.close_if_quiet(self._probe(core, call))
        self.assertFalse(after["ok"])
        self.assertIn("call_assist_busy", after["busy_reasons"])
        call.handle_stop({})

    def test_call_assist_activity_snapshot_states(self):
        """Сессия без собственного capture (borrowed) всё равно busy; лок занят — unknown."""
        core = self._core()
        call = self._call_assist(core)
        self.assertEqual(call.activity_snapshot()["state"], "idle")
        with call._lock:
            call._state["active"] = True
        snap = call.activity_snapshot()
        self.assertEqual(snap["state"], "busy")
        self.assertEqual(snap["busy_reasons"], ["call_session"])
        with call._lock:
            call._state["active"] = False
            call._stop_owner = object()
        self.assertEqual(call.activity_snapshot()["busy_reasons"], ["call_stopping"])
        with call._lock:
            call._stop_owner = None
            self.assertEqual(call.activity_snapshot()["state"], "unknown")

    def test_meeting_start_refused_before_any_setup(self):
        from test_meeting_tmp_wav_perms_2026_11_21 import _service

        meeting = _service(self.tmp)
        self._services.append(meeting)
        core_calls = []
        meeting._recording_core.handle_start_recording = lambda params: core_calls.append(params)
        meeting._maintenance_fence = self.fence
        res = self.fence.close_if_quiet(_idle)
        self.assertTrue(res["ok"])
        out = meeting.handle_meeting_start({})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], REASON_MAINTENANCE)
        self.assertEqual(core_calls, [])
        self.assertIsNone(meeting._session)


# ---------------------------------------------------------------------------
# BackendService: IPC-методы и проба
# ---------------------------------------------------------------------------


class _Snap:
    def __init__(self, state="idle"):
        self.state = state

    def activity_snapshot(self):
        return {"known": self.state != "unknown", "state": self.state}


class BackendIpcTest(unittest.TestCase):
    def _service(self, recording="idle", meeting="idle", call="idle"):
        from backend.service import BackendService

        svc = BackendService.__new__(BackendService)
        svc._maintenance_fence = MaintenanceFence()
        svc._recording_core_svc = _Snap(recording)
        svc._meeting_svc = _Snap(meeting)
        svc._call_assist = _Snap(call)
        return svc

    def test_close_and_open_roundtrip(self):
        svc = self._service()
        res = svc._handle_maintenance_fence_close({"lease_sec": 30})
        self.assertTrue(res["ok"])
        self.assertEqual(res["lease_sec"], 30.0)
        self.assertEqual(svc._maintenance_fence.status()["state"], "closed")
        self.assertEqual(svc._handle_maintenance_fence_open({"token": res["token"]}), {"ok": True})
        self.assertEqual(svc._maintenance_fence.status()["state"], "open")
        self.assertEqual(svc._handle_maintenance_fence_open({"token": res["token"]}),
                         {"ok": False, "reason": "token_mismatch"})

    def test_close_refused_for_each_busy_or_unknown_component(self):
        for kwargs, reason in (({"recording": "busy"}, "recording_busy"),
                               ({"meeting": "busy"}, "meeting_busy"),
                               ({"call": "busy"}, "call_assist_busy"),
                               ({"meeting": "unknown"}, "meeting_unknown")):
            svc = self._service(**kwargs)
            res = svc._handle_maintenance_fence_close({})
            self.assertFalse(res["ok"], kwargs)
            self.assertIn(reason, res["busy_reasons"])
            self.assertEqual(svc._maintenance_fence.status()["state"], "open")

    def test_dispatch_registers_fence_methods(self):
        source = (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        keys = {
            node.value
            for fn in ast.walk(tree)
            if isinstance(fn, ast.FunctionDef) and fn.name == "_build_dispatch_table"
            for node in ast.walk(fn)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn("maintenance_fence_close", keys)
        self.assertIn("maintenance_fence_open", keys)

    def test_fence_injected_into_all_three_start_services(self):
        """Wiring: один и тот же fence у записи, встречи и CallAssist."""
        source = (PROJECT_ROOT / "backend" / "service.py").read_text(encoding="utf-8")
        for target in ("self._recording_core_svc", "self._meeting_svc", "self._call_assist"):
            self.assertIn(f"{target}._maintenance_fence = self._maintenance_fence", source)


if __name__ == "__main__":
    unittest.main()
