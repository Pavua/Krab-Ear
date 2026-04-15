"""Тесты для эндпойнта /metrics/prometheus в REST-сервере."""

import sys
import os
import re
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy modules before any rest_server import
# (следуем паттерну test_rest_auth.py)
# ---------------------------------------------------------------------------

# core.engine stub
_engine_mod = types.ModuleType("core.engine")


class _FakeEngine:
    quality_profile = "balanced"

    def normalize_audio(self, *a, **kw):
        pass


_engine_mod.AudioEngine = _FakeEngine
sys.modules.setdefault("core.engine", _engine_mod)

# backend.event_bus stub
_event_bus_mod = types.ModuleType("backend.event_bus")
_event_bus_mod.bus = MagicMock()
_event_bus_mod.sse_stream = MagicMock(return_value=iter([]))
sys.modules.setdefault("backend.event_bus", _event_bus_mod)

# backend.service stub
_service_mod = types.ModuleType("backend.service")


class _FakeBackendService:
    @staticmethod
    def _build_readiness_report_static():
        return {"overall_ready": True, "components": {}}


_service_mod.BackendService = _FakeBackendService
sys.modules.setdefault("backend.service", _service_mod)

# backend.state_store stub
_state_store_mod = types.ModuleType("backend.state_store")


class _FakeStateStore:
    def __init__(self, *a, **kw):
        pass

    def is_idempotent(self, *a, **kw):
        return False

    def load_vocabulary(self):
        return []

    def save_vocabulary(self, *a, **kw):
        pass

    def add_history_item(self, *a, **kw):
        m = MagicMock()
        m.id = "fake-history-id"
        return m


_state_store_mod.StateStore = _FakeStateStore
sys.modules.setdefault("backend.state_store", _state_store_mod)

# backend.transcriber stub
_transcriber_mod = types.ModuleType("backend.transcriber")


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_transcriber_mod.Transcriber = _FakeTranscriber
sys.modules.setdefault("backend.transcriber", _transcriber_mod)

# backend.metrics_collector — use the real implementation for accurate tests
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.metrics_collector import MetricsCollector  # noqa: E402

# We'll inject a fresh MetricsCollector instance per test, but we need to
# provide a module-level stub so rest_server imports without error.
_metrics_mod = types.ModuleType("backend.metrics_collector")
_metrics_mod.MetricsCollector = MetricsCollector
_metrics_mod.metrics = MetricsCollector()
sys.modules.setdefault("backend.metrics_collector", _metrics_mod)

# flask_smorest stubs (if not installed)
try:
    import flask_smorest  # noqa: F401
except ImportError:
    _smorest_mod = types.ModuleType("flask_smorest")

    class _FakeApi:
        def __init__(self, app):
            pass

        def register_blueprint(self, blp):
            pass

    class _FakeBlueprint:
        def __init__(self, *a, **kw):
            pass

        def route(self, *a, **kw):
            def decorator(f):
                return f
            return decorator

        def response(self, *a, **kw):
            def decorator(f):
                return f
            return decorator

        def alt_response(self, *a, **kw):
            def decorator(f):
                return f
            return decorator

        def arguments(self, *a, **kw):
            def decorator(f):
                return f
            return decorator

    _smorest_mod.Api = _FakeApi
    _smorest_mod.Blueprint = _FakeBlueprint
    _smorest_mod.abort = MagicMock()
    sys.modules["flask_smorest"] = _smorest_mod

# marshmallow stubs
try:
    import marshmallow  # noqa: F401
except ImportError:
    _ma_mod = types.ModuleType("marshmallow")

    class _Schema:
        pass

    class _Fields:
        String = MagicMock(return_value=None)
        Boolean = MagicMock(return_value=None)
        Float = MagicMock(return_value=None)
        Integer = MagicMock(return_value=None)
        List = MagicMock(return_value=None)
        Dict = MagicMock(return_value=None)

    _ma_mod.Schema = _Schema
    _ma_mod.fields = _Fields()
    _ma_mod.validate = MagicMock()
    sys.modules["marshmallow"] = _ma_mod

# werkzeug stub (guard only — flask usually installs it)
try:
    from werkzeug.utils import secure_filename  # noqa: F401
except ImportError:
    _wz_mod = types.ModuleType("werkzeug.utils")
    _wz_mod.secure_filename = lambda name: name
    sys.modules["werkzeug.utils"] = _wz_mod

# ---------------------------------------------------------------------------
# Import the app under test
# ---------------------------------------------------------------------------

with patch("pathlib.Path.mkdir"):
    from backend.rest_server import app, _build_prometheus_text  # noqa: E402
