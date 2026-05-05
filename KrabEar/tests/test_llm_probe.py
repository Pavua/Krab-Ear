"""Tests for LLMHttpProbe — active LM Studio HTTP probe thread.

Uses passive_health_check() (GET /v1/models) rather than warmup() to avoid
JIT model reload churn.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import unittest
from unittest.mock import MagicMock

from backend.llm_probe import LLMHttpProbe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeRewriter:
    """Minimal stand-in for LLMRewriter that tracks passive_health_check calls."""

    def __init__(
        self,
        *,
        reachable: bool = True,
        has_model: bool = True,
        latency_ms: int | None = 100,
    ):
        self.check_calls: int = 0
        self._reachable = reachable
        self._has_model = has_model
        self._last_latency_ms: int | None = latency_ms

    def passive_health_check(self) -> tuple[bool, bool]:
        self.check_calls += 1
        return (self._reachable, self._has_model)

    # warmup kept for backwards compatibility in any old tests that may be run
    def warmup(self) -> None:  # pragma: no cover
        pass


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
        """When passive_health_check transitions from alive to unreachable, probe must
        push a KrabError with code='rewriter.unavailable' on the error_bus."""
        rewriter = FakeRewriter(reachable=True, has_model=True, latency_ms=100)
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

        # Now make LM Studio unreachable → dead
        rewriter._reachable = False
        rewriter._has_model = False
        probe._tick()

        self.assertTrue(error_bus.push.called, "error_bus.push must be called on alive→dead")
        call_args = error_bus.push.call_args[0]
        krab_error = call_args[0]
        self.assertEqual(krab_error.code, "rewriter.unavailable")
        self.assertEqual(krab_error.component, "rewriter")


class TestLLMHttpProbeDeadToAliveEmitsRecoveredEvent(unittest.TestCase):
    """test_dead_to_alive_emits_recovered_event — event_bus.emit('rewriter_recovered', {...})."""

    def test_dead_to_alive_emits_recovered_event(self):
        """After being dead, when passive_health_check succeeds again, probe must emit
        'rewriter_recovered' on the event_bus."""
        rewriter = FakeRewriter(reachable=False, has_model=False, latency_ms=None)
        error_bus = MagicMock()
        event_bus = MagicMock()

        probe = _make_probe(rewriter, error_bus=error_bus, event_bus=event_bus)

        # First tick: dead (unreachable)
        probe._tick()
        # Probe should now be in dead state (False)

        # Now recover
        rewriter._reachable = True
        rewriter._has_model = True
        rewriter._last_latency_ms = 120
        probe._tick()

        self.assertTrue(event_bus.emit.called, "event_bus.emit must be called on dead→alive")
        call_args = event_bus.emit.call_args[0]
        event_name = call_args[0]
        payload = call_args[1]
        self.assertEqual(event_name, "rewriter_recovered")
        self.assertIn("ts", payload)
        self.assertIn("latency_ms", payload)
        # GET /v1/models does not measure latency; latency_ms is always None in passive probe.
        self.assertIsNone(payload["latency_ms"])


class TestLLMHttpProbeSkipsWhenDisabled(unittest.TestCase):
    """test_skips_when_disabled — settings llm_rewrite_enabled=False → no warmup calls."""

    def test_skips_when_disabled(self):
        """When llm_rewrite_enabled=False in settings, passive_health_check must NOT be called."""
        rewriter = FakeRewriter(reachable=True, has_model=True, latency_ms=100)
        settings = {"llm_rewrite_enabled": False}

        probe = _make_probe(rewriter, settings=settings)

        probe._tick()
        probe._tick()
        probe._tick()

        self.assertEqual(
            rewriter.check_calls, 0,
            "passive_health_check must not be called when llm_rewrite_enabled=False",
        )


class TestLLMHttpProbeIntervalStaysFixed(unittest.TestCase):
    """GET /v1/models is always fast — interval stays at base_interval_sec."""

    def test_interval_stays_at_base_after_multiple_ticks(self):
        """passive_health_check is always fast; interval must remain at base_interval_sec
        regardless of how many ticks have occurred."""
        rewriter = FakeRewriter(reachable=True, has_model=True, latency_ms=50)
        probe = _make_probe(
            rewriter,
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
            max_interval_sec=0.5,
        )

        initial_interval = probe._current_interval_sec
        for _ in range(5):
            probe._tick()

        self.assertAlmostEqual(
            probe._current_interval_sec,
            initial_interval,
            places=6,
            msg="interval must not change — GET /models latency is always fast",
        )

    def test_interval_stays_at_base_when_model_evicted(self):
        """Even when model is evicted (reachable=True, has_model=False), interval is unchanged."""
        rewriter = FakeRewriter(reachable=True, has_model=False, latency_ms=50)
        probe = _make_probe(
            rewriter,
            base_interval_sec=0.05,
            cold_load_threshold_ms=3000,
            max_interval_sec=0.5,
        )

        initial_interval = probe._current_interval_sec
        probe._tick()

        self.assertAlmostEqual(
            probe._current_interval_sec,
            initial_interval,
            places=6,
        )


class TestLLMHttpProbeModelEvicted(unittest.TestCase):
    """rewriter.model_evicted diagnostic — emitted when reachable but model not loaded."""

    def test_model_evicted_error_pushed_when_reachable_but_no_model(self):
        """When passive_health_check returns (True, False), error_bus must receive
        a KrabError with code='rewriter.model_evicted'."""
        rewriter = FakeRewriter(reachable=True, has_model=False)
        error_bus = MagicMock()
        probe = _make_probe(rewriter, error_bus=error_bus)

        probe._tick()

        pushed_codes = [c[0][0].code for c in error_bus.push.call_args_list]
        self.assertIn(
            "rewriter.model_evicted",
            pushed_codes,
            f"model_evicted must be pushed; got codes: {pushed_codes}",
        )
        # Find the model_evicted error and check its severity
        evicted_err = next(
            c[0][0] for c in error_bus.push.call_args_list
            if c[0][0].code == "rewriter.model_evicted"
        )
        self.assertEqual(evicted_err.severity, "info")

    def test_model_evicted_deduped_within_window(self):
        """model_evicted must only be pushed once within the dedupe window (600 s)."""
        rewriter = FakeRewriter(reachable=True, has_model=False)
        error_bus = MagicMock()
        probe = _make_probe(rewriter, error_bus=error_bus)

        # Simulate several ticks; dedupe window not elapsed
        for _ in range(5):
            probe._tick()

        # Only the first tick should have pushed model_evicted
        model_evicted_calls = [
            c for c in error_bus.push.call_args_list
            if c[0][0].code == "rewriter.model_evicted"
        ]
        self.assertEqual(len(model_evicted_calls), 1, "model_evicted must be deduped")

    def test_model_evicted_not_pushed_when_model_loaded(self):
        """No model_evicted error when has_model=True."""
        rewriter = FakeRewriter(reachable=True, has_model=True)
        error_bus = MagicMock()
        probe = _make_probe(rewriter, error_bus=error_bus)

        probe._tick()

        for c in error_bus.push.call_args_list:
            self.assertNotEqual(c[0][0].code, "rewriter.model_evicted")


if __name__ == "__main__":
    unittest.main()
