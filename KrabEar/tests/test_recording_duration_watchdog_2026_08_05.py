"""Тесты RecordingDurationWatchdog (живой инцидент 2026-08-05).

52-минутная незамеченная диктовка обвалила STT-fallback конвейер
(GigaAM деградация → Whisper упёрся в таймаут → Remote STT без ключа →
critical error), спасена только backstop-таймаутом IPC. Watchdog даёт
раннее предупреждение задолго до того порога, где начинается деградация.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for p in (str(PROJECT_ROOT), str(PACKAGE_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend.recording_duration_watchdog import RecordingDurationWatchdog


def _make_settings(
    enabled: bool = True,
    warn_sec: float = 600.0,
    interval_sec: float = 30.0,
) -> MagicMock:
    s = MagicMock()
    s.RECORDING_DURATION_WATCHDOG_ENABLED = enabled
    s.RECORDING_DURATION_WARN_SEC = warn_sec
    s.RECORDING_DURATION_CHECK_INTERVAL_SEC = interval_sec
    return s


def _make_recorder(is_recording: bool, duration_sec: float) -> MagicMock:
    r = MagicMock()
    r.is_recording = is_recording
    r.get_duration_sec.return_value = duration_sec
    return r


class TestRecordingDurationWatchdogTick(unittest.TestCase):
    """tick() — синхронная, без реального потока/sleep."""

    def test_no_push_when_not_recording(self) -> None:
        recorder = _make_recorder(is_recording=False, duration_sec=9999.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(), error_bus=error_bus, recorder=recorder
        )
        wd.tick()
        error_bus.push.assert_not_called()

    def test_no_push_below_threshold(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=100.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.tick()
        error_bus.push.assert_not_called()

    def test_pushes_warning_at_threshold(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.tick()
        error_bus.push.assert_called_once()
        pushed = error_bus.push.call_args.args[0]
        self.assertEqual(pushed.code, "recording.long_duration_warning")
        self.assertEqual(pushed.component, "recording")
        self.assertEqual(pushed.severity, "warn")
        self.assertAlmostEqual(pushed.context["duration_sec"], 650.0)

    def test_tick_never_raises_when_error_bus_push_fails(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        error_bus.push.side_effect = RuntimeError("boom")
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.tick()  # не должно бросить

    def test_tick_never_raises_when_recorder_getters_fail(self) -> None:
        recorder = MagicMock()
        recorder.is_recording = True
        recorder.get_duration_sec.side_effect = RuntimeError("boom")
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(), error_bus=error_bus, recorder=recorder
        )
        wd.tick()  # не должно бросить
        error_bus.push.assert_not_called()

    def test_no_push_when_error_bus_is_none(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=None,
            recorder=recorder,
        )
        wd.tick()  # не должно бросить


class TestRecordingDurationWatchdogLifecycle(unittest.TestCase):
    """start()/stop() — реальный поток, но короткий и детерминированный."""

    def test_disabled_does_not_start_thread(self) -> None:
        recorder = _make_recorder(is_recording=False, duration_sec=0.0)
        wd = RecordingDurationWatchdog(
            settings=_make_settings(enabled=False),
            error_bus=MagicMock(),
            recorder=recorder,
        )
        wd.start()
        self.assertIsNone(wd._thread)

    def test_start_then_stop_joins_thread(self) -> None:
        recorder = _make_recorder(is_recording=False, duration_sec=0.0)
        wd = RecordingDurationWatchdog(
            settings=_make_settings(enabled=True, interval_sec=0.05),
            error_bus=MagicMock(),
            recorder=recorder,
        )
        wd.start()
        self.assertIsNotNone(wd._thread)
        wd.stop()
        self.assertFalse(wd._thread.is_alive())

    def test_double_start_is_noop(self) -> None:
        recorder = _make_recorder(is_recording=False, duration_sec=0.0)
        wd = RecordingDurationWatchdog(
            settings=_make_settings(enabled=True, interval_sec=5.0),
            error_bus=MagicMock(),
            recorder=recorder,
        )
        wd.start()
        first_thread = wd._thread
        wd.start()
        self.assertIs(wd._thread, first_thread)
        wd.stop()

    def test_stop_without_start_is_safe(self) -> None:
        recorder = _make_recorder(is_recording=False, duration_sec=0.0)
        wd = RecordingDurationWatchdog(
            settings=_make_settings(), error_bus=MagicMock(), recorder=recorder
        )
        wd.stop()  # не должно бросить

    def test_background_thread_actually_calls_tick(self) -> None:
        """2026-08-05 (Fable test-gap): реальный поток должен реально вызывать
        tick() — а не просто существовать. Ловим это через error_bus.push,
        а не через мок tick(), чтобы проверить весь _run()-цикл целиком."""
        recorder = _make_recorder(is_recording=True, duration_sec=9999.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=0.0, interval_sec=0.05),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.start()
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and not error_bus.push.called:
                time.sleep(0.05)
        finally:
            wd.stop()
        error_bus.push.assert_called()


class TestRecordingDurationWatchdogMeetingExemption(unittest.TestCase):
    """2026-08-05, Fable MEDIUM-2/MEDIUM-B: watchdog не должен нагонять тосты
    во время легитимно длинной записи с ДРУГИМ владельцем — meeting (C2 Live
    Meeting Overlay) ИЛИ Call Assist (владеет recorder напрямую, owner=None).
    Inclusion-семантика: owner_is_dictation_like=False → тишина."""

    def test_no_push_when_owner_not_dictation_like_meeting(self) -> None:
        """owner="meeting" → callback возвращает False → тишина."""
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
            owner_is_dictation_like=lambda: False,
        )
        wd.tick()
        error_bus.push.assert_not_called()

    def test_no_push_when_owner_not_dictation_like_call_assist(self) -> None:
        """2026-08-05 MEDIUM-B: Call Assist владеет recorder БЕЗ generation
        (current_recording_owner() → None) — та же тишина, что meeting."""
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
            owner_is_dictation_like=lambda: (None in ("dictation", "quick_capture")),
        )
        wd.tick()
        error_bus.push.assert_not_called()

    def test_pushes_when_owner_is_dictation_like(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
            owner_is_dictation_like=lambda: True,
        )
        wd.tick()
        error_bus.push.assert_called_once()

    def test_default_none_callback_does_not_suppress(self) -> None:
        """Обратная совместимость: без owner_is_dictation_like поведение прежнее."""
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.tick()
        error_bus.push.assert_called_once()


class TestRecordingDurationWatchdogRenagContract(unittest.TestCase):
    """2026-08-05 (Fable test-gap): watchdog не дедуплицирует сам — пушит на
    КАЖДЫЙ тик, пока порог превышен; периодичность нагоняющего тоста —
    ответственность error_bus'а (dedupe_seconds в ERROR_REGISTRY), не
    watchdog'а."""

    def test_pushes_on_every_qualifying_tick(self) -> None:
        recorder = _make_recorder(is_recording=True, duration_sec=650.0)
        error_bus = MagicMock()
        wd = RecordingDurationWatchdog(
            settings=_make_settings(warn_sec=600.0),
            error_bus=error_bus,
            recorder=recorder,
        )
        wd.tick()
        wd.tick()
        wd.tick()
        self.assertEqual(error_bus.push.call_count, 3)


if __name__ == "__main__":
    unittest.main()
