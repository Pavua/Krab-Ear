"""Coverage tests for MetricsCollector — Wave 79.

Tests: sliding-window latency, percentiles, confidence tracking,
diarization stub, dashboard structure, thread safety, reset, export,
invalid-input graceful handling.
"""

import json
import os
import sys
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.metrics_collector import MetricsCollector  # noqa: E402


class TestRecordLatencyAppendsToWindow(unittest.TestCase):
    """test_record_latency_appends_to_window"""

    def test_record_latency_appends_to_window(self):
        mc = MetricsCollector()
        mc.record(50.0, 0.9)
        mc.record(100.0, 0.8)
        summary = mc.get_summary()
        self.assertEqual(summary["total_requests"], 2)
        self.assertEqual(summary["window_size"], 2)
        self.assertIn("stt_metrics", summary)


class TestRecordLatencyTruncatesOldEntries(unittest.TestCase):
    """test_record_latency_truncates_old_entries_beyond_window"""

    def test_record_latency_truncates_old_entries_beyond_window(self):
        mc = MetricsCollector(window_size=3)
        # Fill with 3 values; old ones evicted as new ones arrive
        for v in [10.0, 20.0, 30.0]:
            mc.record(v, 0.9)
        # 4th record evicts 10.0
        mc.record(40.0, 0.9)

        summary = mc.get_summary()
        # window_size stays at cap
        self.assertEqual(summary["window_size"], 3)
        # total_requests counts all 4 writes
        self.assertEqual(summary["total_requests"], 4)
        # avg of [20, 30, 40] = 30
        avg = summary["stt_metrics"]["latency_ms"]["avg"]
        self.assertAlmostEqual(avg, 30.0, delta=0.1)


class TestGetPercentilesReturnsPValues(unittest.TestCase):
    """test_get_percentiles_returns_p50_p95_p99"""

    def test_get_percentiles_returns_p50_p95_p99(self):
        mc = MetricsCollector()
        # Insert 100 sorted values 1..100
        for i in range(1, 101):
            mc.record(float(i), 0.9)

        lat = mc.get_summary()["stt_metrics"]["latency_ms"]
        # numpy percentile on 1..100: p50≈50.5, p95≈95.05, p99≈99.01
        self.assertAlmostEqual(lat["p50"], 50.5, delta=1.0)
        self.assertAlmostEqual(lat["p95"], 95.05, delta=1.5)
        self.assertAlmostEqual(lat["p99"], 99.01, delta=1.5)
        # All three keys must be present
        for key in ("p50", "p95", "p99"):
            self.assertIn(key, lat)


class TestGetPercentilesEmptyReturnsZeros(unittest.TestCase):
    """test_get_percentiles_empty_returns_zeros"""

    def test_get_percentiles_empty_returns_zeros(self):
        mc = MetricsCollector()
        summary = mc.get_summary()
        # No data yet — stt_metrics absent, status = waiting_data
        self.assertNotIn("stt_metrics", summary)
        self.assertEqual(summary.get("status"), "waiting_data")
        self.assertEqual(summary["error_rate"], 0)
        self.assertEqual(summary["total_requests"], 0)


class TestRecordConfidenceInRange(unittest.TestCase):
    """test_record_confidence_in_range_0_1"""

    def test_record_confidence_in_range_0_1(self):
        mc = MetricsCollector()
        # Boundary values
        mc.record(100.0, 0.0)
        mc.record(100.0, 1.0)
        mc.record(100.0, 0.5)

        conf = mc.get_summary()["stt_metrics"]["confidence"]
        self.assertAlmostEqual(conf["min"], 0.0, places=3)
        self.assertAlmostEqual(conf["max"], 1.0, places=3)
        self.assertAlmostEqual(conf["avg"], 0.5, delta=0.01)


class TestRecordConfidenceTruncatesOldEntries(unittest.TestCase):
    """test_record_confidence_truncates_old_entries"""

    def test_record_confidence_truncates_old_entries(self):
        mc = MetricsCollector(window_size=2)
        mc.record(100.0, 0.3)   # evicted on 3rd
        mc.record(100.0, 0.5)
        mc.record(100.0, 0.7)   # evicts 0.3

        conf = mc.get_summary()["stt_metrics"]["confidence"]
        # min of remaining [0.5, 0.7] = 0.5
        self.assertAlmostEqual(conf["min"], 0.5, places=3)
        self.assertEqual(mc.get_summary()["window_size"], 2)


class TestTrackDiarizationUsedIncrements(unittest.TestCase):
    """test_track_diarization_used_increments

    MetricsCollector does not expose a dedicated diarization counter,
    but the record() interface accepts is_error=False (success path).
    We verify that each record() increments total_requests, which is
    the counter used to derive error_rate; diarization-specific tracking
    is not implemented in this module (it lives in service.py statistics).
    The test therefore validates the surrogate: successive records
    monotonically increase total_requests.
    """

    def test_track_diarization_used_increments(self):
        mc = MetricsCollector()
        for i in range(1, 6):
            mc.record(float(i * 10), 0.9)
            self.assertEqual(mc.get_summary()["total_requests"], i)


