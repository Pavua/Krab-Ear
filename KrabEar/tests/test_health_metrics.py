"""Тесты для HealthMetrics (RSS, uptime, active_requests)."""

import unittest
import time
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_metrics import HealthMetrics


class HealthMetricsTestCase(unittest.TestCase):
    def test_rss_mb_returns_positive_int(self):
        metrics = HealthMetrics()
        rss = metrics.rss_mb()
        self.assertIsInstance(rss, (int, float))
        self.assertGreater(rss, 0)
        # Sanity: процесс не может занимать больше 100 GB
        self.assertLess(rss, 100_000)

    def test_uptime_sec_increases(self):
        metrics = HealthMetrics()
        first = metrics.uptime_sec()
        time.sleep(0.05)
        second = metrics.uptime_sec()
        self.assertGreater(second, first)

    def test_active_requests_default_zero(self):
        metrics = HealthMetrics()
        self.assertEqual(metrics.active_requests(), 0)

    def test_active_requests_increments_and_decrements(self):
        metrics = HealthMetrics()
        with metrics.track_request():
            self.assertEqual(metrics.active_requests(), 1)
            with metrics.track_request():
                self.assertEqual(metrics.active_requests(), 2)
            self.assertEqual(metrics.active_requests(), 1)
        self.assertEqual(metrics.active_requests(), 0)

    def test_active_requests_decrements_on_exception(self):
        metrics = HealthMetrics()
        try:
            with metrics.track_request():
                self.assertEqual(metrics.active_requests(), 1)
                raise RuntimeError("simulated")
        except RuntimeError:
            pass
        self.assertEqual(metrics.active_requests(), 0)


if __name__ == "__main__":
    unittest.main()
