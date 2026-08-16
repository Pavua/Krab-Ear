"""Unit tests for CallCostEstimator (Phase 3 step 2/4)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_cost_estimator import (  # noqa: E402
    CallCostEstimator,
    WARN_THRESHOLD_USD,
)


class TestEstimateMinuteCost(unittest.TestCase):
    def setUp(self) -> None:
        self.est = CallCostEstimator()

    def test_telnyx_us_rate(self) -> None:
        rate = self.est.estimate_minute_cost("telnyx", "us")
        self.assertAlmostEqual(rate, 0.004)

    def test_twilio_us_rate(self) -> None:
        rate = self.est.estimate_minute_cost("twilio", "us")
        self.assertAlmostEqual(rate, 0.0140)

    def test_livekit_us_rate(self) -> None:
        rate = self.est.estimate_minute_cost("livekit", "us")
        self.assertAlmostEqual(rate, 0.001)

    def test_sip_local_rate_is_zero(self) -> None:
        rate_us = self.est.estimate_minute_cost("sip_local", "us")
        rate_ru = self.est.estimate_minute_cost("sip_local", "ru")
        rate_default = self.est.estimate_minute_cost("sip_local", "unknown")
        self.assertEqual(rate_us, 0.0)
        self.assertEqual(rate_ru, 0.0)
        self.assertEqual(rate_default, 0.0)

    def test_case_insensitive_provider(self) -> None:
        rate_lower = self.est.estimate_minute_cost("telnyx", "ru")
        rate_upper = self.est.estimate_minute_cost("TELNYX", "RU")
        self.assertAlmostEqual(rate_lower, rate_upper)

    def test_unknown_country_falls_back_to_default(self) -> None:
        rate = self.est.estimate_minute_cost("telnyx", "zz")
        from backend.call_cost_estimator import _TELNYX_RATES
        self.assertAlmostEqual(rate, _TELNYX_RATES["default"])

    def test_unknown_provider_falls_back_to_telnyx(self) -> None:
        rate = self.est.estimate_minute_cost("vonage", "us")
        from backend.call_cost_estimator import _TELNYX_RATES
        self.assertAlmostEqual(rate, _TELNYX_RATES["us"])

    def test_ru_more_expensive_than_us_telnyx(self) -> None:
        rate_us = self.est.estimate_minute_cost("telnyx", "us")
        rate_ru = self.est.estimate_minute_cost("telnyx", "ru")
        self.assertGreater(rate_ru, rate_us)

    def test_twilio_more_expensive_than_livekit(self) -> None:
        rate_twilio = self.est.estimate_minute_cost("twilio", "us")
        rate_lk = self.est.estimate_minute_cost("livekit", "us")
        self.assertGreater(rate_twilio, rate_lk)


class TestShouldWarnUser(unittest.TestCase):
    def setUp(self) -> None:
        self.est = CallCostEstimator()

    def test_no_warn_short_call(self) -> None:
        # 1 минута × $1/ч = $0.017 (ниже $5)
        self.assertFalse(self.est.should_warn_user(60, 1.0))

    def test_warn_long_expensive_call(self) -> None:
        # 6 часов × $2/ч = $12 → warn
        self.assertTrue(self.est.should_warn_user(6 * 3600, 2.0))

    def test_warn_threshold_exact_boundary(self) -> None:
        # ровно на пороге: NOT warn (> не >=)
        hourly = 10.0
        duration = (WARN_THRESHOLD_USD / hourly) * 3600
        self.assertFalse(self.est.should_warn_user(duration, hourly))

    def test_warn_just_over_threshold(self) -> None:
        hourly = 10.0
        duration = (WARN_THRESHOLD_USD / hourly) * 3600 + 1
        self.assertTrue(self.est.should_warn_user(duration, hourly))

    def test_zero_duration_no_warn(self) -> None:
        self.assertFalse(self.est.should_warn_user(0, 10.0))

    def test_zero_rate_no_warn(self) -> None:
        self.assertFalse(self.est.should_warn_user(10000, 0))


class TestRunningCost(unittest.TestCase):
    def setUp(self) -> None:
        self.est = CallCostEstimator()

    def test_running_cost_zero_duration(self) -> None:
        cost = self.est.running_cost_usd(0, "telnyx", "us")
        self.assertAlmostEqual(cost, 0.0)

    def test_running_cost_one_minute(self) -> None:
        cost = self.est.running_cost_usd(60, "telnyx", "us")
        self.assertAlmostEqual(cost, 0.004)

    def test_running_cost_proportional(self) -> None:
        cost_1min = self.est.running_cost_usd(60, "twilio", "gb")
        cost_2min = self.est.running_cost_usd(120, "twilio", "gb")
        self.assertAlmostEqual(cost_2min, cost_1min * 2)


class TestHandleEstimateCost(unittest.TestCase):
    def setUp(self) -> None:
        self.est = CallCostEstimator()

    def test_handler_returns_ok(self) -> None:
        result = self.est.handle_estimate_cost(
            {"provider": "telnyx", "destination": "us", "duration_sec": 0}
        )
        self.assertTrue(result["ok"])
        self.assertIn("result", result)

    def test_handler_includes_all_fields(self) -> None:
        result = self.est.handle_estimate_cost(
            {"provider": "twilio", "destination": "ru", "duration_sec": 120}
        )
        r = result["result"]
        for key in ("minute_rate_usd", "hourly_rate_usd", "running_cost_usd",
                    "warn_threshold_usd", "should_warn"):
            self.assertIn(key, r)

    def test_handler_warn_flag_active(self) -> None:
        # Twilio RU: $0.049/min = $2.94/h; 2h = $5.88 → warn
        result = self.est.handle_estimate_cost(
            {"provider": "twilio", "destination": "ru", "duration_sec": 2 * 3600}
        )
        self.assertTrue(result["result"]["should_warn"])

    def test_handler_defaults(self) -> None:
        result = self.est.handle_estimate_cost({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["provider"], "telnyx")
        self.assertEqual(result["result"]["destination"], "us")


class TestWave185Requirements(unittest.TestCase):
    """Additional tests required by Wave 185 task spec."""

    def setUp(self) -> None:
        self.est = CallCostEstimator()

    def test_estimate_per_minute_basic(self) -> None:
        """Basic per-minute cost estimation returns a positive float."""
        rate = self.est.estimate_minute_cost("telnyx", "us")
        self.assertIsInstance(rate, float)
        self.assertGreater(rate, 0.0)

    def test_includes_no_setup_fee(self) -> None:
        """Module has no setup fee — cost is purely per-minute * duration."""
        rate = self.est.estimate_minute_cost("telnyx", "us")
        cost_30s = self.est.running_cost_usd(30, "telnyx", "us")
        # Should equal exactly rate/2 (30 sec = 0.5 min)
        self.assertAlmostEqual(cost_30s, rate * 0.5, places=6)

    def test_unknown_destination_uses_default_rate(self) -> None:
        """Destination 'xx' not in table → returns provider's 'default' rate."""
        from backend.call_cost_estimator import _TELNYX_RATES
        rate = self.est.estimate_minute_cost("telnyx", "xx")
        self.assertAlmostEqual(rate, _TELNYX_RATES["default"])

    def test_zero_duration_returns_zero(self) -> None:
        """Running cost for 0 seconds is exactly 0.0."""
        cost = self.est.running_cost_usd(0.0, "twilio", "gb")
        self.assertAlmostEqual(cost, 0.0)

    def test_unicode_destination_country(self) -> None:
        """Unicode country codes with leading/trailing whitespace are handled."""
        # Cyrillic-adjacent test: non-ASCII stripped country string → fallback default
        rate_plain = self.est.estimate_minute_cost("telnyx", "  us  ")
        rate_direct = self.est.estimate_minute_cost("telnyx", "us")
        self.assertAlmostEqual(rate_plain, rate_direct)

        # Emoji/non-ASCII country → falls back to default (no KeyError)
        rate_emoji = self.est.estimate_minute_cost("telnyx", "\U0001F1FA\U0001F1F8")
        from backend.call_cost_estimator import _TELNYX_RATES
        self.assertAlmostEqual(rate_emoji, _TELNYX_RATES["default"])

    def test_concurrent_estimate(self) -> None:
        """Concurrent calls from multiple threads produce consistent results."""
        import threading
        results: list[float] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def _worker(provider: str, country: str) -> None:
            try:
                rate = self.est.estimate_minute_cost(provider, country)
                with lock:
                    results.append(rate)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=_worker, args=("telnyx", "ru")),
            threading.Thread(target=_worker, args=("twilio", "us")),
            threading.Thread(target=_worker, args=("livekit", "de")),
            threading.Thread(target=_worker, args=("telnyx", "zz")),
            threading.Thread(target=_worker, args=("twilio", "cn")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), 5)
        for r in results:
            self.assertGreater(r, 0.0)


if __name__ == "__main__":
    unittest.main()
