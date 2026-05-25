"""Unit tests for CallAutoEnd (Phase 3 step 2/4)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_auto_end import (  # noqa: E402
    CallAutoEnd,
    AutoEndResult,
    MAX_DURATION_DEFAULT_SEC,
    SILENCE_PROBE_TRIGGER_SEC,
    OPERATOR_SILENT_AFTER_INTERRUPTION_SEC,
    REASON_MAX_DURATION,
    REASON_SILENCE_CONFIRMED,
    REASON_OPERATOR_SILENT,
    REASON_COST_LIMIT,
)
from backend.call_cost_estimator import CallCostEstimator  # noqa: E402
from backend.call_silence_probe import CallSilenceProbe  # noqa: E402


def _make_auto_end(**kwargs) -> CallAutoEnd:
    return CallAutoEnd(
        cost_estimator=CallCostEstimator(),
        silence_probe=CallSilenceProbe(),
        **kwargs,
    )


class TestMaxDurationRule(unittest.TestCase):
    def test_exactly_at_max_triggers(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(current_duration_sec=MAX_DURATION_DEFAULT_SEC)
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_MAX_DURATION)

    def test_just_below_max_no_trigger(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(current_duration_sec=MAX_DURATION_DEFAULT_SEC - 1)
        # Might trigger silence if silence_duration_sec == 0 → no silence rule
        # No silence, no cost, no max → should_end = False
        self.assertFalse(result.should_end)

    def test_custom_max_duration(self) -> None:
        ae = _make_auto_end(max_duration_sec=60)
        result = ae.evaluate(current_duration_sec=61)
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_MAX_DURATION)

    def test_max_duration_detail_fields(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(current_duration_sec=MAX_DURATION_DEFAULT_SEC + 10)
        self.assertIn("current_duration_sec", result.details)
        self.assertIn("max_duration_sec", result.details)


class TestSilenceRule(unittest.TestCase):
    def test_silence_trigger_exact(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=100,
            silence_duration_sec=SILENCE_PROBE_TRIGGER_SEC,
        )
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_SILENCE_CONFIRMED)

    def test_silence_below_trigger_no_end(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=100,
            silence_duration_sec=SILENCE_PROBE_TRIGGER_SEC - 1,
        )
        self.assertFalse(result.should_end)

    def test_operator_silent_after_interruption(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=100,
            silence_duration_sec=OPERATOR_SILENT_AFTER_INTERRUPTION_SEC,
            after_interruption=True,
        )
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_OPERATOR_SILENT)

    def test_operator_silent_without_interruption_uses_probe_rule(self) -> None:
        ae = _make_auto_end()
        # 15 сек тишины но НЕ после прерывания → probe trigger (>= 10 сек)
        result = ae.evaluate(
            current_duration_sec=100,
            silence_duration_sec=15.0,
            after_interruption=False,
        )
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_SILENCE_CONFIRMED)


class TestCostRule(unittest.TestCase):
    def test_cost_triggers_when_expensive(self) -> None:
        # Twilio RU: $0.049/min × 103.3 min ≈ $5.06 → warn.
        # 6200 sec < max_duration default (1800 s), so use custom max to avoid
        # max_duration rule firing first.
        ae = _make_auto_end(max_duration_sec=7200)  # 2-hour ceiling
        result = ae.evaluate(
            current_duration_sec=6200,
            provider="twilio",
            destination_country="ru",
        )
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_COST_LIMIT)

    def test_cost_no_trigger_cheap_short(self) -> None:
        ae = _make_auto_end()
        # Telnyx US: $0.004/min × 10 мин = $0.04 → no warn
        result = ae.evaluate(
            current_duration_sec=10 * 60,
            provider="telnyx",
            destination_country="us",
        )
        self.assertFalse(result.should_end)

    def test_cost_rule_skipped_without_provider(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(current_duration_sec=10 * 60)
        self.assertFalse(result.should_end)

    def test_cost_detail_fields(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=120 * 60,
            provider="twilio",
            destination_country="ru",
        )
        if result.reason == REASON_COST_LIMIT:
            self.assertIn("running_cost_usd", result.details)
            self.assertIn("provider", result.details)


class TestPriorityOrder(unittest.TestCase):
    def test_max_duration_wins_over_silence(self) -> None:
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=MAX_DURATION_DEFAULT_SEC + 10,
            silence_duration_sec=SILENCE_PROBE_TRIGGER_SEC + 5,
        )
        # max_duration checked first
        self.assertEqual(result.reason, REASON_MAX_DURATION)


class TestAutoEndResult(unittest.TestCase):
    def test_to_dict_structure(self) -> None:
        r = AutoEndResult(should_end=True, reason="test", details={"k": 1})
        d = r.to_dict()
        self.assertEqual(d["should_end"], True)
        self.assertEqual(d["reason"], "test")
        self.assertEqual(d["details"]["k"], 1)

    def test_to_dict_no_end(self) -> None:
        r = AutoEndResult(should_end=False)
        d = r.to_dict()
        self.assertFalse(d["should_end"])
        self.assertIsNone(d["reason"])


class TestHandleCheckAutoEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.ae = _make_auto_end()

    def test_handler_ok_field(self) -> None:
        result = self.ae.handle_check_auto_end({
            "session_id": "test-123",
            "current_state": {"duration_sec": 100},
        })
        self.assertTrue(result["ok"])
        self.assertIn("result", result)
        self.assertIn("should_end", result["result"])

    def test_handler_triggers_max_duration(self) -> None:
        result = self.ae.handle_check_auto_end({
            "session_id": "sess-1",
            "current_state": {"duration_sec": MAX_DURATION_DEFAULT_SEC + 60},
        })
        self.assertTrue(result["result"]["should_end"])
        self.assertEqual(result["result"]["reason"], REASON_MAX_DURATION)

    def test_handler_no_end_short_call(self) -> None:
        result = self.ae.handle_check_auto_end({
            "session_id": "sess-2",
            "current_state": {"duration_sec": 30, "silence_sec": 2},
        })
        self.assertFalse(result["result"]["should_end"])

    def test_handler_missing_params(self) -> None:
        result = self.ae.handle_check_auto_end({})
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["should_end"])

    def test_handler_invalid_state_type(self) -> None:
        result = self.ae.handle_check_auto_end({
            "session_id": "sess-x",
            "current_state": "bad-type",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["result"]["should_end"])

    def test_handler_silence_rule_via_state(self) -> None:
        result = self.ae.handle_check_auto_end({
            "session_id": "sess-3",
            "current_state": {
                "duration_sec": 300,
                "silence_sec": 12.0,
                "after_interruption": False,
            },
        })
        self.assertTrue(result["result"]["should_end"])
        self.assertEqual(result["result"]["reason"], REASON_SILENCE_CONFIRMED)


class TestWave185Requirements(unittest.TestCase):
    """Additional tests required by Wave 185 task spec."""

    def test_max_duration_30min_default(self) -> None:
        """Default max_duration is 1800 seconds (30 minutes)."""
        from backend.call_auto_end import MAX_DURATION_DEFAULT_SEC
        self.assertEqual(MAX_DURATION_DEFAULT_SEC, 1800)
        ae = _make_auto_end()
        self.assertEqual(ae.max_duration_sec, 1800)

    def test_max_duration_override(self) -> None:
        """Custom max_duration_sec overrides the default."""
        ae = _make_auto_end(max_duration_sec=300)
        self.assertEqual(ae.max_duration_sec, 300)
        result = ae.evaluate(current_duration_sec=300)
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_MAX_DURATION)

    def test_silence_triggers_end(self) -> None:
        """10+ seconds of silence triggers SILENCE_CONFIRMED end reason."""
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=120,
            silence_duration_sec=SILENCE_PROBE_TRIGGER_SEC,
        )
        self.assertTrue(result.should_end)
        self.assertEqual(result.reason, REASON_SILENCE_CONFIRMED)

    def test_active_call_not_auto_ended_early(self) -> None:
        """Active call with short duration and no silence/cost is not ended."""
        ae = _make_auto_end()
        result = ae.evaluate(
            current_duration_sec=30,        # 30 sec — far below 30 min max
            silence_duration_sec=2.0,       # well below 10s trigger
            after_interruption=False,
            provider="telnyx",
            destination_country="us",       # cheap rate → no cost trigger
        )
        self.assertFalse(result.should_end)
        self.assertIsNone(result.reason)

    def test_concurrent_check(self) -> None:
        """evaluate() called from multiple threads returns consistent results."""
        import threading
        ae = _make_auto_end()
        results: list[AutoEndResult] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _eval_no_end() -> None:
            try:
                r = ae.evaluate(current_duration_sec=60, silence_duration_sec=2.0)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        def _eval_max_dur() -> None:
            try:
                r = ae.evaluate(current_duration_sec=MAX_DURATION_DEFAULT_SEC + 60)
                with lock:
                    results.append(r)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=_eval_no_end) for _ in range(3)]
            + [threading.Thread(target=_eval_max_dur) for _ in range(3)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), 6)
        # All max-duration evaluations should return should_end=True with reason
        max_dur_results = [r for r in results if r.should_end]
        no_end_results = [r for r in results if not r.should_end]
        self.assertEqual(len(max_dur_results), 3)
        self.assertEqual(len(no_end_results), 3)
        for r in max_dur_results:
            self.assertEqual(r.reason, REASON_MAX_DURATION)


if __name__ == "__main__":
    unittest.main()
