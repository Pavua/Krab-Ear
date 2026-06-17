"""Regression tests for W1771 hardening findings.

Finding #6 (LOW, thread-safety): warmup_probe called _circuit._transition_to()
without holding _lock. Fixed by adding CircuitBreaker.force_reset() that wraps
the call with self._lock.

Finding #7 (LOW, DoS): llm_timeout_sec uncapped — _timeout property only checked
val > 0; set_settings({llm_timeout_sec: 86400}) would block _post_lock for the full
duration. Fixed by (a) adding llm_timeout_sec to settings_validator._RANGE_FIELDS
with max=300 and (b) hard-capping _timeout property at min(val, 300.0).
"""

import inspect
import os
import sys
import threading
import unittest

# Ensure KrabEar/ is on the path (same pattern as other test files in this project)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.llm_rewriter import CircuitBreaker, CircuitState, LLMRewriter
from backend.settings_validator import SettingsValidator, _RANGE_FIELDS


class TestCircuitBreakerForceReset(unittest.TestCase):
    """Finding #6 — CircuitBreaker.force_reset() exists and is thread-safe."""

    def test_force_reset_method_exists(self):
        """force_reset() must be a public method on CircuitBreaker."""
        cb = CircuitBreaker(fail_threshold=3, initial_reset_sec=60)
        self.assertTrue(
            hasattr(cb, "force_reset"),
            "CircuitBreaker must have a public force_reset() method",
        )
        self.assertTrue(
            callable(cb.force_reset),
            "force_reset must be callable",
        )

    def test_force_reset_transitions_to_closed_from_open(self):
        """force_reset() must unconditionally move state to CLOSED."""
        cb = CircuitBreaker(fail_threshold=2, initial_reset_sec=60)
        # Drive it OPEN
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "open")

        cb.force_reset()
        self.assertEqual(cb.state, "closed")

    def test_force_reset_transitions_to_closed_from_half_open(self):
        """force_reset() also works from HALF_OPEN."""
        cb = CircuitBreaker(fail_threshold=1, initial_reset_sec=0)
        cb.record_failure()  # → OPEN
        # Simulate time elapsed by patching _opened_at to the past
        import time
        cb._opened_at = time.monotonic() - 1  # elapsed > reset_sec (0)
        # allow_request transitions to HALF_OPEN internally
        cb.allow_request()
        self.assertEqual(cb.state, "half_open")

        cb.force_reset()
        self.assertEqual(cb.state, "closed")

    def test_force_reset_resets_counters(self):
        """After force_reset(), consecutive_failures and opened_at are cleared."""
        cb = CircuitBreaker(fail_threshold=2, initial_reset_sec=60)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "open")

        cb.force_reset()
        self.assertEqual(cb.state, "closed")
        self.assertIsNone(cb._opened_at)
        self.assertEqual(cb._consecutive_failures, 0)

    def test_force_reset_holds_lock(self):
        """force_reset() must acquire _lock (verify it is NOT lock-free).

        We verify this by checking the source code no longer contains
        a bare _circuit._transition_to( call in warmup_probe().
        """
        source = inspect.getsource(LLMRewriter.warmup_probe)
        self.assertNotIn(
            "_circuit._transition_to(",
            source,
            "warmup_probe must not call _circuit._transition_to() directly "
            "(unlocked); must use force_reset() instead",
        )

    def test_force_reset_called_in_warmup_probe(self):
        """warmup_probe source must reference force_reset."""
        source = inspect.getsource(LLMRewriter.warmup_probe)
        self.assertIn(
            "force_reset()",
            source,
            "warmup_probe must call force_reset() to reset the circuit breaker",
        )

    def test_force_reset_concurrent_safety(self):
        """Concurrent force_reset() + record_failure() must not raise."""
        cb = CircuitBreaker(fail_threshold=2, initial_reset_sec=60)
        errors = []

        def spam_failures():
            for _ in range(200):
                try:
                    cb.record_failure()
                except Exception as e:
                    errors.append(e)

        def spam_resets():
            for _ in range(200):
                try:
                    cb.force_reset()
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=spam_failures)
        t2 = threading.Thread(target=spam_resets)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertFalse(
            errors,
            f"Concurrent force_reset + record_failure raised: {errors}",
        )


