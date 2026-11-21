"""KE05A-16 / KE05A-09: фоновый зонд LM Studio переживает сбой тика и stop/start.

* Исключение в ``_tick`` (например, из ``passive_health_check``) завершало поток навсегда.
* ``stop()`` с незавершённой пробой обнулял ``_thread``, а ``start()`` очищал ОБЩИЙ stop-event —
  старый поток оживал и параллельно запускался второй.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.llm_probe import LLMHttpProbe  # noqa: E402

_SETTINGS = {"llm_rewrite_enabled": True}


def _probe(rewriter) -> LLMHttpProbe:
    return LLMHttpProbe(
        rewriter=rewriter,
        error_bus=MagicMock(),
        event_bus=MagicMock(),
        settings_provider=lambda: dict(_SETTINGS),
        base_interval_sec=0.03,
    )


def _alive_probe_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "LLMHttpProbe" and t.is_alive())


def _wait(cond, timeout=5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class TickFailureDoesNotKillThread(unittest.TestCase):
    def test_loop_survives_exception_in_tick(self):
        calls = {"n": 0}

        class Boom:
            def passive_health_check(self):
                calls["n"] += 1
                raise RuntimeError("probe blew up")

        probe = _probe(Boom())
        probe.start()
        self.addCleanup(probe.stop)
        self.assertTrue(_wait(lambda: calls["n"] >= 3), f"поток умер после первого сбоя (calls={calls['n']})")
        self.assertEqual(_alive_probe_threads(), 1)


class StopStartDoesNotDuplicateThreads(unittest.TestCase):
    def test_restart_during_inflight_probe_keeps_single_thread(self):
        entered = threading.Event()
        gate = threading.Event()

        class Slow:
            def passive_health_check(self):
                entered.set()
                gate.wait(10)
                return (True, True)

        probe = _probe(Slow())
        probe.start()
        self.addCleanup(gate.set)
        self.addCleanup(probe.stop)
        self.assertTrue(entered.wait(5))
        probe.stop()  # проба зависла: join(2) истекает, поток ещё жив
        probe.start()  # не должен ни оживить старый поток, ни запустить второй
        self.assertEqual(_alive_probe_threads(), 1)
        gate.set()
        # старый поток обязан завершиться по СВОЕМУ stop-событию, а не крутиться дальше
        self.assertTrue(_wait(lambda: _alive_probe_threads() <= 1))
        probe.stop()
        self.assertTrue(_wait(lambda: _alive_probe_threads() == 0), "поток зонда не остановился")

    def test_normal_stop_then_start_works(self):
        class Ok:
            calls = 0

            def passive_health_check(self):
                Ok.calls += 1
                return (True, True)

        probe = _probe(Ok())
        probe.start()
        self.assertTrue(_wait(lambda: Ok.calls >= 2))
        probe.stop()
        self.assertTrue(_wait(lambda: _alive_probe_threads() == 0))
        before = Ok.calls
        probe.start()
        self.addCleanup(probe.stop)
        self.assertTrue(_wait(lambda: Ok.calls > before + 1))
        self.assertEqual(_alive_probe_threads(), 1)


if __name__ == "__main__":
    unittest.main()