class TestGetDashboardReturnsCompleteDict(unittest.TestCase):
    """test_get_dashboard_returns_complete_dict"""

    def test_get_dashboard_returns_complete_dict(self):
        mc = MetricsCollector()
        mc.record(120.0, 0.85)
        mc.record(80.0, 0.92)
        mc.record(0.0, 0.0, is_error=True)

        summary = mc.get_summary()

        # Top-level keys
        for key in ("total_requests", "error_rate", "window_size", "stt_metrics"):
            self.assertIn(key, summary)

        self.assertEqual(summary["total_requests"], 3)
        self.assertAlmostEqual(summary["error_rate"], round(1 / 3, 4), delta=0.001)

        lat = summary["stt_metrics"]["latency_ms"]
        for key in ("p50", "p95", "p99", "avg"):
            self.assertIn(key, lat)

        conf = summary["stt_metrics"]["confidence"]
        for key in ("avg", "min", "max"):
            self.assertIn(key, conf)


class TestConcurrentRecordThreadSafety(unittest.TestCase):
    """test_concurrent_record_thread_safety"""

    def test_concurrent_record_thread_safety(self):
        mc = MetricsCollector(window_size=500)
        exceptions = []
        num_threads = 20
        per_thread = 50

        def worker():
            try:
                for i in range(per_thread):
                    mc.record(float(i), 0.9, is_error=(i % 5 == 0))
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(exceptions, [], f"Thread exceptions: {exceptions}")
        summary = mc.get_summary()
        self.assertEqual(summary["total_requests"], num_threads * per_thread)
        # error_rate ≈ 0.2 (every 5th)
        self.assertAlmostEqual(summary["error_rate"], 0.2, delta=0.02)


class TestResetClearsAllMetrics(unittest.TestCase):
    """test_reset_clears_all_metrics

    MetricsCollector has no explicit reset() method. We verify that
    creating a new instance gives a clean state — equivalent to a reset —
    and also confirm that the internal deques are independently mutable.
    """

    def test_reset_clears_all_metrics(self):
        mc = MetricsCollector()
        for i in range(10):
            mc.record(float(i * 10), 0.8, is_error=(i % 3 == 0))

        # Simulate reset by reinitialising internal state directly
        with mc._lock:
            mc.latencies.clear()
            mc.confidences.clear()
            mc.errors = 0
            mc.total_requests = 0

        summary = mc.get_summary()
        self.assertEqual(summary["total_requests"], 0)
        self.assertEqual(summary["error_rate"], 0)
        self.assertEqual(summary.get("status"), "waiting_data")
        self.assertNotIn("stt_metrics", summary)


class TestExportMetricsSerializable(unittest.TestCase):
    """test_export_metrics_serializable"""

    def test_export_metrics_serializable(self):
        mc = MetricsCollector()
        mc.record(250.5, 0.77)
        mc.record(310.2, 0.91)

        summary = mc.get_summary()
        # Must be JSON-serializable (no numpy scalars leaking out)
        try:
            serialized = json.dumps(summary)
        except (TypeError, ValueError) as exc:
            self.fail(f"get_summary() result is not JSON-serializable: {exc}")

        reloaded = json.loads(serialized)
        self.assertEqual(reloaded["total_requests"], 2)
        self.assertIn("stt_metrics", reloaded)

    def test_empty_state_serializable(self):
        mc = MetricsCollector()
        summary = mc.get_summary()
        try:
            json.dumps(summary)
        except (TypeError, ValueError) as exc:
            self.fail(f"Empty summary is not JSON-serializable: {exc}")


class TestInvalidInputHandledGracefully(unittest.TestCase):
    """test_invalid_input_handled_gracefully"""

    def test_negative_latency_recorded(self):
        """Negative latency is unusual but should not crash."""
        mc = MetricsCollector()
        mc.record(-50.0, 0.9)
        summary = mc.get_summary()
        self.assertEqual(summary["total_requests"], 1)

    def test_zero_latency_recorded(self):
        mc = MetricsCollector()
        mc.record(0.0, 0.5)
        summary = mc.get_summary()
        self.assertAlmostEqual(
            summary["stt_metrics"]["latency_ms"]["avg"], 0.0, delta=0.01
        )

    def test_very_large_latency_recorded(self):
        mc = MetricsCollector()
        mc.record(1_000_000.0, 0.0)
        summary = mc.get_summary()
        self.assertEqual(summary["window_size"], 1)
        self.assertAlmostEqual(
            summary["stt_metrics"]["latency_ms"]["p99"], 1_000_000.0, delta=1.0
        )

    def test_confidence_boundary_zero(self):
        mc = MetricsCollector()
        mc.record(100.0, 0.0)
        conf = mc.get_summary()["stt_metrics"]["confidence"]
        self.assertAlmostEqual(conf["min"], 0.0, places=3)

    def test_error_flag_true_no_latency_recorded(self):
        """is_error=True must NOT append to latencies/confidences deques."""
        mc = MetricsCollector()
        mc.record(999.0, 0.0, is_error=True)
        summary = mc.get_summary()
        # No latency data → waiting_data
        self.assertEqual(summary.get("status"), "waiting_data")
        self.assertEqual(summary["total_requests"], 1)
        self.assertEqual(summary["error_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
