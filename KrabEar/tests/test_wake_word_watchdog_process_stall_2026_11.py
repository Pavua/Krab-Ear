"""Сторож не эскалирует при остановке ВСЕГО процесса (инцидент 2026-11-05 18:28).

Факты из backend.log: ~80с тишины у всех потоков (MemoryConductor, сам сторож,
flock настроек), read_pending_age=none → слушатель не висел в PortAudio, а
процесс стоял целиком. Сторож принял это за thread_hung → wedged → самоубийство
backend'а. Лечение: опоздавший собственный тик = общий стопор → льготные тики;
при настоящем зависании след (стек потока) попадает в событие.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_wake_word_watchdog import _FakeErrorBus, _make  # noqa: E402


def _stale(adapter):
    adapter.hb = {"last_chunk_ts": 100.0, "listen_started_ts": 50.0}


def _stale_by_stall(adapter):
    # Как в инциденте: staleness (80с) ≈ длина стопора процесса.
    adapter.hb = {"last_chunk_ts": 920.0, "listen_started_ts": 50.0}


class ProcessStallGraceTests(unittest.TestCase):
    def test_late_own_tick_defers_heal_and_escalation(self):
        wd, adapter, coord, clock = _make()
        _stale_by_stall(adapter)
        clock.t = 1000.0
        wd._note_tick_gap(80.0)  # тик опоздал на ~75с при interval=5с
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)
        self.assertFalse(adapter.wedged)

    def test_grace_is_bounded_then_normal_handling_resumes(self):
        wd, adapter, coord, clock = _make()
        _stale_by_stall(adapter)
        clock.t = 1000.0
        wd._note_tick_gap(80.0)
        wd.check_once()
        wd.check_once()
        self.assertEqual(wd.check_once(), "healed")  # льгота не вечная
        self.assertEqual(coord.calls, 1)

    def test_on_time_tick_does_not_grant_grace(self):
        wd, adapter, coord, clock = _make()
        _stale(adapter)
        clock.t = 1000.0
        wd._note_tick_gap(5.2)
        self.assertEqual(wd.check_once(), "healed")

    def test_run_loop_detects_gap_between_ticks(self):
        # Реальный _run: подменяем clock так, чтобы между тиками «прошло» 60с.
        wd, adapter, coord, clock = _make()
        wd._check_interval_sec = 0.01
        seen = []
        orig = wd._note_tick_gap

        def spy(gap):
            seen.append(gap)
            orig(gap)

        wd._note_tick_gap = spy
        ticks = {"n": 0}

        def jumping_clock():
            ticks["n"] += 1
            return ticks["n"] * 60.0

        wd._clock = jumping_clock
        wd.start()
        deadline = time.time() + 3
        while not seen and time.time() < deadline:
            time.sleep(0.01)
        wd.stop()
        self.assertTrue(seen and max(seen) >= 60.0)


class EscalationStackTests(unittest.TestCase):
    def test_escalation_attaches_listener_stack(self):
        bus = _FakeErrorBus()
        wd, adapter, coord, clock = _make(bus=bus)
        adapter.listener_stack = lambda: "File x.py, line 1, in _listen_loop"
        _stale(adapter)
        clock.t = 1000.0
        wd.check_once()          # heal
        wd.check_once()          # stale_after_reinit → escalate
        self.assertEqual(len(bus.pushed), 1)
        self.assertIn("_listen_loop", bus.pushed[0].context["listener_stack"])


class AdapterListenerStackTests(unittest.TestCase):
    def test_listener_stack_none_without_thread(self):
        from backend.openwakeword_adapter import OpenWakeWordAdapter

        a = OpenWakeWordAdapter(data_dir=".", settings_get=lambda k, d: d)
        self.assertIsNone(a.listener_stack())

    def test_listener_stack_shows_blocked_frame(self):
        from backend.openwakeword_adapter import OpenWakeWordAdapter

        a = OpenWakeWordAdapter(data_dir=".", settings_get=lambda k, d: d)
        ev = threading.Event()
        t = threading.Thread(target=ev.wait, daemon=True)
        t.start()
        a._thread = t
        try:
            stack = a.listener_stack()
        finally:
            ev.set()
            t.join(2)
        self.assertIn("wait", stack)


class ReviewFollowupTests(unittest.TestCase):
    def test_stall_does_not_age_dead_session_timer(self):
        # Ревью #1: dead_session считает время от _anomaly_since — стопор не должен его «насчитать».
        bus = _FakeErrorBus()
        wd, adapter, coord, clock = _make(bus=bus)
        adapter.running = False  # model выставлен, треда нет → dead_session
        clock.t = 1000.0
        self.assertIsNone(wd.check_once())  # аномалия зафиксирована
        clock.t = 1080.0
        wd._note_tick_gap(80.0)
        self.assertIsNone(wd.check_once())
        self.assertEqual(bus.pushed, [])

    def test_repeated_short_stalls_do_not_hide_growing_hang(self):
        # Ревью #2: льгота только когда стопор объясняет staleness.
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": 100.0, "listen_started_ts": 50.0}
        clock.t = 1000.0  # staleness 900с >> 16с стопора + 30с порога
        wd._note_tick_gap(16.0)
        self.assertEqual(wd.check_once(), "healed")

    def test_gap_threshold_scales_with_small_stale_sec(self):
        # Ревью #5: при stale_sec=10 стопор 12с уже должен давать льготу.
        wd, adapter, coord, clock = _make(settings={"wake_word_stale_sec": 10})
        adapter.hb = {"last_chunk_ts": 988.0, "listen_started_ts": 50.0}
        clock.t = 1000.0
        wd._note_tick_gap(12.0)
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_grace_cleared_by_pause(self):
        # Ревью #4: льгота не переживает эпизод.
        wd, adapter, coord, clock = _make()
        wd._note_tick_gap(80.0)
        wd._reset_episode()
        adapter.hb = {"last_chunk_ts": 100.0, "listen_started_ts": 50.0}
        clock.t = 150.0
        self.assertEqual(wd.check_once(), "healed")


class WebhookCancelPendingTests(unittest.TestCase):
    def test_close_path_cancels_queued_deliveries(self):
        # Ревью #6: очередь доставок не держит выход интерпретатора.
        import tempfile
        from backend.webhook_manager import WebhookManager

        with tempfile.TemporaryDirectory() as d:
            mgr = WebhookManager(Path(d))
            gate = threading.Event()
            ran = []
            futures = [mgr._executor.submit(gate.wait, 5) for _ in range(4)]
            queued = mgr._executor.submit(ran.append, 1)
            mgr.shutdown(wait=False, cancel_pending=True)
            gate.set()
            for f in futures:
                f.result(timeout=5)
            self.assertTrue(queued.cancelled())
            self.assertEqual(ran, [])


if __name__ == "__main__":
    unittest.main()
