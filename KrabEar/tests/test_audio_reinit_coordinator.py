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
        self._stop_epoch = 0
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
        self._stop_epoch += 1
        if self._stop_result:
            self._running = False
        return self._stop_result

    def stop_epoch(self):
        return self._stop_epoch

    def start(self, model_name, on_detected, threshold=0.5, **kw):
        self.calls.append("start")
        self.start_args.append((model_name, threshold))
        self._running = True
        on_detected("smoke", 0.99)

    def begin_maintenance(self):
        self.calls.append("begin_maintenance")

    def end_maintenance(self):
        self.calls.append("end_maintenance")


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
        # Порядок stop→start; полная последовательность с maintenance-окном
        # пинится отдельно в test_maintenance_window_covers_stop_and_reinit_only.
        self.assertEqual(
            [c for c in adapter.calls if c in ("stop", "start")], ["stop", "start"],
        )
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
        self.assertEqual([c for c in adapter.calls if c in ("stop", "start")], [])
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
        self.assertEqual(
            [c for c in adapter.calls if c in ("stop", "start")], ["stop"],
        )
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
        self.assertEqual(
            [c for c in adapter.calls if c in ("stop", "start")], ["stop", "start"],
        )

    def test_sequential_calls_release_flight_lock(self):
        # Регрессия-гард: сломанный try/finally в reinit_with_wake_word_restore
        # превратил бы ВСЕ вызовы после первого в вечный BUSY.
        coord, calls = _make(adapter=None)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit", "reinit"])

    def test_recording_started_mid_dance_defers_and_restores_listener(self):
        # is_recording: False на первом чеке, True на re-check (запись
        # стартовала, пока шёл adapter.stop()).
        answers = [False, True]
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        calls: list[str] = []

        def _reinit():
            calls.append("reinit")

        coord = AudioReinitCoordinator(
            reinit_audio_backend=_reinit,
            is_recording=lambda: answers.pop(0),
            wake_word_adapter=adapter,
        )
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertEqual(calls, [])  # Pa_Terminate НЕ вызывался
        # Слушатель восстановлен; maintenance-порядок пинится отдельно
        # в test_maintenance_cleared_on_mid_dance_defer.
        self.assertEqual(
            [c for c in adapter.calls if c in ("stop", "start")], ["stop", "start"],
        )
        self.assertEqual(adapter.start_args, [("krab_ru", 0.42)])

    def test_is_recording_exception_fails_closed(self):
        def _boom():
            raise RuntimeError("recorder broken")

        adapter = _FakeAdapter()
        calls: list[str] = []
        coord = AudioReinitCoordinator(
            reinit_audio_backend=lambda: calls.append("reinit"),
            is_recording=_boom,
            wake_word_adapter=adapter,
        )
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertEqual(calls, [])
        self.assertEqual(adapter.calls, [])  # до stop() даже не дошли

    def test_maintenance_window_covers_stop_and_reinit_only(self):
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        coord, calls = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        # Окно: begin → stop → (reinit) → end → restore-start ПОСЛЕ end.
        self.assertEqual(
            adapter.calls,
            ["begin_maintenance", "stop", "end_maintenance", "start"],
        )

    def test_maintenance_cleared_on_thread_hung(self):
        adapter = _FakeAdapter(stop_result=False)
        coord, _ = _make(adapter=adapter)
        self.assertEqual(
            coord.reinit_with_wake_word_restore(), ReinitOutcome.THREAD_HUNG,
        )
        self.assertEqual(
            adapter.calls, ["begin_maintenance", "stop", "end_maintenance"],
        )

    def test_maintenance_cleared_on_mid_dance_defer(self):
        answers = [False, True]
        adapter = _FakeAdapter(running=True)
        coord = AudioReinitCoordinator(
            reinit_audio_backend=lambda: None,
            is_recording=lambda: answers.pop(0),
            wake_word_adapter=adapter,
        )
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertEqual(adapter.calls[0], "begin_maintenance")
        self.assertIn("end_maintenance", adapter.calls)
        # restore-start (оживление слушателя) идёт ПОСЛЕ снятия окна
        self.assertGreater(
            adapter.calls.index("start"), adapter.calls.index("end_maintenance"),
        )

    def test_external_stop_during_dance_skips_restore(self):
        # Chip Finding 5: владелец выключил тумблер (wake_word_stop) в окно
        # танца — restore НЕ должен включать микрофон обратно.
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        calls: list[str] = []

        def _reinit():
            calls.append("reinit")
            adapter.stop()   # внешний stop во время reinit-фазы

        coord = AudioReinitCoordinator(
            reinit_audio_backend=_reinit,
            is_recording=lambda: False,
            wake_word_adapter=adapter,
        )
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertEqual(calls, ["reinit"])
        self.assertNotIn("start", adapter.calls)   # restore пропущен

    def test_no_external_stop_restores_listener(self):
        # Симметричный пин: без внешнего stop epoch-механика не мешает
        # штатному restore.
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)
        coord, _ = _make(adapter=adapter)
        self.assertEqual(coord.reinit_with_wake_word_restore(), ReinitOutcome.OK)
        self.assertIn("start", adapter.calls)
        self.assertEqual(adapter.start_args, [("krab_ru", 0.42)])

    def test_external_stop_during_stop_join_skips_restore_on_deferred_path(self):
        # Тот же гард на mid-dance-defer пути: toggle-off, пока шёл
        # stop-join, а затем re-check увидел запись.
        answers = [False, True]
        adapter = _FakeAdapter(running=True, model="krab_ru", threshold=0.42)

        def _recording():
            v = answers.pop(0)
            if v:
                # запись «увидена» на re-check; toggle-off случился раньше
                pass
            return v

        coord = AudioReinitCoordinator(
            reinit_audio_backend=lambda: None,
            is_recording=_recording,
            wake_word_adapter=adapter,
        )
        # внешний stop между стопом координатора и re-check: эмулируем,
        # обернув is_recording — проще инжектнуть через сам адаптер:
        orig_stop = adapter.stop

        def _stop_and_external():
            r = orig_stop()          # стоп координатора
            orig_stop()              # и сразу внешний toggle-off
            return r

        adapter.stop = _stop_and_external
        self.assertEqual(
            coord.reinit_with_wake_word_restore(),
            ReinitOutcome.DEFERRED_RECORDING,
        )
        self.assertNotIn("start", adapter.calls)   # restore пропущен и тут


if __name__ == "__main__":
    unittest.main()
