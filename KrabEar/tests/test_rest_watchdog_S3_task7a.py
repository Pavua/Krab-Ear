"""RestWatchdog — сторож встроенного REST (S3/Задача 7a).

Всё на фейках с инжектированным clock/probe/owner — реальный тред только в
LifecycleTests (короткий interval, join по join()). Реальная сеть НЕ
трогается: _default_probe покрыт отдельным набором тестов с моком
requests.get.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.rest_watchdog import CONSECUTIVE_FAILURES_TO_HEAL, RestWatchdog  # noqa: E402


class _FakeOwner:
    """Фейковый владелец REST: status()/restart(), как того требует задача."""

    def __init__(self, *, running=True, enabled=True, tombstone=False,
                 ever_served=True, port=5005, restart_result=True):
        self.running = running
        self.enabled = enabled
        self.tombstone = tombstone
        self.ever_served = ever_served
        self.port = port
        self.restart_result = restart_result
        self.restart_calls = 0
        self.restart_side_effect: Exception | None = None

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
        if self.restart_side_effect is not None:
            raise self.restart_side_effect
        return self.restart_result


class _FakeProbe:
    """Инжектируемая проба: очередь результатов, дефолт — здоров (True)."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return True


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


def _make(owner=None, probe=None, bus=None, clock=None,
          tick_interval_sec=10.0, probe_interval_sec=30.0):
    owner = owner if owner is not None else _FakeOwner()
    clock = clock or _Clock()
    wd = RestWatchdog(
        owner=owner,
        probe=probe,
        error_bus=bus,
        clock=clock,
        tick_interval_sec=tick_interval_sec,
        probe_interval_sec=probe_interval_sec,
    )
    return wd, owner, clock


class SeriesNotSingleObservationTests(unittest.TestCase):
    """п.3: серия, а не одно наблюдение."""

    def test_single_probe_failure_does_not_heal(self):
        probe = _FakeProbe(results=[False])
        owner = _FakeOwner()
        wd, owner, clock = _make(owner=owner, probe=probe)
        self.assertIsNone(wd.check_once())
        self.assertEqual(owner.restart_calls, 0)

    def test_two_consecutive_failures_heal(self):
        self.assertEqual(CONSECUTIVE_FAILURES_TO_HEAL, 2)
        probe = _FakeProbe(results=[False, False])
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())            # 1-й провал — только warning
        self.assertEqual(owner.restart_calls, 0)
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "healed")    # 2-й подряд — лечение
        self.assertEqual(owner.restart_calls, 1)

    def test_healthy_probe_between_failures_resets_the_streak(self):
        # Провал, потом здоровая проба, потом ещё один провал — серия НЕ
        # должна засчитаться как 2 подряд (между ними было здоровое наблюдение).
        probe = _FakeProbe(results=[False, True, False])
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertIsNone(wd.check_once())             # здоров — серия сброшена
        clock.t += 30.0
        self.assertIsNone(wd.check_once())              # снова первый провал
        self.assertEqual(owner.restart_calls, 0)


class NonConnectionResponseIsNotFailureTests(unittest.TestCase):
    """п.2: провалом считаются ТОЛЬКО таймаут и ошибка соединения."""

    def test_repeated_non_connection_http_responses_never_heal(self):
        # Проба уже классифицирует ЛЮБОЙ HTTP-ответ (в т.ч. 429/5xx) как True
        # на своём уровне — контракт check_once() фиксируется тут: серия
        # провалов не растёт, лечения нет.
        probe = _FakeProbe(results=[True, True, True])
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        for _ in range(3):
            self.assertIsNone(wd.check_once())
            clock.t += 30.0
        self.assertEqual(owner.restart_calls, 0)


class DefaultProbeTests(unittest.TestCase):
    """Классификация исхода реальной HTTP-пробы (_default_probe)."""

    def test_default_probe_returns_true_on_429_response(self):
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(status_code=429)
            self.assertTrue(wd._default_probe())

    def test_default_probe_returns_true_on_5xx_response(self):
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get") as m:
            m.return_value = mock.Mock(status_code=503)
            self.assertTrue(wd._default_probe())

    def test_default_probe_returns_false_on_timeout(self):
        import requests
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get", side_effect=requests.exceptions.Timeout()):
            self.assertFalse(wd._default_probe())

    def test_default_probe_returns_false_on_connection_error(self):
        import requests
        owner = _FakeOwner(port=5005)
        wd = RestWatchdog(owner=owner)
        with mock.patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            self.assertFalse(wd._default_probe())


