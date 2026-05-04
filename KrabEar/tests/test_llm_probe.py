"""Tests for LLMHttpProbe — active LM Studio HTTP probe thread.

TDD: these tests are written against the intended interface before implementation.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from backend.llm_probe import LLMHttpProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRewriter:
    """Minimal stand-in for LLMRewriter that tracks warmup calls."""

    def __init__(self, *, raise_on_warmup: bool = False, latency_ms: int | None = 100):
        self.warmup_calls: int = 0
        self._raise = raise_on_warmup
        self._last_latency_ms: int | None = latency_ms

    def warmup(self) -> None:
        self.warmup_calls += 1
        if self._raise:
            raise ConnectionError("LM Studio not reachable")


def _make_probe(
    rewriter: FakeRewriter,
    error_bus=None,
    event_bus=None,
    settings: dict | None = None,
    base_interval_sec: float = 0.05,
    cold_load_threshold_ms: int = 3000,
    max_interval_sec: float = 0.5,
    recovery_consecutive: int = 3,
) -> LLMHttpProbe:
    if error_bus is None:
        error_bus = MagicMock()
    if event_bus is None:
        event_bus = MagicMock()
    if settings is None:
        settings = {"llm_rewrite_enabled": True}
    return LLMHttpProbe(
        rewriter=rewriter,
        error_bus=error_bus,
        event_bus=event_bus,
        settings_provider=lambda: settings,
        base_interval_sec=base_interval_sec,
        cold_load_threshold_ms=cold_load_threshold_ms,
        max_interval_sec=max_interval_sec,
        recovery_consecutive=recovery_consecutive,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLLMHttpProbeAliveToDeadEmitsUnavailable(unittest.TestCase):
    """test_alive_to_dead_emits_unavailable — push 'rewriter.unavailable' KrabError."""

    def test_alive_to_dead_emits_unavailable(self):
        """When warmup starts raising, probe must push a KrabError with
        code='rewriter.unavailable' on the error_bus."""
        rewriter = FakeRewriter(raise_on_warmup=False, latency_ms=100)
        error_bus = MagicMock()
        event_bus = MagicMock()
        settings = {"llm_rewrite_enabled": True}

        probe = LLMHttpProbe(
            rewriter=rewriter,
            error_bus=error_bus,
            event_bus=event_bus,
            settings_provider=lambda: settings,
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
            max_interval_sec=0.5,
            recovery_consecutive=3,
        )

        # First tick: alive (no error)
        probe._tick()
        error_bus.push.assert_not_called()

        # Now make warmup fail → dead
        rewriter._raise = True
        rewriter._last_latency_ms = None
        probe._tick()

        self.assertTrue(error_bus.push.called, "error_bus.push must be called on alive→dead")
        call_args = error_bus.push.call_args[0]
        krab_error = call_args[0]
        self.assertEqual(krab_error.code, "rewriter.unavailable")
        self.assertEqual(krab_error.component, "rewriter")


class TestLLMHttpProbeDeadToAliveEmitsRecoveredEvent(unittest.TestCase):
    """test_dead_to_alive_emits_recovered_event — event_bus.emit('rewriter_recovered', {...})."""

    def test_dead_to_alive_emits_recovered_event(self):
        """After being dead, when warmup succeeds again, probe must emit
        'rewriter_recovered' on the event_bus."""
        rewriter = FakeRewriter(raise_on_warmup=True, latency_ms=None)
        error_bus = MagicMock()
        event_bus = MagicMock()

        probe = _make_probe(rewriter, error_bus=error_bus, event_bus=event_bus)

        # First tick: dead (warmup fails)
        probe._tick()
        # Probe should now be in dead state (False)

        # Now recover
        rewriter._raise = False
        rewriter._last_latency_ms = 120
        probe._tick()

        self.assertTrue(event_bus.emit.called, "event_bus.emit must be called on dead→alive")
        call_args = event_bus.emit.call_args[0]
        event_name = call_args[0]
        payload = call_args[1]
        self.assertEqual(event_name, "rewriter_recovered")
        self.assertIn("ts", payload)
        self.assertIn("latency_ms", payload)
        self.assertEqual(payload["latency_ms"], 120)


class TestLLMHttpProbeSkipsWhenDisabled(unittest.TestCase):
    """test_skips_when_disabled — settings llm_rewrite_enabled=False → no warmup calls."""

    def test_skips_when_disabled(self):
        """When llm_rewrite_enabled=False in settings, warmup must NOT be called."""
        rewriter = FakeRewriter(raise_on_warmup=False, latency_ms=100)
        settings = {"llm_rewrite_enabled": False}

        probe = _make_probe(rewriter, settings=settings)

        probe._tick()
        probe._tick()
        probe._tick()

        self.assertEqual(
            rewriter.warmup_calls, 0,
            "warmup must not be called when llm_rewrite_enabled=False",
        )


class TestLLMHttpProbeAdaptiveIntervalExtendsOnColdLoad(unittest.TestCase):
    """test_adaptive_interval_extends_on_cold_load — slow rewriter causes interval extension."""

    def test_adaptive_interval_extends_on_cold_load(self):
        """When rewriter._last_latency_ms > cold_load_threshold_ms, the probe interval
        must be extended (multiplied by 10, capped at max_interval_sec)."""
        # Use a latency well above threshold
        rewriter = FakeRewriter(raise_on_warmup=False, latency_ms=5000)
        probe = _make_probe(
            rewriter,
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
            max_interval_sec=0.5,
        )

        initial_interval = probe._current_interval_sec
        probe._tick()

        self.assertGreater(
            probe._current_interval_sec,
            initial_interval,
            "interval must grow after a cold-load tick",
        )
        self.assertLessEqual(
            probe._current_interval_sec,
            probe._max_interval_sec,
            "interval must not exceed max_interval_sec",
        )

    def test_adaptive_interval_resets_after_fast_responses(self):
        """After recovery_consecutive fast ticks, interval must reset to base_interval_sec."""
        rewriter = FakeRewriter(raise_on_warmup=False, latency_ms=5000)
        probe = _make_probe(
            rewriter,
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
            max_interval_sec=0.5,
            recovery_consecutive=3,
        )

        # Trigger extension
        probe._tick()
        self.assertGreater(probe._current_interval_sec, probe._base_interval_sec)

        # Now simulate fast responses
        rewriter._last_latency_ms = 200
        for _ in range(3):
            probe._tick()

        self.assertAlmostEqual(
            probe._current_interval_sec,
            probe._base_interval_sec,
            places=6,
            msg="interval must reset to base after recovery_consecutive fast ticks",
        )


if __name__ == "__main__":
    unittest.main()
