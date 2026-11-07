"""OpenWakeWordAdapter.stop(): стопор процесса ≠ зависший слушатель (R5, 2026-11-07).

stop() ставил wedged, если join(3с) не дождался треда. При останове ВСЕГО процесса
(инцидент 05.11, ~80с) сам join «длится» десятки секунд — это не клин слушателя.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.openwakeword_adapter import OpenWakeWordAdapter  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class _StuckThread:
    """Тред, который не выходит; join «длится» join_cost секунд по фейковым часам."""

    def __init__(self, clock, join_cost):
        self._clock = clock
        self._cost = join_cost

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self._clock.t += self._cost


def _adapter(join_cost):
    clock = _Clock()
    a = OpenWakeWordAdapter(data_dir=tempfile.mkdtemp(), settings_get=lambda k, d: d)
    a._clock = clock
    a._thread = _StuckThread(clock, join_cost)
    return a


class StopProcessStallTests(unittest.TestCase):
    def test_join_overrun_by_process_stall_does_not_set_wedged(self):
        a = _adapter(join_cost=80.0)
        self.assertFalse(a.stop(timeout=3.0))  # тред не вышел — честно False
        self.assertFalse(a.is_wedged())

    def test_honest_timeout_still_sets_wedged(self):
        a = _adapter(join_cost=3.0)
        self.assertFalse(a.stop(timeout=3.0))
        self.assertTrue(a.is_wedged())


if __name__ == "__main__":
    unittest.main()
