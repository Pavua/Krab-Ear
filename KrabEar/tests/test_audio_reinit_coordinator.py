"""AudioReinitCoordinator — single-flight танец reinit (спека 2026-07-15)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import AudioReinitCoordinator, ReinitOutcome  # noqa: E402


class _FakeAdapter:
    """Duck-type OpenWakeWordAdapter (см. test_audio_selfheal.py)."""

    def __init__(self, running=True, model="hey_jarvis", threshold=0.63,
                 stop_result=True):
        self._running = running
        self._model = model if running else None
        self._threshold = threshold if running else None
        self._stop_result = stop_result
        self.calls: list[str] = []
        self.start_args: list[tuple] = []

    def is_running(self):
        return self._running

    def active_model(self):
        return self._model

    def active_threshold(self):
        return self._threshold

    def stop(self):
        self.calls.append("stop")
        if self._stop_result:
            self._running = False
        return self._stop_result

    def start(self, model_name, on_detected, threshold=0.5, **kw):
        self.calls.append("start")
        self.start_args.append((model_name, threshold))
        self._running = True
        on_detected("smoke", 0.99)


def _make(adapter=None, recording=False, reinit_exc=None):
    calls: list[str] = []

    def _reinit():
        calls.append("reinit")
        if reinit_exc:
            raise reinit_exc

    coord = AudioReinitCoordinator(
        reinit_audio_backend=_reinit,
        is_recording=lambda: recording,
        wake_word_adapter=adapter,
    )
    return coord, calls


class DanceTests(unittest.TestCase):
    def test_ok_full_dance_order_and_restore(self):
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        coord, calls = _make(adapter=adapter)
        outcome = coord.reinit_with_wake_word_restore()
        self.assertEqual(outcome, ReinitOutcome.OK)
        self.assertEqual(adapter.calls, ["stop", "start"])
        self.assertEqual(calls, ["reinit"])
        self.assertEqual(adapter.start_args, [("krab_ru", 0.42)])

    def test_ok_without_adapter(self):
        coord, calls = _make(adapter=None)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit"])

    def test_listener_not_running_reinit_only(self):
        adapter = _FakeAdapter(running=False)
        coord, calls = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, ["reinit"])

    def test_threshold_none_falls_back_to_default(self):
        adapter = _FakeAdapter(running=True, threshold=None)
        adapter._threshold = None
        coord, _ = _make(adapter=adapter)
        coord.reinit_with_wake_word_restore()
        self.assertEqual(adapter.start_args, [("hey_jarvis", 0.5)])


class GuardTests(unittest.TestCase):
    def test_recording_defers(self):
        adapter = _FakeAdapter()
        coord, calls = _make(adapter=adapter, recording=True)
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, [])

    def test_hung_thread_skips_reinit(self):
        adapter = _FakeAdapter(stop_result=False)
        coord, calls = _make(adapter=adapter)
        self.assertEqual(
            coord.reinit_with_wake_word_restore(), ReinitOutcome.THREAD_HUNG,
        )
        self.assertEqual(adapter.calls, ["stop"])
        self.assertEqual(calls, [])  # sd._terminate НЕ вызывался

    def test_legacy_stop_returning_none_is_success(self):
        adapter = _FakeAdapter()
        adapter.stop = lambda: adapter.calls.append("stop")  # -> None
        coord, calls = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit"])

    def test_busy_when_flight_lock_held(self):
        coord, calls = _make(adapter=None)
        self.assertTrue(coord._flight_lock.acquire(blocking=False))
        try:
            self.assertEqual(
                coord.reinit_with_wake_word_restore(), ReinitOutcome.BUSY,
            )
        finally:
            coord._flight_lock.release()
        self.assertEqual(calls, [])

    def test_reinit_exception_returns_failed_but_restores_listener(self):
        adapter = _FakeAdapter(running=True)
        coord, calls = _make(adapter=adapter, reinit_exc=RuntimeError("boom"))
        self.assertEqual(
            coord.reinit_with_wake_word_restore(), ReinitOutcome.FAILED,
        )
        self.assertEqual(adapter.calls, ["stop", "start"])


if __name__ == "__main__":
    unittest.main()
