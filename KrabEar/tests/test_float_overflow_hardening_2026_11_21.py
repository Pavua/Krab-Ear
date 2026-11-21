"""KE05C-21, KE05C-24, KE05A-19, KE05A-15: огромные числа не роняют обработчики.

``math.isfinite(10**500)`` и ``float(10**500)`` бросают OverflowError (не ValueError),
``int(float("inf"))`` — тоже. Тот же класс, что RF04-09 в auto_deduplication.
Значения приходят из IPC/настроек, где JSON допускает ``Infinity`` и ``1e400``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.live_subs_service import LiveSubsService  # noqa: E402
from backend.llm_probe import LLMHttpProbe  # noqa: E402
from backend.metadata_enricher import _coerce_finite_float  # noqa: E402
from backend.metrics_collector import MetricsCollector  # noqa: E402

_HUGE = 10**500


class MetricsCollectorHugeValues(unittest.TestCase):
    def test_huge_latency_is_dropped_not_raised(self):
        mc = MetricsCollector(window_size=10)
        mc.record(_HUGE, 0.5)
        self.assertEqual(len(mc.latencies), 0)

    def test_huge_confidence_is_dropped_not_raised(self):
        mc = MetricsCollector(window_size=10)
        mc.record(10.0, _HUGE)
        self.assertEqual(len(mc.confidences), 0)

    def test_normal_sample_still_recorded(self):
        mc = MetricsCollector(window_size=10)
        mc.record(10.0, 0.5)
        self.assertEqual(list(mc.latencies), [10.0])


class CoerceFiniteFloatHuge(unittest.TestCase):
    def test_huge_int_returns_default(self):
        self.assertEqual(_coerce_finite_float(_HUGE), 0.0)
        self.assertEqual(_coerce_finite_float(_HUGE, 5.0), 5.0)

    def test_huge_numeric_string_returns_default(self):
        self.assertEqual(_coerce_finite_float("1" + "0" * 500, 2.0), 2.0)

    def test_normal_values_unchanged(self):
        self.assertEqual(_coerce_finite_float("12.5"), 12.5)
        self.assertEqual(_coerce_finite_float(3), 3.0)


class SampleRateInfinity(unittest.TestCase):
    def test_infinity_falls_back_to_default(self):
        self.assertEqual(LiveSubsService._sanitize_sample_rate(float("inf")), 16000)
        self.assertEqual(LiveSubsService._sanitize_sample_rate(float("-inf")), 16000)

    def test_json_1e400_falls_back_to_default(self):
        import json

        self.assertEqual(LiveSubsService._sanitize_sample_rate(json.loads("1e400")), 16000)

    def test_valid_rate_unchanged(self):
        self.assertEqual(LiveSubsService._sanitize_sample_rate(48000), 48000)


def _probe(value, base=30.0) -> LLMHttpProbe:
    return LLMHttpProbe(
        rewriter=MagicMock(),
        error_bus=MagicMock(),
        event_bus=MagicMock(),
        settings_provider=lambda: {"llm_probe_interval_sec": value},
        base_interval_sec=base,
    )


class ProbeIntervalBounds(unittest.TestCase):
    def test_infinity_falls_back_to_base(self):
        self.assertEqual(_probe(float("inf"))._effective_interval(), 30.0)

    def test_nan_falls_back_to_base(self):
        self.assertEqual(_probe(float("nan"))._effective_interval(), 30.0)

    def test_tiny_interval_is_floored(self):
        self.assertGreaterEqual(_probe(1e-9)._effective_interval(), 1.0)

    def test_huge_interval_is_capped(self):
        self.assertLessEqual(_probe(1e9)._effective_interval(), 300.0)

    def test_in_range_value_unchanged(self):
        self.assertEqual(_probe(99.0)._effective_interval(), 99.0)


if __name__ == "__main__":
    unittest.main()