class TestLlmTimeoutCapProperty(unittest.TestCase):
    """Finding #7b — _timeout property hard-capped at 300.0."""

    def _make_rewriter(self, timeout_provider):
        """Construct a minimal LLMRewriter with a runtime_timeout_provider stub."""
        return LLMRewriter(
            base_url="http://localhost:11434",
            api_key="",
            model="test-model",
            timeout_sec=45.0,
            idle_keepalive_enabled=False,
            runtime_timeout_provider=timeout_provider,
        )

    def test_timeout_capped_at_300_via_provider(self):
        """When runtime provider returns a huge value, _timeout must be ≤ 300."""
        rewriter = self._make_rewriter(lambda: 999999.0)
        self.assertLessEqual(
            rewriter._timeout,
            300.0,
            "_timeout must not exceed 300.0 even when provider returns 999999",
        )

    def test_timeout_capped_at_300_fallback(self):
        """When no provider is set, fallback also capped at 300."""
        rewriter = LLMRewriter(
            base_url="http://localhost:11434",
            api_key="",
            model="test-model",
            timeout_sec=86400.0,
            idle_keepalive_enabled=False,
        )
        self.assertLessEqual(
            rewriter._timeout,
            300.0,
            "_timeout fallback must not exceed 300.0",
        )

    def test_timeout_normal_value_unchanged(self):
        """A reasonable timeout (45 s) must pass through unmodified."""
        rewriter = self._make_rewriter(lambda: 45.0)
        self.assertAlmostEqual(rewriter._timeout, 45.0, places=3)

    def test_timeout_max_boundary(self):
        """A value of exactly 300.0 must be accepted as-is."""
        rewriter = self._make_rewriter(lambda: 300.0)
        self.assertAlmostEqual(rewriter._timeout, 300.0, places=3)

    def test_timeout_just_above_cap_is_clamped(self):
        """300.001 must be clamped to 300.0."""
        rewriter = self._make_rewriter(lambda: 300.001)
        self.assertLessEqual(rewriter._timeout, 300.0)


class TestLlmTimeoutSecInRangeFields(unittest.TestCase):
    """Finding #7a — llm_timeout_sec must be validated by settings_validator."""

    def test_llm_timeout_sec_in_range_fields(self):
        """llm_timeout_sec must appear in _RANGE_FIELDS."""
        self.assertIn(
            "llm_timeout_sec",
            _RANGE_FIELDS,
            "llm_timeout_sec must be in settings_validator._RANGE_FIELDS",
        )

    def test_llm_timeout_sec_max_is_300(self):
        """The max bound for llm_timeout_sec must be ≤ 300.0."""
        min_v, max_v, default, coerce = _RANGE_FIELDS["llm_timeout_sec"]
        self.assertLessEqual(
            max_v,
            300.0,
            f"llm_timeout_sec max bound must be ≤ 300.0, got {max_v}",
        )

    def test_llm_timeout_sec_min_is_positive(self):
        """The min bound for llm_timeout_sec must be > 0."""
        min_v, max_v, default, coerce = _RANGE_FIELDS["llm_timeout_sec"]
        self.assertGreater(min_v, 0.0)

    def test_settings_validator_clamps_huge_timeout(self):
        """SettingsValidator.validate() must clamp llm_timeout_sec: 999999 → ≤ 300."""
        validator = SettingsValidator()
        settings_in = {"llm_timeout_sec": 999999.0}
        result = validator.validate(settings_in)
        clamped = result.fixed.get("llm_timeout_sec")
        self.assertIsNotNone(clamped, "llm_timeout_sec must survive validation")
        self.assertLessEqual(
            clamped,
            300.0,
            f"SettingsValidator must clamp llm_timeout_sec 999999 → ≤300, got {clamped}",
        )

    def test_settings_validator_clamps_negative_timeout(self):
        """Negative llm_timeout_sec must be clamped to the min bound (≥ 1.0)."""
        validator = SettingsValidator()
        settings_in = {"llm_timeout_sec": -5.0}
        result = validator.validate(settings_in)
        clamped = result.fixed.get("llm_timeout_sec")
        self.assertIsNotNone(clamped)
        self.assertGreaterEqual(clamped, 1.0)

    def test_settings_validator_coerce_type_is_float(self):
        """The coerce type registered in _RANGE_FIELDS must be float."""
        min_v, max_v, default, coerce = _RANGE_FIELDS["llm_timeout_sec"]
        self.assertIs(coerce, float)


if __name__ == "__main__":
    unittest.main()
