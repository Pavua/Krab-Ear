"""test_realtime_adaptive_backoff.py

A3 unit tests for adaptive preview backoff logic.

The Swift implementation lives in main+RealtimeOverlay.swift.
This test validates the same algorithm in pure Python so it can run in CI
without a Swift build.

Constants mirror those declared as `static let` in AgentAppDelegate:
  - previewSilenceRmsThreshold = 0.02
  - previewSilenceTicksToBackoff = 3
  - previewActiveInterval = 0.85
  - previewSilenceInterval = 3.0
"""

import unittest

# ---------------------------------------------------------------------------
# Python mirror of the Swift adaptive backoff logic
# ---------------------------------------------------------------------------

SILENCE_RMS_THRESHOLD = 0.02
SILENCE_TICKS_TO_BACKOFF = 3
ACTIVE_INTERVAL = 0.85
SILENCE_INTERVAL = 3.0


class AdaptiveBackoffSimulator:
    """Pure-Python simulation of updatePreviewPollingInterval() in Swift."""

    def __init__(self):
        self.current_interval: float = ACTIVE_INTERVAL
        self.silence_tick_count: int = 0
        self.last_rms: float = 1.0
        # Record interval transitions for assertions
        self.transitions: list[float] = []

    def feed_rms(self, rms: float) -> None:
        """Simulate one call to updatePreviewPollingInterval(rms:)."""
        is_silent = rms < SILENCE_RMS_THRESHOLD

        if is_silent:
            self.silence_tick_count += 1
            should_backoff = self.silence_tick_count >= SILENCE_TICKS_TO_BACKOFF
            if should_backoff and self.current_interval < SILENCE_INTERVAL - 0.1:
                self._set_interval(SILENCE_INTERVAL)
        else:
            if (
                self.silence_tick_count >= SILENCE_TICKS_TO_BACKOFF
                and self.current_interval > ACTIVE_INTERVAL + 0.1
            ):
                self._set_interval(ACTIVE_INTERVAL)
            self.silence_tick_count = 0

        self.last_rms = rms

    def _set_interval(self, interval: float) -> None:
        self.current_interval = interval
        self.transitions.append(interval)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAdaptiveBackoffSilenceSlowdown(unittest.TestCase):
    """After SILENCE_TICKS_TO_BACKOFF consecutive silent ticks, interval widens."""

    def setUp(self):
        self.sim = AdaptiveBackoffSimulator()

    def test_no_backoff_before_threshold_ticks(self):
        # Feed 2 silent ticks (one below threshold)
        for _ in range(SILENCE_TICKS_TO_BACKOFF - 1):
            self.sim.feed_rms(0.001)
        self.assertAlmostEqual(self.sim.current_interval, ACTIVE_INTERVAL, places=2)
        self.assertEqual(len(self.sim.transitions), 0)

    def test_backoff_activates_on_third_silent_tick(self):
        for _ in range(SILENCE_TICKS_TO_BACKOFF):
            self.sim.feed_rms(0.001)
        self.assertAlmostEqual(self.sim.current_interval, SILENCE_INTERVAL, places=2)
        self.assertEqual(self.sim.transitions, [SILENCE_INTERVAL])

    def test_backoff_does_not_repeat_once_active(self):
        # Fill up to backoff, then keep feeding silence
        for _ in range(SILENCE_TICKS_TO_BACKOFF + 5):
            self.sim.feed_rms(0.001)
        # Should only have transitioned once
        self.assertEqual(len(self.sim.transitions), 1)
        self.assertAlmostEqual(self.sim.current_interval, SILENCE_INTERVAL, places=2)


class TestAdaptiveBackoffActivityRestore(unittest.TestCase):
    """When audio activity resumes, interval snaps back to active."""

    def setUp(self):
        self.sim = AdaptiveBackoffSimulator()

    def _enter_silence_mode(self):
        for _ in range(SILENCE_TICKS_TO_BACKOFF):
            self.sim.feed_rms(0.001)

    def test_activity_restores_fast_interval(self):
        self._enter_silence_mode()
        self.assertAlmostEqual(self.sim.current_interval, SILENCE_INTERVAL, places=2)

        # One active tick restores fast interval
        self.sim.feed_rms(0.1)
        self.assertAlmostEqual(self.sim.current_interval, ACTIVE_INTERVAL, places=2)
        self.assertEqual(self.sim.transitions, [SILENCE_INTERVAL, ACTIVE_INTERVAL])

    def test_silence_counter_reset_after_activity(self):
        self._enter_silence_mode()
        self.sim.feed_rms(0.1)  # active tick
        self.assertEqual(self.sim.silence_tick_count, 0)

    def test_re_entering_silence_triggers_backoff_again(self):
        self._enter_silence_mode()
        self.sim.feed_rms(0.1)  # active tick restores
        self.assertEqual(self.sim.current_interval, ACTIVE_INTERVAL)

        # Enter silence again
        for _ in range(SILENCE_TICKS_TO_BACKOFF):
            self.sim.feed_rms(0.001)
        self.assertAlmostEqual(self.sim.current_interval, SILENCE_INTERVAL, places=2)
        self.assertEqual(self.sim.transitions, [SILENCE_INTERVAL, ACTIVE_INTERVAL, SILENCE_INTERVAL])


class TestAdaptiveBackoffThresholdBoundary(unittest.TestCase):
    """RMS exactly at threshold is NOT considered silence."""

    def setUp(self):
        self.sim = AdaptiveBackoffSimulator()

    def test_rms_at_threshold_is_not_silence(self):
        # Exactly at threshold → not silent → no backoff
        for _ in range(SILENCE_TICKS_TO_BACKOFF + 1):
            self.sim.feed_rms(SILENCE_RMS_THRESHOLD)
        self.assertAlmostEqual(self.sim.current_interval, ACTIVE_INTERVAL, places=2)

    def test_rms_below_threshold_is_silence(self):
        for _ in range(SILENCE_TICKS_TO_BACKOFF):
            self.sim.feed_rms(SILENCE_RMS_THRESHOLD - 0.001)
        self.assertAlmostEqual(self.sim.current_interval, SILENCE_INTERVAL, places=2)


class TestAdaptiveBackoffNeverTransitionsDuringActivity(unittest.TestCase):
    """Steady active signal never causes any timer changes."""

    def setUp(self):
        self.sim = AdaptiveBackoffSimulator()

    def test_constant_activity_no_transitions(self):
        for _ in range(100):
            self.sim.feed_rms(0.15)
        self.assertEqual(len(self.sim.transitions), 0)
        self.assertAlmostEqual(self.sim.current_interval, ACTIVE_INTERVAL, places=2)


class TestAdaptiveBackoffConstantsConsistency(unittest.TestCase):
    """Verify constant relationships hold (mirror of Swift static let values)."""

    def test_silence_interval_greater_than_active(self):
        self.assertGreater(SILENCE_INTERVAL, ACTIVE_INTERVAL)

    def test_threshold_is_low_sensible_value(self):
        # Threshold should be below normal speech (0.05) and above ~0
        self.assertGreater(SILENCE_RMS_THRESHOLD, 0.0)
        self.assertLess(SILENCE_RMS_THRESHOLD, 0.05)

    def test_ticks_to_backoff_is_positive(self):
        self.assertGreater(SILENCE_TICKS_TO_BACKOFF, 0)

    def test_silence_interval_at_least_2x_active(self):
        # Backoff should meaningfully reduce poll frequency (>2×)
        self.assertGreater(SILENCE_INTERVAL, ACTIVE_INTERVAL * 2)


if __name__ == "__main__":
    unittest.main()