class PortHeldExternallyTests(unittest.TestCase):
    """п.4: занятый порт — не смерть.

    R2-фикс 2, часть 2 (см. test_rest_watchdog_own_503_marker_S3_r2fix2.py
    для полного набора): port_held_externally требует ТАКЖЕ
    ever_served=False — иначе "здоровая" проба на not-running почти всегда
    бьёт в НАШ ЖЕ полу-закрытый инстанс, а не в чужой легаси-юнит.
    """

    def test_healthy_probe_while_not_running_and_never_served_is_port_held_externally(self):
        probe = _FakeProbe(results=[True])
        owner = _FakeOwner(running=False, ever_served=False)
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        self.assertEqual(owner.restart_calls, 0)
        self.assertTrue(wd.state()["port_held_externally"])

    def test_running_true_with_healthy_probe_is_not_port_held_externally(self):
        probe = _FakeProbe(results=[True])
        owner = _FakeOwner(running=True)
        wd, owner, clock = _make(owner=owner, probe=probe)
        wd.check_once()
        self.assertFalse(wd.state()["port_held_externally"])

    def test_never_served_is_never_healed_even_after_many_failures(self):
        # Краевой случай, который сознательно НЕ чинится (докстринг модуля):
        # REST, ни разу не поднявшийся (конфликт порта на старте), сторож не
        # лечит — штатный поток включения рестартит backend целиком.
        probe = _FakeProbe(results=[False, False, False, False])
        owner = _FakeOwner(ever_served=False)
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        for _ in range(4):
            self.assertIsNone(wd.check_once())
            clock.t += 30.0
        self.assertEqual(owner.restart_calls, 0)


class TombstoneNeverHealsTests(unittest.TestCase):
    """п.5: не лечить надгробие."""

    def test_tombstone_skips_probe_and_never_heals(self):
        probe = _FakeProbe(results=[False] * 10)
        owner = _FakeOwner(tombstone=True)
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        for _ in range(10):
            self.assertIsNone(wd.check_once())
            clock.t += 30.0
        self.assertEqual(owner.restart_calls, 0)
        self.assertEqual(probe.calls, 0)


class DisabledModeTests(unittest.TestCase):
    def test_disabled_mode_never_probes_or_heals(self):
        probe = _FakeProbe(results=[False])
        owner = _FakeOwner(enabled=False)
        wd, owner, clock = _make(owner=owner, probe=probe)
        self.assertIsNone(wd.check_once())
        self.assertEqual(probe.calls, 0)
        self.assertEqual(owner.restart_calls, 0)


class ProbeCadenceTests(unittest.TestCase):
    """п.1 vs п.2: тик 10с, но проба не чаще раза в probe_interval_sec."""

    def test_probe_not_called_more_often_than_interval(self):
        probe = _FakeProbe(results=[True, True, True])
        owner = _FakeOwner()
        clock = _Clock()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        self.assertEqual(probe.calls, 1)
        clock.t += 10.0   # тик, но ещё не время пробы (30с)
        self.assertIsNone(wd.check_once())
        self.assertEqual(probe.calls, 1)
        clock.t += 25.0   # суммарно +35с с последней пробы
        self.assertIsNone(wd.check_once())
        self.assertEqual(probe.calls, 2)


class FaultToleranceTests(unittest.TestCase):
    def test_owner_status_exception_is_swallowed(self):
        class _BrokenOwner:
            def status(self):
                raise RuntimeError("boom")

            def restart(self):
                return True

        wd, _owner, _clock = _make(owner=_BrokenOwner())
        self.assertIsNone(wd.check_once())

    def test_probe_exception_is_swallowed_and_does_not_count_as_failure(self):
        def _raising_probe():
            raise RuntimeError("boom")

        owner = _FakeOwner()
        wd, owner, clock = _make(owner=owner, probe=_raising_probe)
        self.assertIsNone(wd.check_once())
        self.assertEqual(owner.restart_calls, 0)


