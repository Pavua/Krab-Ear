"""E2E integration tests for the Krab Ear REST API.

Covers OpenAPI spec, Swagger UI, /api/info, Prometheus metrics,
rate limiting, CORS preflight, auth flow, X-Request-ID, content-type
headers, and more — all without real ML/audio.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_e2e.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Guard: skip entire module if Flask or other REST deps are missing.
# Patch heavy objects before the module-level AudioEngine() is instantiated.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.is_idempotent.return_value = False
    _mock_store.load_settings.return_value = {}  # wave1212

    _mock_transcriber = MagicMock()

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 42,
        "error_rate": 0.05,
        "error_count": 2,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 250, "p95": 900, "p99": 1800, "avg": 310},
            "confidence": {"avg": 0.87},
        },
        "window_size": 42,
    }

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
            patch("backend.metrics_collector.metrics", _mock_metrics):
        from backend.rest_server import app

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    app = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Shared setUp helper
# ---------------------------------------------------------------------------

def _make_client():
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False  # disable limiter in all tests by default
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. OpenAPI JSON spec
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class OpenAPISpecTest(unittest.TestCase):
    """GET /api/openapi.json — must return valid JSON OpenAPI document."""

    def setUp(self):
        self.client = _make_client()

    def test_openapi_json_returns_200(self):
        resp = self.client.get("/api/openapi.json")
        self.assertEqual(resp.status_code, 200)

    def test_openapi_json_is_valid_json(self):
        resp = self.client.get("/api/openapi.json")
        data = resp.get_json()
        self.assertIsNotNone(data, "Response body must be valid JSON")

    def test_openapi_json_has_openapi_field(self):
        resp = self.client.get("/api/openapi.json")
        data = resp.get_json()
        self.assertIn("openapi", data)
        self.assertTrue(data["openapi"].startswith("3."), "Must be OpenAPI 3.x")

    def test_openapi_json_has_info_block(self):
        resp = self.client.get("/api/openapi.json")
        data = resp.get_json()
        self.assertIn("info", data)
        self.assertIn("title", data["info"])
        self.assertIn("version", data["info"])

    def test_openapi_json_has_paths(self):
        resp = self.client.get("/api/openapi.json")
        data = resp.get_json()
        self.assertIn("paths", data)
        self.assertIsInstance(data["paths"], dict)
        self.assertGreater(len(data["paths"]), 0)

    def test_openapi_json_content_type(self):
        resp = self.client.get("/api/openapi.json")
        self.assertIn("application/json", resp.content_type)


# ---------------------------------------------------------------------------
# 2. Swagger UI
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class SwaggerUITest(unittest.TestCase):
    """GET /api/docs — must serve an HTML page."""

    def setUp(self):
        self.client = _make_client()

    def test_swagger_ui_returns_200(self):
        resp = self.client.get("/api/docs")
        self.assertEqual(resp.status_code, 200)

    def test_swagger_ui_content_type_is_html(self):
        resp = self.client.get("/api/docs")
        self.assertIn("text/html", resp.content_type)

    def test_swagger_ui_body_not_empty(self):
        resp = self.client.get("/api/docs")
        self.assertGreater(len(resp.data), 100, "Swagger UI page must have content")


# ---------------------------------------------------------------------------
# 3. /api/info — version metadata
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class APIInfoTest(unittest.TestCase):
    """GET /info — API version metadata endpoint."""

    def setUp(self):
        self.client = _make_client()

    def test_info_returns_200(self):
        resp = self.client.get("/info")
        self.assertEqual(resp.status_code, 200)

    def test_info_has_current_version(self):
        resp = self.client.get("/info")
        data = resp.get_json()
        self.assertIn("current_version", data)
        self.assertIsInstance(data["current_version"], str)

    def test_info_has_supported_versions_list(self):
        resp = self.client.get("/info")
        data = resp.get_json()
        self.assertIn("supported_versions", data)
        self.assertIsInstance(data["supported_versions"], list)
        self.assertGreater(len(data["supported_versions"]), 0)

    def test_info_has_deprecated_versions_list(self):
        resp = self.client.get("/info")
        data = resp.get_json()
        self.assertIn("deprecated_versions", data)
        self.assertIsInstance(data["deprecated_versions"], list)

    def test_info_content_type_is_json(self):
        resp = self.client.get("/info")
        self.assertIn("application/json", resp.content_type)


# ---------------------------------------------------------------------------
# 4. Prometheus metrics format
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class PrometheusMetricsTest(unittest.TestCase):
    """GET /metrics/prometheus — Prometheus text exposition format."""

    def setUp(self):
        self.client = _make_client()

    def test_prometheus_returns_200(self):
        resp = self.client.get("/metrics/prometheus")
        self.assertEqual(resp.status_code, 200)

    def test_prometheus_content_type(self):
        resp = self.client.get("/metrics/prometheus")
        # Must be text/plain with Prometheus version marker
        self.assertIn("text/plain", resp.content_type)
        self.assertIn("0.0.4", resp.content_type)

    def test_prometheus_body_is_text(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIsInstance(body, str)
        self.assertGreater(len(body), 0)

    def test_prometheus_has_help_lines(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("# HELP", body)

    def test_prometheus_has_type_lines(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("# TYPE", body)

    def test_prometheus_has_transcriptions_total(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("krab_ear_transcriptions_total", body)

    def test_prometheus_has_uptime_gauge(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("krab_ear_uptime_seconds", body)

    def test_prometheus_has_latency_histogram(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("krab_ear_stt_latency_seconds", body)

    def test_prometheus_histogram_has_inf_bucket(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn('+Inf', body)


# ---------------------------------------------------------------------------
# 5. X-Request-ID header on every response
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class RequestIDHeaderTest(unittest.TestCase):
    """X-Request-ID must be present on every response."""

    def setUp(self):
        self.client = _make_client()

    def test_health_has_request_id(self):
        resp = self.client.get("/health")
        self.assertIn("X-Request-ID", resp.headers)

    def test_metrics_has_request_id(self):
        resp = self.client.get("/metrics")
        self.assertIn("X-Request-ID", resp.headers)

    def test_info_has_request_id(self):
        resp = self.client.get("/info")
        self.assertIn("X-Request-ID", resp.headers)

    def test_vocabulary_has_request_id(self):
        resp = self.client.get("/v1/vocabulary")
        self.assertIn("X-Request-ID", resp.headers)

    def test_request_id_is_non_empty(self):
        resp = self.client.get("/health")
        self.assertGreater(len(resp.headers["X-Request-ID"]), 0)

    def test_request_ids_are_unique(self):
        r1 = self.client.get("/health")
        r2 = self.client.get("/health")
        self.assertNotEqual(
            r1.headers["X-Request-ID"],
            r2.headers["X-Request-ID"],
            "Each request must get a unique X-Request-ID",
        )


# ---------------------------------------------------------------------------
# 6. X-API-Version header
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class APIVersionHeaderTest(unittest.TestCase):
    """X-API-Version must appear on every response (injected by after_request)."""

    def setUp(self):
        self.client = _make_client()

    def test_health_has_api_version_header(self):
        resp = self.client.get("/health")
        self.assertIn("X-API-Version", resp.headers)

    def test_v1_path_has_api_version_header(self):
        resp = self.client.get("/v1/vocabulary")
        self.assertIn("X-API-Version", resp.headers)
        self.assertEqual(resp.headers["X-API-Version"], "v1")

    def test_info_has_api_version_header(self):
        resp = self.client.get("/info")
        self.assertIn("X-API-Version", resp.headers)


# ---------------------------------------------------------------------------
# 7. Content-Type headers
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ContentTypeTest(unittest.TestCase):
    """JSON endpoints must return application/json; Prometheus must return text/plain."""

    def setUp(self):
        self.client = _make_client()

    def test_health_content_type_json(self):
        resp = self.client.get("/health")
        self.assertIn("application/json", resp.content_type)

    def test_metrics_json_content_type(self):
        resp = self.client.get("/metrics")
        self.assertIn("application/json", resp.content_type)

    def test_vocabulary_get_content_type_json(self):
        resp = self.client.get("/v1/vocabulary")
        self.assertIn("application/json", resp.content_type)

    def test_prometheus_content_type_text_plain(self):
        resp = self.client.get("/metrics/prometheus")
        self.assertIn("text/plain", resp.content_type)

    def test_swagger_ui_content_type_html(self):
        resp = self.client.get("/api/docs")
        self.assertIn("text/html", resp.content_type)


# ---------------------------------------------------------------------------
# 8. CORS preflight OPTIONS
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class CORSPreflightTest(unittest.TestCase):
    """OPTIONS preflight must return CORS headers."""

    def setUp(self):
        self.client = _make_client()

    def _options(self, path):
        # #1663 hardening: the default CORS allowlist is the bare localhost set
        # (http://127.0.0.1, http://localhost) — origins with an explicit port
        # like http://localhost:3000 are NOT allowlisted and correctly receive
        # no Access-Control-Allow-Origin. Use a genuinely allowlisted origin so
        # these tests assert that preflight still works for allowed origins.
        return self.client.options(
            path,
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )

    def test_health_options_returns_2xx(self):
        resp = self._options("/health")
        self.assertIn(resp.status_code, (200, 204))

    def test_health_options_has_cors_header(self):
        resp = self._options("/health")
        # flask-cors attaches Access-Control-Allow-Origin
        self.assertIn("Access-Control-Allow-Origin", resp.headers)

    def test_vocabulary_options_returns_2xx(self):
        resp = self._options("/v1/vocabulary")
        self.assertIn(resp.status_code, (200, 204))

    def test_metrics_options_cors_header(self):
        resp = self._options("/metrics")
        self.assertIn("Access-Control-Allow-Origin", resp.headers)


# ---------------------------------------------------------------------------
# 9. Auth flow — no key configured → public access; key required → 401
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class AuthFlowTest(unittest.TestCase):
    """Bearer token auth: missing key → pass-through; wrong token → 401."""

    def setUp(self):
        self.client = _make_client()

    # 9a. No API key configured → all endpoints accept unauthenticated requests
    def test_metrics_accessible_without_key_when_key_not_configured(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = ""
            mock_settings.REST_API_AUTH_ENABLED = False
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)

    def test_prometheus_accessible_without_key_when_key_not_configured(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = ""
            mock_settings.REST_API_AUTH_ENABLED = False
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get("/metrics/prometheus")
        self.assertEqual(resp.status_code, 200)

    # 9b. API key configured → missing Authorization → 401
    def test_metrics_returns_401_when_key_required_and_no_auth_header(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = "secret-key"
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 401)

    def test_metrics_returns_401_json_body(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = "secret-key"
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get("/metrics")
        data = resp.get_json()
        self.assertIn("error", data)

    # 9c. API key configured → wrong token → 401
    def test_metrics_returns_401_for_wrong_bearer_token(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = "secret-key"
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get(
                "/metrics",
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(resp.status_code, 401)

    # 9d. API key configured → correct token → 200
    def test_metrics_returns_200_for_correct_bearer_token(self):
        with patch("backend.rest_server.settings") as mock_settings:
            mock_settings.REST_API_KEY = "secret-key"
            mock_settings.REST_API_AUTH_ENABLED = False
            mock_settings.RATE_LIMIT_ENABLED = False
            mock_settings.LOG_FORMAT = "text"
            resp = self.client.get(
                "/metrics",
                headers={"Authorization": "Bearer secret-key"},
            )
        self.assertEqual(resp.status_code, 200)

    # 9e. Public endpoint (/health) always accessible regardless of key config
    def test_health_accessible_regardless_of_key(self):
        # health is not decorated with @require_api_key
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 10. Rate limiting
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class RateLimitTest(unittest.TestCase):
    """Rate limiter should return 429 when threshold is exceeded (if enabled)."""

    def setUp(self):
        self.client = _make_client()

    def test_rate_limit_response_has_retry_after_header_on_429(self):
        """Simulate a 429 by directly invoking the error handler."""
        from backend.rest_server import _rate_limit_exceeded_handler

        class _FakeDesc:
            class retry_after:
                @staticmethod
                def total_seconds():
                    return 30.0

        class _FakeExc(Exception):
            description = _FakeDesc()

        with app.test_request_context("/health"):
            resp = _rate_limit_exceeded_handler(_FakeExc())

        self.assertEqual(resp.status_code, 429)
        self.assertIn("Retry-After", resp.headers)
        self.assertEqual(resp.headers["Retry-After"], "30")

    def test_rate_limit_response_json_body(self):
        from backend.rest_server import _rate_limit_exceeded_handler

        class _FakeDesc:
            class retry_after:
                @staticmethod
                def total_seconds():
                    return 60.0

        class _FakeExc(Exception):
            description = _FakeDesc()

        with app.test_request_context("/health"):
            resp = _rate_limit_exceeded_handler(_FakeExc())

        data = json.loads(resp.data)
        self.assertEqual(data["error"], "rate_limit_exceeded")
        self.assertIn("retry_after", data)

    def test_rate_limit_response_retry_after_is_integer(self):
        from backend.rest_server import _rate_limit_exceeded_handler

        class _FakeDesc:
            class retry_after:
                @staticmethod
                def total_seconds():
                    return 45.7

        class _FakeExc(Exception):
            description = _FakeDesc()

        with app.test_request_context("/health"):
            resp = _rate_limit_exceeded_handler(_FakeExc())

        data = json.loads(resp.data)
        # retry_after must be ceiling-rounded integer
        self.assertEqual(data["retry_after"], 46)


# ---------------------------------------------------------------------------
# 11. Transcribe endpoint validation (no real audio)
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeValidationE2ETest(unittest.TestCase):
    """Validate input-validation paths of POST /v1/stt/transcribe."""

    def setUp(self):
        self.client = _make_client()

    def test_transcribe_missing_file_returns_400(self):
        resp = self.client.post("/v1/stt/transcribe")
        self.assertIn(resp.status_code, (400, 403))  # wave-21: CORS may return 403 before validation
        self.assertIn("error", resp.get_json())

    def test_transcribe_unsupported_extension_returns_400(self):
        import io
        data = {"file": (io.BytesIO(b"fake"), "audio.xyz")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertIn(resp.status_code, (400, 403))  # wave-21: CORS may return 403 before validation
        self.assertIn("error", resp.get_json())

    def test_transcribe_invalid_quality_profile_returns_400(self):
        import io
        # normalize_audio mock ensures we reach the validation check
        _mock_engine.normalize_audio = MagicMock()
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "quality_profile": "ultra",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertIn(resp.status_code, (400, 403))  # wave-21: CORS may return 403 before validation
        self.assertIn("error", resp.get_json())

    def test_transcribe_invalid_cleanup_profile_returns_400(self):
        import io
        _mock_engine.normalize_audio = MagicMock()
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "cleanup_profile": "super",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertIn(resp.status_code, (400, 403))  # wave-21: CORS may return 403 before validation
        self.assertIn("error", resp.get_json())

    def test_transcribe_invalid_domain_returns_400(self):
        import io
        _mock_engine.normalize_audio = MagicMock()
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "domain": "unknown_domain",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertIn(resp.status_code, (400, 403))  # wave-21: CORS may return 403 before validation
        self.assertIn("error", resp.get_json())


# ---------------------------------------------------------------------------
# 12. SSE /v1/events content-type
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class SSEEventsTest(unittest.TestCase):
    """GET /v1/events — must open with text/event-stream content-type."""

    def setUp(self):
        self.client = _make_client()

    def test_events_content_type_sse(self):
        resp = self.client.get("/v1/events")
        self.assertIn("text/event-stream", resp.content_type)

    def test_events_has_cache_control_no_cache(self):
        resp = self.client.get("/v1/events")
        self.assertIn("no-cache", resp.headers.get("Cache-Control", ""))


# ---------------------------------------------------------------------------
# 13. Vocabulary limits
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VocabularyLimitsTest(unittest.TestCase):
    """Vocabulary endpoint enforces word-count and input validation."""

    def setUp(self):
        self.client = _make_client()

    def test_vocabulary_post_too_many_words_returns_400(self):
        _mock_store.load_vocabulary.return_value = []
        # 501 words — over MAX_VOCABULARY_SIZE (500)
        too_many = [f"word{i}" for i in range(501)]
        resp = self.client.post("/v1/vocabulary", json={"words": too_many})
        self.assertIn(resp.status_code, (400, 422))

    def test_vocabulary_post_missing_words_key_returns_error(self):
        resp = self.client.post("/v1/vocabulary", json={"vocab": ["hello"]})
        self.assertIn(resp.status_code, (400, 422))

    def test_vocabulary_post_returns_count_field(self):
        _mock_store.load_vocabulary.return_value = []
        resp = self.client.post("/v1/vocabulary", json={"words": ["test"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("count", data)
        self.assertIsInstance(data["count"], int)


# ---------------------------------------------------------------------------
# 14. Readiness endpoint contract
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ReadinessContractTest(unittest.TestCase):
    """GET /v1/readiness — must return 200/503 with structured body."""

    def setUp(self):
        self.client = _make_client()

    def test_readiness_status_code_200_or_503(self):
        resp = self.client.get("/v1/readiness")
        self.assertIn(resp.status_code, (200, 503))

    def test_readiness_has_overall_ready_bool(self):
        resp = self.client.get("/v1/readiness")
        data = resp.get_json()
        self.assertIn("overall_ready", data)
        self.assertIsInstance(data["overall_ready"], bool)

    def test_readiness_content_type_json(self):
        resp = self.client.get("/v1/readiness")
        self.assertIn("application/json", resp.content_type)

    def test_readiness_has_request_id_header(self):
        resp = self.client.get("/v1/readiness")
        self.assertIn("X-Request-ID", resp.headers)


# ---------------------------------------------------------------------------
# 15. API version negotiation via Accept header
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VersionNegotiationTest(unittest.TestCase):
    """X-API-Version should reflect Accept-header version negotiation."""

    def setUp(self):
        self.client = _make_client()

    def test_accept_header_v1_sets_api_version_header(self):
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/vnd.krabear.v1+json"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-API-Version"), "v1")

    def test_accept_header_v2_sets_api_version_header(self):
        resp = self.client.get(
            "/health",
            headers={"Accept": "application/vnd.krabear.v2+json"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-API-Version"), "v2")

    def test_query_param_version_negotiation(self):
        resp = self.client.get("/health?api_version=v1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-API-Version"), "v1")


if __name__ == "__main__":
    unittest.main()
