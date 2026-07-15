"""WakeWordWatchdog — матрица check_once + жизненный цикл (спека 2026-07-15).

Всё на фейках с инжектированным clock; реальный тред — только в
LifecycleTests (короткий interval, join по Event).
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import ReinitOutcome  # noqa: E402
from backend.wake_word_watchdog import WakeWordWatchdog  # noqa: E402


class _FakeAdapter:
    def __init__(self):
        self.running = True
        self.model = "hey_jarvis"
        self.hb = {"last_chunk_ts": None, "listen_started_ts": None}
        self.wedged = False

    def is_running(self):
        return self.running

    def active_model(self):
        return self.model

    def heartbeat(self):
        return dict(self.hb)

    def set_wedged(self, v):
        self.wedged = bool(v)

    def is_wedged(self):
        return self.wedged


class _FakeCoordinator:
    def __init__(self, outcomes=None):
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def reinit_with_wake_word_restore(self):
        self.calls += 1
        return self._outcomes.pop(0) if self._outcomes else ReinitOutcome.OK


class _FakeErrorBus:
    def __init__(self):
        self.pushed = []

    def push(self, err):
        self.pushed.append(err)
        return True


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _make(adapter=None, coordinator=None, bus=None, settings=None, clock=None):
    settings = dict(settings or {})
    adapter = adapter or _FakeAdapter()
    coordinator = coordinator or _FakeCoordinator()
    clock = clock or _Clock()
    wd = WakeWordWatchdog(
        adapter=adapter,
        reinit_coordinator=coordinator,
        error_bus=bus,
        settings_get=lambda k, d: settings.get(k, d),
        clock=clock,
    )
    return wd, adapter, coordinator, clock


class CheckOnceGuardTests(unittest.TestCase):
    def test_disabled_noop(self):
        wd, adapter, coord, clock = _make(
            settings={"wake_word_watchdog_enabled": False},
        )
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 0.0}
        clock.t = 1000.0
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_inactive_session_noop_and_resets_episode(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 0.0}
        clock.t = 1000.0
        self.assertEqual(wd.check_once(), "healed")   # эпизод открыт
        adapter.running = False
        self.assertIsNone(wd.check_once())            # сессии нет → сброс
        adapter.running = True
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t}
        clock.t += 40.0
        self.assertEqual(wd.check_once(), "healed")   # новый эпизод: heal снова доступен

    def test_started_none_is_fresh(self):
        wd, adapter, coord, _ = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": None}
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_warmup_grace_within_threshold(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": 990.0}
        clock.t = 1000.0   # staleness 10 < 30
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)

    def test_fresh_chunk_noop(self):
        wd, adapter, coord, clock = _make()
        adapter.hb = {"last_chunk_ts": 995.0, "listen_started_ts": 900.0}
        clock.t = 1000.0
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)


class HealAndEscalateTests(unittest.TestCase):
    def _stale(self, adapter, clock):
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t - 60.0}

    def test_stale_triggers_single_heal(self):
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 1)

    def test_heal_does_not_close_episode_until_real_chunk(self):
        # Ловушка: после heal новая сессия даёт свежий listen_started_ts —
        # grace-окно НЕ должно сбрасывать эпизод, иначе watchdog зациклится
        # heal'ом каждые ~35с и никогда не эскалирует.
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        # heal «перезапустил» сессию: started свежий, чанков всё ещё нет
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t}
        clock.t += 10.0
        self.assertIsNone(wd.check_once())            # grace — но эпизод жив
        clock.t += 35.0                                # снова stale
        self.assertEqual(wd.check_once(), "escalated")
        self.assertTrue(adapter.wedged)
        self.assertEqual(coord.calls, 1)              # второго heal НЕ было

    def test_real_chunk_closes_episode_and_clears_wedged(self):
        wd, adapter, coord, clock = _make()
        self._stale(adapter, clock)
        wd.check_once()                                # healed
        adapter.wedged = True                          # как будто эскалировали раньше
        adapter.hb = {"last_chunk_ts": clock.t - 1.0, "listen_started_ts": clock.t - 90.0}
        self.assertIsNone(wd.check_once())
        self.assertFalse(adapter.wedged)
        # эпизод закрыт: новый stale → heal доступен снова
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 2)

    def test_thread_hung_escalates_immediately(self):
        bus = _FakeErrorBus()
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        wd, adapter, _, clock = _make(coordinator=coord, bus=bus)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "escalated")
        self.assertTrue(adapter.wedged)
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(bus.pushed[0].code, "audio.wakeword_wedged")

    def test_deferred_keeps_retrying_without_burning_attempt(self):
        coord = _FakeCoordinator(outcomes=[
            ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.BUSY, ReinitOutcome.OK,
        ])
        wd, adapter, _, clock = _make(coordinator=coord)
        self._stale(adapter, clock)
        self.assertIsNone(wd.check_once())     # deferred
        self.assertIsNone(wd.check_once())     # busy
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(coord.calls, 3)

    def test_escalation_fires_once_per_episode(self):
        bus = _FakeErrorBus()
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        wd, adapter, _, clock = _make(coordinator=coord, bus=bus)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "escalated")
        clock.t += 10.0
        self.assertIsNone(wd.check_once())     # молчим до конца эпизода
        self.assertEqual(len(bus.pushed), 1)

    def test_failed_outcome_counts_as_attempt(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.FAILED])
        wd, adapter, _, clock = _make(coordinator=coord)
        self._stale(adapter, clock)
        self.assertEqual(wd.check_once(), "healed")
        clock.t += 40.0
        self.assertEqual(wd.check_once(), "escalated")

    def test_stale_sec_clamped_from_settings(self):
        wd, adapter, coord, clock = _make(settings={"wake_word_stale_sec": 1})
        # clamp к 10: staleness 5 НЕ алармит
        adapter.hb = {"last_chunk_ts": None, "listen_started_ts": clock.t - 5.0}
        self.assertIsNone(wd.check_once())
        self.assertEqual(coord.calls, 0)


class StateTests(unittest.TestCase):
    def test_state_dict_shape(self):
        wd, adapter, _, clock = _make()
        adapter.hb = {"last_chunk_ts": clock.t - 2.0, "listen_started_ts": clock.t - 50.0}
        state = wd.state()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["session_active"])
        self.assertAlmostEqual(state["staleness_sec"], 2.0, places=3)
        self.assertFalse(state["heal_attempted_this_episode"])
        self.assertFalse(state["wedged"])


class LifecycleTests(unittest.TestCase):
    def test_start_stop_real_thread(self):
        wd, adapter, _, _ = _make()
        adapter.running = False
        wd._check_interval_sec = 0.05
        wd.start()
        self.assertTrue(wd._thread.is_alive())
        wd.start()   # идемпотентен
        wd.stop()
        self.assertFalse(wd._thread is not None and wd._thread.is_alive())
        wd.stop()    # идемпотентен

    def test_tick_exception_does_not_kill_thread(self):
        wd, adapter, _, _ = _make()
        adapter.is_running = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        wd._check_interval_sec = 0.02
        wd.start()
        done = threading.Event()
        done.wait(0.1)
        self.assertTrue(wd._thread.is_alive())
        wd.stop()


if __name__ == "__main__":
    unittest.main()
