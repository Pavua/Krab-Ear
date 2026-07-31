"""S3/Задача 7b, п.7: терминальная защёлка RestWatchdog.begin_shutdown().

Фейки скопированы из test_rest_watchdog_S3_task7a.py (независимость
тест-файлов — тот же приём, что у соседних test_rest_inprocess_*.py).
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

from backend.rest_watchdog import RestWatchdog  # noqa: E402


class _FakeOwner:
    def __init__(self, *, running=True, enabled=True, tombstone=False,
                 ever_served=True, port=5005, restart_result=True):
        self.running = running
        self.enabled = enabled
        self.tombstone = tombstone
        self.ever_served = ever_served
        self.port = port
        self.restart_result = restart_result
        self.restart_calls = 0

    def status(self):
        return {
            "running": self.running,
            "enabled": self.enabled,
            "tombstone": self.tombstone,
            "ever_served": self.ever_served,
            "port": self.port,
        }

    def restart(self):
        self.restart_calls += 1
        return self.restart_result


class _FakeProbe:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return False  # дефолт "нездоров" — латч должен всё равно не лечить


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _make(owner=None, probe=None, clock=None):
    owner = owner if owner is not None else _FakeOwner()
    clock = clock or _Clock()
    wd = RestWatchdog(owner=owner, probe=probe, clock=clock)
    return wd, owner, clock


class TerminalLatchBlocksNewTicksTest(unittest.TestCase):
    def test_begin_shutdown_prevents_check_once_from_probing(self):
        probe = _FakeProbe(results=[False, False, False])
        owner = _FakeOwner()
        wd, owner, clock = _make(owner=owner, probe=probe)
        wd.begin_shutdown()

        self.assertIsNone(wd.check_once())
        self.assertEqual(probe.calls, 0, "защёлка обязана блокировать ДАЖЕ пробу")
        self.assertEqual(owner.restart_calls, 0)

    def test_begin_shutdown_prevents_healing_even_after_many_ticks(self):
        probe = _FakeProbe(results=[False] * 20)
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        wd.begin_shutdown()

        for _ in range(20):
            self.assertIsNone(wd.check_once())
            clock.t += 30.0
        self.assertEqual(owner.restart_calls, 0)

    def test_begin_shutdown_mid_streak_stops_further_progress(self):
        # Серия уже начата ДО защёлки — после begin_shutdown() дальнейшие
        # тики не продолжают её и не лечат.
        probe = _FakeProbe(results=[False])
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())  # 1-й провал — только warning

        wd.begin_shutdown()
        probe._results = [False]
        clock.t += 30.0
        self.assertIsNone(wd.check_once())
        self.assertEqual(owner.restart_calls, 0)


class HealOrEscalateGuardTest(unittest.TestCase):
    """Более узкое окно, чем guard в check_once(): защёлка выставляется
    МЕЖДУ решением «пора лечить» (серия набрана) и фактическим
    owner.restart() — отдельный guard непосредственно перед вызовом."""

    def test_heal_or_escalate_returns_none_when_shutdown_flag_set(self):
        owner = _FakeOwner(restart_result=True)
        wd, owner, clock = _make(owner=owner)
        wd.begin_shutdown()

        result = wd._heal_or_escalate(clock())
        self.assertIsNone(result)
        self.assertEqual(owner.restart_calls, 0)


class TerminalLatchIsIrreversibleTest(unittest.TestCase):
    def test_no_resume_method_exists(self):
        # Намеренно: begin_shutdown() зовётся только из финального teardown
        # процесса — отмены для живого процесса не бывает.
        wd, _owner, _clock = _make()
        self.assertFalse(hasattr(wd, "resume"))
        self.assertFalse(hasattr(wd, "end_shutdown"))

    def test_calling_begin_shutdown_twice_is_idempotent(self):
        probe = _FakeProbe(results=[False, False])
        owner = _FakeOwner()
        wd, owner, clock = _make(owner=owner, probe=probe)
        wd.begin_shutdown()
        wd.begin_shutdown()  # повторный вызов не бросает и не "отменяет"
        self.assertIsNone(wd.check_once())
        self.assertEqual(owner.restart_calls, 0)


class StopIsNotTerminalTest(unittest.TestCase):
    """Регрессия: stop() и begin_shutdown() — НЕЗАВИСИМЫЕ контролы. Голый
    stop() (без begin_shutdown()) остаётся переиспользуемым lifecycle —
    именно так им пользуются LifecycleTests в test_rest_watchdog_S3_task7a.py."""

    def test_plain_stop_without_begin_shutdown_allows_restart_of_thread(self):
        owner = _FakeOwner()
        probe = _FakeProbe(results=[True] * 50)
        wd = RestWatchdog(
            owner=owner, probe=probe, tick_interval_sec=0.02, probe_interval_sec=0.02,
        )
        wd.start()
        try:
            deadline = time.monotonic() + 2.0
            while probe.calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(probe.calls, 0)
        finally:
            wd.stop()

        # Голый stop() НЕ терминален — новый start() снова запускает тред.
        calls_before_restart = probe.calls
        wd.start()
        try:
            deadline = time.monotonic() + 2.0
            while probe.calls == calls_before_restart and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(probe.calls, calls_before_restart)
        finally:
            wd.stop()


class TerminalLatchWithRealThreadTest(unittest.TestCase):
    def test_begin_shutdown_before_start_prevents_thread_from_ever_healing(self):
        owner = _FakeOwner()
        probe = _FakeProbe(results=[False] * 200)
        wd = RestWatchdog(
            owner=owner, probe=probe, tick_interval_sec=0.02, probe_interval_sec=0.02,
        )
        wd.begin_shutdown()
        wd.start()
        try:
            time.sleep(0.3)
        finally:
            wd.stop()
        self.assertEqual(owner.restart_calls, 0)


if __name__ == "__main__":
    unittest.main()
