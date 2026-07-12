"""Tests for AudioSelfHealer — passive self-heal for a wedged audio stack.

2026-07-12 prod incident: the long-lived backend process ended up with
PortAudio streams that opened without error but returned all-zero frames for
9 days ("Аудио пустое, попробуйте ещё раз", wake word silent). AudioSelfHealer
counts consecutive empty-recording outcomes fed to it by
RecordingCoreService.handle_stop_recording and reacts with a soft PortAudio
reinit, then a loud ErrorBus escalation if that doesn't help.

All tests here exercise AudioSelfHealer directly with pure fakes — no real
audio, no real sounddevice, no BackendService/RecordingCoreService
construction (per project convention: test the collaborator in isolation).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.audio_selfheal import AudioSelfHealer  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeWakeWordAdapter:
    """Duck-typed fake mirroring backend.openwakeword_adapter.OpenWakeWordAdapter."""

    def __init__(self, running: bool = False, model: str = "hey_jarvis", threshold: float = 0.63):
        self._running = running
        self._model = model if running else None
        self._threshold = threshold if running else None
        self.calls: list[str] = []
        self.start_args: list[tuple] = []

    def is_running(self) -> bool:
        return self._running

    def active_model(self):
        return self._model

    def active_threshold(self):
        return self._threshold

    def stop(self) -> None:
        self.calls.append("stop")
        self._running = False
        self._model = None
        self._threshold = None

    def start(self, model_name, on_detected, threshold=0.5, **kwargs):
        self.calls.append("start")
        self.start_args.append((model_name, threshold))
        self._running = True
        self._model = model_name
        self._threshold = threshold
        # Exercise the callback so a regression there isn't silently uncovered.
        on_detected("smoke", 0.99)


class _FakeErrorBus:
    def __init__(self):
        self.pushed: list = []

    def push(self, err) -> bool:
        self.pushed.append(err)
        return True


def _make_healer(
    *,
    settings=None,
    is_recording=lambda: False,
    wake_word_adapter=None,
    error_bus=None,
):
    """Build an AudioSelfHealer + a list that records reinit_audio_backend() calls."""
    settings = dict(settings or {})
    calls: list[str] = []

    def _reinit():
        calls.append("reinit")

    healer = AudioSelfHealer(
        reinit_audio_backend=_reinit,
        is_recording=is_recording,
        wake_word_adapter=wake_word_adapter,
        error_bus=error_bus,
        settings_get=lambda k, d: settings.get(k, d),
    )
    return healer, calls


# ---------------------------------------------------------------------------
# Counter / threshold behaviour
# ---------------------------------------------------------------------------

class ThresholdCounterTests(unittest.TestCase):

    def test_reinit_not_called_before_threshold(self):
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, [])

    def test_reinit_called_on_third_consecutive_empty(self):
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_success_resets_streak(self):
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 3})
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_success()
        healer.record_empty_result()
        healer.record_empty_result()
        # Only 2 consecutive empties since the reset — must not have reinit'd.
        self.assertEqual(calls, [])

    def test_disabled_never_triggers_reinit(self):
        healer, calls = _make_healer(
            settings={"audio_selfheal_enabled": False, "audio_selfheal_empty_threshold": 2},
        )
        for _ in range(10):
            healer.record_empty_result()
        self.assertEqual(calls, [])

    def test_custom_threshold_respected(self):
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 5})
        for _ in range(4):
            healer.record_empty_result()
        self.assertEqual(calls, [])
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_default_threshold_is_three_when_unset(self):
        healer, calls = _make_healer(settings={})
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, [])
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_threshold_below_minimum_is_clamped_to_two(self):
        """Mirrors settings_validator._RANGE_FIELDS['audio_selfheal_empty_threshold']
        = (2, 10, 3, int) — belt-and-suspenders clamp inside the healer itself,
        matching the codebase's established double-clamp convention (see
        recording_core_service._load_stop_recording_settings)."""
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 0})
        healer.record_empty_result()
        self.assertEqual(calls, [], "a single empty result must never reinit (floor=2)")
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_threshold_above_maximum_is_clamped_to_ten(self):
        healer, calls = _make_healer(settings={"audio_selfheal_empty_threshold": 999})
        for _ in range(9):
            healer.record_empty_result()
        self.assertEqual(calls, [])
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])


# ---------------------------------------------------------------------------
# is_recording guard — never reinit while audio is actively flowing
# ---------------------------------------------------------------------------

class RecordingGuardTests(unittest.TestCase):

    def test_reinit_deferred_while_recording(self):
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            is_recording=lambda: True,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, [], "reinit must not fire while a recording is active")

    def test_deferred_reinit_fires_once_recording_stops(self):
        state = {"recording": True}
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            is_recording=lambda: state["recording"],
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, [])
        state["recording"] = False
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_wake_word_not_touched_while_deferred(self):
        adapter = _FakeWakeWordAdapter(running=True)
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            is_recording=lambda: True,
            wake_word_adapter=adapter,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, [])


# ---------------------------------------------------------------------------
# Escalation — second empty cycle right after a reinit attempt
# ---------------------------------------------------------------------------

class EscalationTests(unittest.TestCase):

    def test_second_empty_cycle_after_reinit_escalates(self):
        bus = _FakeErrorBus()
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()  # crosses threshold -> reinit
        self.assertEqual(calls, ["reinit"])
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
        self.assertEqual(calls, ["reinit"])

    def test_no_escalation_without_prior_reinit_attempt(self):
        """Escalation must never fire on the FIRST threshold-crossing — only
        after an actual reinit attempt already happened once."""
        bus = _FakeErrorBus()
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])
        self.assertEqual(bus.pushed, [])

    def test_success_between_reinit_and_next_empty_prevents_escalation(self):
        bus = _FakeErrorBus()
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 3},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        healer.record_empty_result()  # reinit
        healer.record_success()       # audio recovered in between
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])  # not triggered again yet
        self.assertEqual(bus.pushed, [])

    def test_escalation_without_error_bus_does_not_raise(self):
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2}, error_bus=None,
        )
        healer.record_empty_result()
        healer.record_empty_result()  # reinit
        healer.record_empty_result()  # would escalate, but no bus -> no-op, no crash
        self.assertEqual(calls, ["reinit"])

    def test_streak_resets_after_escalation_and_can_fire_again(self):
        bus = _FakeErrorBus()
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            error_bus=bus,
        )
        healer.record_empty_result()
        healer.record_empty_result()  # reinit (streak=2)
        healer.record_empty_result()  # escalate (streak=3 >= 2), resets state
        self.assertEqual(len(bus.pushed), 1)
        self.assertEqual(calls, ["reinit"])
        # A fresh 2-empty streak should trigger a brand new reinit attempt,
        # not another immediate escalation.
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit", "reinit"])
        self.assertEqual(len(bus.pushed), 1)


# ---------------------------------------------------------------------------
# Wake word: stop before reinit, start after, with preserved model/threshold
# ---------------------------------------------------------------------------

class WakeWordRestartTests(unittest.TestCase):

    def test_stop_before_reinit_start_after_with_same_model_and_threshold(self):
        adapter = _FakeWakeWordAdapter(running=True, model="hey_mycroft", threshold=0.72)
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            wake_word_adapter=adapter,
        )
        healer.record_empty_result()
        healer.record_empty_result()

        self.assertEqual(adapter.calls, ["stop", "start"])
        self.assertEqual(calls, ["reinit"])
        self.assertEqual(adapter.start_args, [("hey_mycroft", 0.72)])

    def test_reinit_ordering_relative_to_wake_word(self):
        """stop() strictly before reinit; start() strictly after."""
        order: list[str] = []
        adapter = _FakeWakeWordAdapter(running=True, model="hey_jarvis", threshold=0.5)
        original_stop, original_start = adapter.stop, adapter.start

        def _stop():
            order.append("stop")
            original_stop()

        def _start(model_name, on_detected, threshold=0.5, **kw):
            order.append("start")
            return original_start(model_name, on_detected, threshold=threshold, **kw)

        adapter.stop = _stop
        adapter.start = _start

        def _reinit():
            order.append("reinit")

        # audio_selfheal_empty_threshold is clamped to a floor of 2 (matches
        # settings_validator._RANGE_FIELDS) — use 2, not 1.
        healer = AudioSelfHealer(
            reinit_audio_backend=_reinit,
            is_recording=lambda: False,
            wake_word_adapter=adapter,
            settings_get=lambda k, d: {"audio_selfheal_empty_threshold": 2}.get(k, d),
        )
        healer.record_empty_result()
        self.assertEqual(order, [])
        healer.record_empty_result()
        self.assertEqual(order, ["stop", "reinit", "start"])

    def test_not_running_wake_word_is_left_alone(self):
        adapter = _FakeWakeWordAdapter(running=False)
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            wake_word_adapter=adapter,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(calls, ["reinit"])

    def test_no_wake_word_adapter_does_not_raise(self):
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            wake_word_adapter=None,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])

    def test_wake_word_stop_failure_does_not_block_reinit(self):
        """A broken adapter.stop() must not prevent PortAudio reinit — the mic
        tap is more important to release than a perfectly clean wake-word
        shutdown."""
        adapter = _FakeWakeWordAdapter(running=True, model="hey_jarvis", threshold=0.5)

        def _broken_stop():
            adapter.calls.append("stop")
            raise RuntimeError("boom")

        adapter.stop = _broken_stop
        healer, calls = _make_healer(
            settings={"audio_selfheal_empty_threshold": 2},
            wake_word_adapter=adapter,
        )
        healer.record_empty_result()
        healer.record_empty_result()
        self.assertEqual(calls, ["reinit"])


if __name__ == "__main__":
    unittest.main()
