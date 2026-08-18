"""W8 — наблюдаемость заблокированного wake_word_start (Fable-ревью 2026-08-18).

Дефект A: W7-гейт отвергает `wake_word_start`, пока worker рекордера жив после
`stop()`. Reason переиспользован от настоящей записи, поэтому Swift считает отказ
транзиентным и ретраит вечно, сессия слушателя не создаётся, а watchdog видит
`running=False, model=None` и трактует это как ЛЕГИТИМНУЮ паузу — эпизод
сбрасывается каждый тик, `wedged` недостижим, путь DEFERRED_WORKER_HUNG (введён
2026-08-09 именно против «тихого бессрочного простоя») не достигается.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import ReinitOutcome  # noqa: E402
from backend.openwakeword_adapter import (  # noqa: E402
    RECORDER_WORKER_HUNG_REASON,
    RECORDING_IN_PROGRESS_REASON,
    OpenWakeWordAdapter,
)
from backend.wake_word_watchdog import WakeWordWatchdog  # noqa: E402

_STALE_SEC = 30.0


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeAdapter:
    """Слушателя нет: ровно то, что видит watchdog после stop() + отказа start."""

    def __init__(self, running: bool = False, model: str | None = None) -> None:
        self.running = running
        self.model = model
        self.wedged = False

    def is_running(self) -> bool:
        return self.running

    def active_model(self) -> str | None:
        return self.model

    def heartbeat(self) -> dict:
        return {"last_chunk_ts": None, "listen_started_ts": None}

    def set_wedged(self, v: bool) -> None:
        self.wedged = bool(v)

    def is_wedged(self) -> bool:
        return self.wedged


class _FakeCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    def reinit_with_wake_word_restore(self):
        self.calls += 1
        return ReinitOutcome.OK


class _FakeErrorBus:
    def __init__(self) -> None:
        self.pushed = []

    def push(self, err) -> None:
        self.pushed.append(err)


def _settings(**over):
    base = {"privacy_mode_enabled": False, "wake_word_enabled": True}
    base.update(over)
    return lambda k, d=None: base.get(k, d)


class BlockedStartReasonTest(unittest.TestCase):
    """Adapter обязан различать «идёт запись» и «worker завис»."""

    def _adapter(self, *, recording: bool, blocked: bool) -> OpenWakeWordAdapter:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return OpenWakeWordAdapter(
            data_dir=Path(self._tmp.name),
            settings_get=_settings(),
            is_recording=lambda: recording,
            is_start_blocked=lambda: blocked,
        )

    def test_real_recording_keeps_legacy_reason(self):
        """Контракт со Swift: настоящая запись — прежняя строка (бюджет не жжётся)."""
        res = self._adapter(recording=True, blocked=True).handle_wake_word_start({})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], RECORDING_IN_PROGRESS_REASON)

    def test_hung_worker_gets_distinct_reason(self):
        """is_recording лжёт False после stop()-таймаута, worker физически жив."""
        res = self._adapter(recording=False, blocked=True).handle_wake_word_start({})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], RECORDER_WORKER_HUNG_REASON)

    def test_reasons_are_distinct_strings(self):
        """Swift сравнивает ТОЧНОЙ строкой — константы обязаны различаться."""
        self.assertNotEqual(RECORDING_IN_PROGRESS_REASON, RECORDER_WORKER_HUNG_REASON)


class BlockedStartWatchdogTest(unittest.TestCase):
    """Watchdog обязан отличать легитимную паузу от заблокированного старта."""

    def _watchdog(self, *, recording: bool, worker_hung: bool):
        self.adapter = _FakeAdapter(running=False, model=None)
        self.bus = _FakeErrorBus()
        self.clock = _Clock()
        return WakeWordWatchdog(
            adapter=self.adapter,
            reinit_coordinator=_FakeCoordinator(),
            error_bus=self.bus,
            is_recording=lambda: recording,
            is_worker_hung=lambda: worker_hung,
            settings_get=lambda k, d: d,
            clock=self.clock,
        )

    def test_hung_worker_escalates_after_stale_window(self):
        w = self._watchdog(recording=False, worker_hung=True)
        self.assertIsNone(w.check_once(), "первый тик только взводит таймер")
        self.clock.advance(_STALE_SEC + 1.0)
        self.assertEqual(w.check_once(), "escalated")
        self.assertTrue(self.adapter.wedged, "wedged — единственный путь к kickstart")
        self.assertTrue(self.bus.pushed, "владелец обязан увидеть уведомление")

    def test_real_recording_stays_clean_pause(self):
        """Диктовка/встреча — законное голодание: ни эскалации, ни wedged."""
        w = self._watchdog(recording=True, worker_hung=False)
        for _ in range(10):
            self.assertIsNone(w.check_once())
            self.clock.advance(_STALE_SEC)
        self.assertFalse(self.adapter.wedged)
        self.assertFalse(self.bus.pushed)

    def test_recording_wins_over_hung_flag(self):
        """Пока идёт реальная запись, зависший флаг не эскалирует (fail-safe)."""
        w = self._watchdog(recording=True, worker_hung=True)
        for _ in range(5):
            w.check_once()
            self.clock.advance(_STALE_SEC)
        self.assertFalse(self.adapter.wedged)

    def test_absent_callback_keeps_legacy_behaviour(self):
        """Без колбэка (старая проводка) — поведение как раньше: чистая пауза."""
        adapter = _FakeAdapter(running=False, model=None)
        clock = _Clock()
        w = WakeWordWatchdog(
            adapter=adapter,
            reinit_coordinator=_FakeCoordinator(),
            error_bus=_FakeErrorBus(),
            is_recording=lambda: False,
            settings_get=lambda k, d: d,
            clock=clock,
        )
        for _ in range(5):
            self.assertIsNone(w.check_once())
            clock.advance(_STALE_SEC)
        self.assertFalse(adapter.wedged)


class CrossLanguageReasonContractTest(unittest.TestCase):
    """Swift сравнивает reason ТОЧНОЙ строкой — дрейф ловим тестом, не ревью.

    Если Python-константу переименуют, а Swift забудут, отказ уедет в
    персистентную ветку, сожжёт `maxFailedStartAttempts` за 3 попытки и wake
    word умрёт до ручного перетыкания тумблера — тише и хуже исходного бага.
    """

    SWIFT = (
        PROJECT_ROOT.parent
        / "native/KrabEarAgent/Sources/KrabEarAgent/WakeWordPoller.swift"
    )

    def setUp(self):
        if not self.SWIFT.exists():
            self.skipTest(f"Swift-исходник недоступен: {self.SWIFT}")
        self.src = self.SWIFT.read_text(encoding="utf-8")

    def test_swift_declares_both_reasons_verbatim(self):
        self.assertIn(
            f'static let recordingInProgressReason = "{RECORDING_IN_PROGRESS_REASON}"',
            self.src,
        )
        self.assertIn(
            f'static let recorderWorkerHungReason = "{RECORDER_WORKER_HUNG_REASON}"',
            self.src,
        )

    def test_swift_treats_worker_hung_as_transient(self):
        """Обе причины обязаны сравниваться ДО ветки, жгущей бюджет."""
        hung = self.src.index("why == Self.recorderWorkerHungReason")
        burn = self.src.index("self.failedStartAttempts += 1")
        self.assertLess(hung, burn, "worker-hung должен обрабатываться до сжигания бюджета")


if __name__ == "__main__":
    unittest.main()
