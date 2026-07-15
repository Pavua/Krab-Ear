"""Tests for AudioSelfHealer — passive self-heal for a wedged audio stack.

2026-07-12 prod incident: the long-lived backend process ended up with
PortAudio streams that opened without error but returned all-zero frames for
9 days ("Аудио пустое, попробуйте ещё раз", wake word silent). AudioSelfHealer
counts consecutive empty-recording outcomes fed to it by
RecordingCoreService.handle_stop_recording and delegates the actual reinit
dance to backend.audio_reinit.AudioReinitCoordinator, escalating loudly via
ErrorBus when a fresh empty streak follows right after a spent attempt.

All tests here exercise AudioSelfHealer directly with pure fakes (a scripted
fake AudioReinitCoordinator, a fake ErrorBus) — no real audio, no real
sounddevice, no BackendService/RecordingCoreService construction (per
project convention: test the collaborator in isolation). The reinit dance
itself (wake-word save/restore, is_recording-guard, single-flight lock) is
covered in test_audio_reinit_coordinator.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_reinit import ReinitOutcome  # noqa: E402
from backend.audio_selfheal import AudioSelfHealer  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCoordinator:
    """Duck-type AudioReinitCoordinator со скриптованными исходами."""

    def __init__(self, outcomes=None):
        self._outcomes = list(outcomes or [])
        self.calls = 0

    def reinit_with_wake_word_restore(self):
        self.calls += 1
        if self._outcomes:
            return self._outcomes.pop(0)
        return ReinitOutcome.OK


class _FakeErrorBus:
    def __init__(self):
        self.pushed: list = []

    def push(self, err) -> bool:
        self.pushed.append(err)
        return True


def _make_healer(*, settings=None, coordinator=None, error_bus=None):
    """Build an AudioSelfHealer + the (fake or given) AudioReinitCoordinator it delegates to."""
    settings = dict(settings or {})
    coordinator = coordinator or _FakeCoordinator()
    healer = AudioSelfHealer(
        reinit_coordinator=coordinator,
        error_bus=error_bus,
        settings_get=lambda k, d: settings.get(k, d),
    )
    return healer, coordinator


# ---------------------------------------------------------------------------
# Counter / threshold behaviour
# ---------------------------------------------------------------------------

class ThresholdCounterTests(unittest.TestCase):

    def test_reinit_not_called_before_threshold(self):
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0)

    def test_reinit_called_on_third_consecutive_empty(self):
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)

    def test_success_resets_streak(self):
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_success()
        healer.record_empty_result()
        healer.record_empty_result()
        # Only 2 consecutive empties since the reset — must not have reinit'd.
        self.assertEqual(coordinator.calls, 0)

    def test_disabled_never_triggers_reinit(self):
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_enabled": False, "audio_selfheal_empty_threshold": 2},
        )
        for _ in range(10):
            healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0)

    def test_custom_threshold_respected(self):
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 5})
        for _ in range(4):
            healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0)
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)

    def test_default_threshold_is_three_when_unset(self):
        healer, coordinator = _make_healer(settings={})
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0)
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)

    def test_threshold_below_minimum_is_clamped_to_two(self):
        """Mirrors settings_validator._RANGE_FIELDS['audio_selfheal_empty_threshold']
        = (2, 10, 3, int) — belt-and-suspenders clamp inside the healer itself,
        matching the codebase's established double-clamp convention (see
        recording_core_service._load_stop_recording_settings)."""
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 0})
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0, "a single empty result must never reinit (floor=2)")
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)

    def test_threshold_above_maximum_is_clamped_to_ten(self):
        healer, coordinator = _make_healer(settings={"audio_selfheal_empty_threshold": 999})
        for _ in range(9):
            healer.record_empty_result()
        self.assertEqual(coordinator.calls, 0)
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)


# ---------------------------------------------------------------------------
# Coordinator outcome handling — DEFERRED_RECORDING/BUSY don't spend the
# attempt budget, THREAD_HUNG (like OK/FAILED) does.
# ---------------------------------------------------------------------------

class OutcomeDelegationTests(unittest.TestCase):

    def test_deferred_outcome_keeps_streak_and_attempt_budget(self):
        coord = _FakeCoordinator(outcomes=[
            ReinitOutcome.DEFERRED_RECORDING, ReinitOutcome.OK,
        ])
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3}, coordinator=coord,
        )
        for _ in range(3):
            healer.record_empty_result()
        self.assertEqual(coord.calls, 1)          # DEFERRED — попытка не потрачена
        healer.record_empty_result()               # streak всё ещё >= threshold
        self.assertEqual(coord.calls, 2)          # повторная попытка, теперь OK

    def test_busy_outcome_treated_as_deferred(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.BUSY, ReinitOutcome.OK])
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2}, coordinator=coord,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coord.calls, 2)

    def test_thread_hung_counts_as_attempt_then_escalates(self):
        coord = _FakeCoordinator(outcomes=[ReinitOutcome.THREAD_HUNG])
        bus = _FakeErrorBus()
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            coordinator=coord, error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()   # attempt (THREAD_HUNG)
        healer.record_empty_result()   # streak снова >= threshold → эскалация
        self.assertEqual(coord.calls, 1)
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(bus.pushed[0].code, "audio.stack_wedged")

    def test_record_success_during_dance_is_not_clobbered(self):
        # Repro гонки: танец идёт долго, за это время record_success()
        # легитимно сбрасывает состояние. Поздний flag=True не должен
        # красть попытку reinit у следующего эпизода.
        healer_ref: list = []

        class _SlowCoordinator(_FakeCoordinator):
            def reinit_with_wake_word_restore(self):
                # «Посреди танца» штатно завершилась успешная диктовка.
                healer_ref[0].record_success()
                return super().reinit_with_wake_word_restore()

        coord = _SlowCoordinator()
        healer, _ = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2}, coordinator=coord,
        )
        healer_ref.append(healer)
        healer.record_empty_result()
        healer.record_empty_result()   # threshold → танец → внутри record_success
        self.assertEqual(coord.calls, 1)
        # Новый эпизод: обязан получить СВОЮ попытку reinit, не эскалацию.
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coord.calls, 2)


# ---------------------------------------------------------------------------
# Escalation — second empty cycle right after a reinit attempt
# ---------------------------------------------------------------------------

class EscalationTests(unittest.TestCase):

    def test_second_empty_cycle_after_reinit_escalates(self):
        bus = _FakeErrorBus()
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()  # crosses threshold -> reinit
        self.assertEqual(coordinator.calls, 1)
        self.assertEqual(bus.pushed, [])

        healer.record_empty_result()  # next recording still empty -> escalate
        self.assertEqual(len(bus.pushed), 1)
        pushed = bus.pushed[0]
        self.assertEqual(pushed.code, "audio.stack_wedged")
        self.assertEqual(pushed.component, "audio")
        self.assertEqual(pushed.severity, "error")
        self.assertFalse(pushed.actionable)
        self.assertIsNone(pushed.action_id)
        self.assertTrue(pushed.message_user)
        self.assertIn("empty_streak", pushed.context)
        # Escalation must not itself trigger another reinit attempt.
        self.assertEqual(coordinator.calls, 1)

    def test_no_escalation_without_prior_reinit_attempt(self):
        """Escalation must never fire on the FIRST threshold-crossing — only
        after an actual reinit attempt already happened once."""
        bus = _FakeErrorBus()
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)
        self.assertEqual(bus.pushed, [])

    def test_success_between_reinit_and_next_empty_prevents_escalation(self):
        bus = _FakeErrorBus()
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()  # reinit
        healer.record_success()       # audio recovered in between
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 1)  # not triggered again yet
        self.assertEqual(bus.pushed, [])

    def test_escalation_without_error_bus_does_not_raise(self):
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2}, error_bus=None,
        )
        healer.record_empty_result()
        healer.record_empty_result()  # reinit
        healer.record_empty_result()  # would escalate, but no bus -> no-op, no crash
        self.assertEqual(coordinator.calls, 1)

    def test_streak_resets_after_escalation_and_can_fire_again(self):
        bus = _FakeErrorBus()
        healer, coordinator = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()  # reinit (streak=2)
        healer.record_empty_result()  # escalate (streak=3 >= 2), resets state
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(coordinator.calls, 1)
        # A fresh 2-empty streak should trigger a brand new reinit attempt,
        # not another immediate escalation.
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(coordinator.calls, 2)
        self.assertEqual(len(bus.pushed), 1)


if __name__ == "__main__":
    unittest.main()