import backend.rest_server as _rest_server  # noqa: E402


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class PrometheusEndpointBasicTests(unittest.TestCase):
    """Базовые тесты эндпойнта /metrics/prometheus."""

    def setUp(self):
        # Inject a fresh MetricsCollector so tests are independent
        self._orig_metrics = _rest_server.metrics
        _rest_server.metrics = MetricsCollector()
        self.client = app.test_client()
        app.config["TESTING"] = True

    def tearDown(self):
        _rest_server.metrics = self._orig_metrics

    # 1. Status code and Content-Type
    def test_status_200_and_content_type(self):
        resp = self.client.get("/metrics/prometheus")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.content_type)

    # 2. All required metric names are present
    def test_contains_required_metric_names(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        for name in [
            "krab_ear_transcriptions_total",
            "krab_ear_errors_total",
            "krab_ear_confidence_avg",
            "krab_ear_uptime_seconds",
            "krab_ear_stt_latency_seconds_bucket",
        ]:
            self.assertIn(name, body, f"Метрика не найдена: {name}")

    # 3. HELP and TYPE lines exist
    def test_help_and_type_lines_present(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        self.assertIn("# HELP krab_ear_transcriptions_total", body)
        self.assertIn("# TYPE krab_ear_transcriptions_total counter", body)
        self.assertIn("# HELP krab_ear_stt_latency_seconds", body)
        self.assertIn("# TYPE krab_ear_stt_latency_seconds histogram", body)
        self.assertIn("# TYPE krab_ear_confidence_avg gauge", body)

    # 4. Transcription counter updates correctly
    def test_transcription_count_reflects_records(self):
        for _ in range(5):
            _rest_server.metrics.record(latency_ms=300, confidence=0.9)
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        self.assertIn("krab_ear_transcriptions_total 5", body)

    # 5. Error counter reflects is_error=True records
    def test_error_count_reflects_error_records(self):
        _rest_server.metrics.record(latency_ms=200, confidence=0.8)
        _rest_server.metrics.record(latency_ms=0, confidence=0, is_error=True)
        _rest_server.metrics.record(latency_ms=0, confidence=0, is_error=True)
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        # 2 errors out of 3 total → error_rate≈0.667 → errors=round(0.667*3)=2
        self.assertIn("krab_ear_errors_total 2", body)

    # 6. With no data, all counts are zero
    def test_empty_metrics_returns_zero_counts(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        self.assertIn("krab_ear_transcriptions_total 0", body)
        self.assertIn("krab_ear_errors_total 0", body)
        self.assertIn("krab_ear_confidence_avg 0.0000", body)

    # 7. Confidence gauge reflects real average
    def test_confidence_avg_gauge_value(self):
        _rest_server.metrics.record(latency_ms=100, confidence=0.8)
        _rest_server.metrics.record(latency_ms=100, confidence=0.6)
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        # avg = 0.70
        self.assertIn("krab_ear_confidence_avg 0.7", body)

    # 8. Histogram has +Inf bucket, _sum and _count lines
    def test_histogram_has_inf_bucket_and_sum_count(self):
        _rest_server.metrics.record(latency_ms=500, confidence=0.9)
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        self.assertIn('krab_ear_stt_latency_seconds_bucket{le="+Inf"}', body)
        self.assertIn("krab_ear_stt_latency_seconds_sum", body)
        self.assertIn("krab_ear_stt_latency_seconds_count", body)

    # 9. uptime_seconds > 0
    def test_uptime_seconds_positive(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode()
        match = re.search(r"krab_ear_uptime_seconds ([0-9.]+)", body)
        self.assertIsNotNone(match, "krab_ear_uptime_seconds не найден")
        self.assertGreater(float(match.group(1)), 0)


class PrometheusTextFormatUnitTests(unittest.TestCase):
    """Юнит-тесты функции _build_prometheus_text напрямую."""

    def _make_summary(self, total=10, error_rate=0.1, window=9,
                      p50=300.0, p95=900.0, p99=1500.0, avg=400.0,
                      conf_avg=0.85):
        return {
            "total_requests": total,
            "error_rate": error_rate,
            "window_size": window,
            "stt_metrics": {
                "latency_ms": {"p50": p50, "p95": p95, "p99": p99, "avg": avg},
                "confidence": {"avg": conf_avg, "min": 0.7, "max": 0.95},
            },
        }

    # 10. Direct call: counter values
    def test_direct_counter_values(self):
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_transcriptions_total 10", text)
        # error_rate=0.1, total=10 → errors=round(0.1*10)=1
        self.assertIn("krab_ear_errors_total 1", text)

    # 11. Direct call: confidence gauge precision
    def test_direct_confidence_precision(self):
        text = _build_prometheus_text(self._make_summary(conf_avg=0.85))
        self.assertIn("krab_ear_confidence_avg 0.8500", text)

    # 12. Direct call: histogram sum calculation
    def test_direct_histogram_sum(self):
        # sum = avg_ms/1000 * window = 400/1000 * 9 = 3.6
        text = _build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_stt_latency_seconds_sum 3.600000", text)
        self.assertIn("krab_ear_stt_latency_seconds_count 9", text)

    # 13. All standard bucket boundaries present
    def test_standard_bucket_boundaries_present(self):
        text = _build_prometheus_text(self._make_summary())
        for le in ["0.1", "0.25", "0.5", "1.0", "2.0", "5.0", "10.0"]:
            self.assertIn(f'le="{le}"', text, f"Bucket le={le} отсутствует")

    # 14. Empty summary (waiting_data) produces sane zero output
    def test_empty_summary_no_crash(self):
        text = _build_prometheus_text({"total_requests": 0, "error_rate": 0.0})
        self.assertIn("krab_ear_transcriptions_total 0", text)
        self.assertIn("krab_ear_errors_total 0", text)

    # 15. Output ends with newline (Prometheus format requirement)
    def test_output_ends_with_newline(self):
        text = _build_prometheus_text(self._make_summary())
        self.assertTrue(text.endswith("\n"), "Текст должен заканчиваться символом новой строки")


if __name__ == "__main__":
    unittest.main()
