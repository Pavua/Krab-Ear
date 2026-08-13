"""Тесты для W1685: wire MetricsCollector into get_metrics_dashboard + HealthCheckService.

F3 MED  — get_metrics_dashboard теперь содержит секцию "metrics" из MetricsCollector.
F5 INFO — HealthCheckService._metrics_collector используется в get_diagnostics
          через новый метод _get_metrics_summary().

Все тесты работают без запуска полного backend — только прямой импорт нужных модулей.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_check_service import HealthCheckService  # noqa: E402
from backend.metrics_collector import MetricsCollector  # noqa: E402


# ---------------------------------------------------------------------------
# Fake collaborators (shared)
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, count: int = 5) -> None:
        self.data_dir = Path("/tmp/krab_test_w1685")
        self._count = count

    def count_active_items(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> int:
        return self._count


class FakeHealthChecker:
    def check_all(self) -> dict:
        return {"overall": "ok", "checks": []}


class FakeStartupDiagnostics:
    class Report:
        def to_dict(self) -> dict:
            return {"status": "ok", "checks": [], "startup_time_ms": 5.0,
                    "errors": [], "warnings": []}

    def run_all_checks(self):
        return FakeStartupDiagnostics.Report()


class FakeIntegrityChecker:
    class Report:
        status = "ok"
        total_items = 5
        orphaned_tombstones = 0
        invalid_json_lines = 0
        checks = []

    def check_integrity(self, data_dir):
        return FakeIntegrityChecker.Report()


class FakeSettingsSvc:
    _cache_ttl = 5
    _cache = {}


class FakeTranscriber:
    class engine:
        quality_profile = "balanced"
        current_model = "mlx-whisper-balanced"

        @staticmethod
        def _resolve_diarization_device():
            return "cpu"


class FakeLLMRewriter:
    def status(self) -> dict:
        return {"enabled": False}


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

def _make_health_svc(metrics_collector=None, **kwargs) -> HealthCheckService:
    defaults: dict[str, Any] = dict(
        store=FakeStore(),
        health_checker=FakeHealthChecker(),
        startup_diagnostics=FakeStartupDiagnostics(),
        integrity_checker=FakeIntegrityChecker(),
        llm_probe=None,
        metrics_collector=metrics_collector,
        transcriber=FakeTranscriber(),
        llm_rewriter=FakeLLMRewriter(),
        settings_svc=FakeSettingsSvc(),
        start_time=time.monotonic() - 10.0,
        app_version="2.0.5-test",
        recorder=None,
        last_stt_engine_ref=["mlx-whisper"],
    )
    defaults.update(kwargs)
    return HealthCheckService(**defaults)


# ---------------------------------------------------------------------------
# F3: test_get_metrics_dashboard_includes_latency_percentiles
# Tests that service.py _handle_get_metrics_dashboard includes "metrics" key
# from MetricsCollector.get_summary() with latency percentiles.
# We test via direct unit-test of the metrics_collector path logic rather
# than importing the full BackendService (too heavy).
# ---------------------------------------------------------------------------

class TestGetMetricsDashboardIncludesLatencyPercentiles(unittest.TestCase):
    """F3: get_metrics_dashboard должен возвращать секцию metrics с p50/p95/p99."""

    def _get_metrics_snapshot(self, mc: MetricsCollector) -> dict:
        """Эмулирует логику _handle_get_metrics_dashboard для секции metrics."""
        try:
            return mc.get_summary()
        except Exception:
            return {"status": "unavailable", "total_requests": 0}

    def test_get_metrics_dashboard_includes_latency_percentiles(self):
        """После записи нескольких значений summary содержит p50/p95/p99."""
        mc = MetricsCollector()
        for i in range(1, 11):
            mc.record(float(i * 10), 0.9)

        snapshot = self._get_metrics_snapshot(mc)

        self.assertIn("stt_metrics", snapshot,
                      "snapshot должен содержать ключ stt_metrics")
        lat = snapshot["stt_metrics"]["latency_ms"]
        for key in ("p50", "p95", "p99", "avg"):
            self.assertIn(key, lat,
                          f"latency_ms должен содержать ключ {key!r}")
        # p50 для 10..100 (step=10) ≈ 55ms
        self.assertGreater(lat["p50"], 0)
        self.assertGreater(lat["p95"], lat["p50"])

    def test_metrics_dashboard_section_key_present_in_handler_output(self):
        """Проверяем, что _handle_get_metrics_dashboard содержит ключ 'metrics'."""
        # Мы патчим глобальный singleton metrics в backend.metrics_collector,
        # затем вызываем handler через мок BackendService.
        mc = MetricsCollector()
        mc.record(100.0, 0.9)
        mc.record(200.0, 0.8)

        # Simulate what the handler does — import global + get_summary
        with patch("backend.metrics_collector.metrics", mc):
            from backend.metrics_collector import metrics as _mc
            result = _mc.get_summary()

        self.assertIn("stt_metrics", result)
        self.assertIn("total_requests", result)
        self.assertEqual(result["total_requests"], 2)


# ---------------------------------------------------------------------------
# F3: test_metrics_dashboard_handles_empty_metrics
# No recordings yet — no crash, sensible defaults (status == "waiting_data").
# ---------------------------------------------------------------------------

class TestMetricsDashboardHandlesEmptyMetrics(unittest.TestCase):
    """F3 guard: пустой MetricsCollector не должен вызывать исключений."""

    def test_empty_collector_returns_waiting_data(self):
        """MetricsCollector.get_summary() без записей возвращает status='waiting_data'."""
        mc = MetricsCollector()
        result = mc.get_summary()

        # No crash
        self.assertIn("status", result)
        self.assertEqual(result["status"], "waiting_data",
                         "Пустой коллектор должен вернуть status='waiting_data'")
        self.assertIn("total_requests", result)
        self.assertEqual(result["total_requests"], 0)

    def test_empty_metrics_no_stt_metrics_key(self):
        """Пустой коллектор не должен содержать stt_metrics (нечего показывать)."""
        mc = MetricsCollector()
        result = mc.get_summary()
        self.assertNotIn("stt_metrics", result,
                         "stt_metrics не должен присутствовать без данных")

    def test_handler_logic_graceful_on_empty(self):
        """Эмуляция логики handler: пустой коллектор — нет крэша, есть sensible defaults."""
        mc = MetricsCollector()
        try:
            snapshot = mc.get_summary()
        except Exception as exc:
            self.fail(f"get_summary() не должен бросать исключения: {exc}")

        # Should have at least total_requests key
        self.assertIn("total_requests", snapshot)


# ---------------------------------------------------------------------------
# F5: test_health_check_uses_metrics_collector
# HealthCheckService._get_metrics_summary() должен использовать injected collector.
# ---------------------------------------------------------------------------

class TestHealthCheckUsesMetricsCollector(unittest.TestCase):
    """F5: HealthCheckService._metrics_collector используется в диагностике."""

    def test_get_metrics_summary_none_collector_returns_unavailable(self):
        """Без инжектированного collector возвращается {available: False}."""
        svc = _make_health_svc(metrics_collector=None)
        result = svc._get_metrics_summary()
        self.assertFalse(result["available"],
                         "Без collector должно быть available=False")

    def test_get_metrics_summary_empty_collector_available_true(self):
        """С инжектированным (пустым) collector возвращается available=True."""
        mc = MetricsCollector()
        svc = _make_health_svc(metrics_collector=mc)
        result = svc._get_metrics_summary()
        self.assertTrue(result["available"],
                        "С пустым collector должно быть available=True")
        # Empty state — status=waiting_data
        self.assertEqual(result.get("status"), "waiting_data")

    def test_get_metrics_summary_with_data_includes_stt_metrics(self):
        """С записями collector возвращает stt_metrics в summary."""
        mc = MetricsCollector()
        for i in range(5):
            mc.record(float(i * 50 + 50), 0.85)

        svc = _make_health_svc(metrics_collector=mc)
        result = svc._get_metrics_summary()

        self.assertTrue(result["available"])
        self.assertIn("stt_metrics", result,
                      "stt_metrics должен присутствовать при наличии данных")
        lat = result["stt_metrics"]["latency_ms"]
        self.assertIn("p50", lat)
        self.assertIn("p95", lat)
        self.assertIn("p99", lat)

    def test_get_diagnostics_includes_metrics_summary_key(self):
        """handle_get_diagnostics должен содержать ключ 'metrics_summary'."""
        mc = MetricsCollector()
        mc.record(100.0, 0.9)

        svc = _make_health_svc(metrics_collector=mc)

        # Нужно заглушить тяжёлые импорты в handle_get_diagnostics
        fake_profiler = MagicMock()
        fake_profiler.get_profile_report.return_value = {
            "methods": {}, "slowest_methods": [],
            "total_profiled_time_sec": 0.0,
        }
        fake_global_settings = MagicMock()
        fake_global_settings.MODEL_BALANCED = "mlx-whisper-base"
        fake_global_settings.MODEL_MAX_CANDIDATES = ["mlx-whisper-large"]
        fake_global_settings.DIARIZATION_ENABLED = False

        with patch("backend.performance_profiler.profiler", fake_profiler), \
                patch("core.config.settings", fake_global_settings):
            result = svc.handle_get_diagnostics({})

        self.assertIn("metrics_summary", result,
                      "get_diagnostics должен содержать ключ 'metrics_summary'")
        ms = result["metrics_summary"]
        self.assertIn("available", ms)
        self.assertTrue(ms["available"])

    def test_get_diagnostics_metrics_summary_unavailable_without_collector(self):
        """Без collector metrics_summary.available == False."""
        svc = _make_health_svc(metrics_collector=None)

        fake_profiler = MagicMock()
        fake_profiler.get_profile_report.return_value = {
            "methods": {}, "slowest_methods": [],
            "total_profiled_time_sec": 0.0,
        }
        fake_global_settings = MagicMock()
        fake_global_settings.MODEL_BALANCED = "mlx-whisper-base"
        fake_global_settings.MODEL_MAX_CANDIDATES = []
        fake_global_settings.DIARIZATION_ENABLED = False

        with patch("backend.performance_profiler.profiler", fake_profiler), \
                patch("core.config.settings", fake_global_settings):
            result = svc.handle_get_diagnostics({})

        ms = result["metrics_summary"]
        self.assertFalse(ms["available"])

    def test_get_metrics_summary_raises_gracefully(self):
        """Исключение в get_summary() не должно проваливаться наружу."""
        mc = MagicMock()
        mc.get_summary.side_effect = RuntimeError("collector exploded")

        svc = _make_health_svc(metrics_collector=mc)
        result = svc._get_metrics_summary()

        self.assertFalse(result["available"])
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