class HealOutcomeTests(unittest.TestCase):
    def test_failed_restart_escalates_immediately(self):
        bus = _FakeErrorBus()
        owner = _FakeOwner(restart_result=False)
        clock = _Clock()
        probe = _FakeProbe(results=[False, False])
        wd, owner, clock = _make(owner=owner, probe=probe, bus=bus, clock=clock)
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "escalated")
        self.assertEqual(owner.restart_calls, 1)
        self.assertEqual(len(bus.pushed), 1)

    def test_restart_exception_escalates(self):
        owner = _FakeOwner()
        owner.restart_side_effect = RuntimeError("boom")
        clock = _Clock()
        probe = _FakeProbe(results=[False, False])
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "escalated")


class HealStormTests(unittest.TestCase):
    """п.8: анти-шторм и re-arm."""

    def _heal_cycle(self, wd, probe, clock):
        probe._results = [False, False]
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "healed")
        clock.t += 30.0

    def test_three_heals_then_fourth_streak_escalates(self):
        bus = _FakeErrorBus()
        owner = _FakeOwner(restart_result=True)
        clock = _Clock()
        probe = _FakeProbe()
        wd, owner, clock = _make(owner=owner, probe=probe, bus=bus, clock=clock)

        for _ in range(3):
            self._heal_cycle(wd, probe, clock)

        probe._results = [False, False]
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "escalated")
        self.assertEqual(owner.restart_calls, 3)   # 4-й restart() НЕ выдан
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(bus.pushed[0].code, "rest.wedged")

    def test_storm_window_expiry_allows_heal_again(self):
        owner = _FakeOwner(restart_result=True)
        clock = _Clock()
        probe = _FakeProbe()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        for _ in range(3):
            self._heal_cycle(wd, probe, clock)
        clock.t += 601.0   # окно 600с истекло
        probe._results = [False, False]
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "healed")
        self.assertEqual(owner.restart_calls, 4)

    def test_healthy_probe_rearms_storm_counter_immediately(self):
        # п.8: «после непрерывного здорового периода счётчик обнуляется» —
        # без ожидания 600с decay, одной здоровой пробы достаточно.
        owner = _FakeOwner(restart_result=True)
        clock = _Clock()
        probe = _FakeProbe()
        wd, owner, clock = _make(owner=owner, probe=probe, clock=clock)
        for _ in range(3):
            self._heal_cycle(wd, probe, clock)

        probe._results = [True]
        self.assertIsNone(wd.check_once())   # здоровый период — re-arm
        clock.t += 30.0

        probe._results = [False, False]
        self.assertIsNone(wd.check_once())
        clock.t += 30.0
        self.assertEqual(wd.check_once(), "healed")   # шторм уже не исчерпан
        self.assertEqual(owner.restart_calls, 4)


class StateSnapshotTests(unittest.TestCase):
    def test_state_snapshot_has_expected_fields(self):
        owner = _FakeOwner()
        wd = RestWatchdog(owner=owner)
        snap = wd.state()
        self.assertIn("consecutive_failures", snap)
        self.assertIn("port_held_externally", snap)
        self.assertIn("heal_attempts_in_window", snap)
        self.assertIn("last_probe_ts", snap)


class ErrorCodeShapeTests(unittest.TestCase):
    def test_rest_wedged_registry_entry_shape(self):
        from backend.error_codes import ERROR_REGISTRY

        entry = ERROR_REGISTRY["rest.wedged"]
        self.assertEqual(entry["severity"], "error")
        self.assertFalse(entry["actionable"])
        self.assertEqual(entry["dedupe_seconds"], 300)
        self.assertTrue(entry["user_msg_ru"])


class LifecycleTests(unittest.TestCase):
    def test_start_stop_real_thread(self):
        owner = _FakeOwner()
        probe = _FakeProbe(results=[True] * 50)
        wd = RestWatchdog(
            owner=owner, probe=probe,
            tick_interval_sec=0.02, probe_interval_sec=0.02,
        )
        wd.start()
        try:
            deadline = time.monotonic() + 2.0
            while probe.calls == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(probe.calls, 0)
        finally:
            wd.stop()
        self.assertIsNone(wd._thread)

    def test_start_is_idempotent(self):
        owner = _FakeOwner()
        wd = RestWatchdog(
            owner=owner, probe=_FakeProbe(results=[True]), tick_interval_sec=5.0,
        )
        wd.start()
        first_thread = wd._thread
        wd.start()
        try:
            self.assertIs(wd._thread, first_thread)
        finally:
            wd.stop()

    def test_stop_without_start_does_not_raise(self):
        owner = _FakeOwner()
        wd = RestWatchdog(owner=owner)
        wd.stop()  # не должно бросать


if __name__ == "__main__":
    unittest.main()
