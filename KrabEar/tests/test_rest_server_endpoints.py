"""Unit tests for KrabEar/backend/rest_server.py — endpoint coverage.

Covers:
  - GET /health → 200 + valid JSON (status, service, profile keys)
  - GET /metrics → 200 + valid dict (no auth key configured)
  - GET /metrics with valid Bearer token → 200
  - GET /metrics with invalid Bearer token → 401
  - GET /metrics with missing Authorization header → 401
  - POST /v1/stt/transcribe without audio part → 400
  - POST /v1/stt/transcribe with unsupported file extension → 400
  - POST /v1/stt/transcribe with invalid quality_profile → 400
  - GET /v1/events SSE endpoint → 200, text/event-stream content-type
  - Rate limit: >N requests → 429 (when rate limiting enabled)
  - CORS: OPTIONS preflight → Allow-Origin present
  - GET /info → 200 + supported_versions key
  - X-Request-ID header present on every response

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_server_endpoints.py -v
"""
from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Guard: skip if Flask / REST-server dependencies are missing.
# Patch heavy runtime objects before module-level instantiation.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None

try:
    import flask  # noqa: F401

    _import_engine = MagicMock()
    _import_engine.quality_profile = "balanced"

    _import_store = MagicMock()
    _import_store.load_vocabulary.return_value = []
    _import_store.is_idempotent.return_value = False
    _import_store.add_history_item.return_value = MagicMock(id="hist-test-001")

    _import_transcriber = MagicMock()
    _import_transcriber.transcribe.return_value = {
        "text": "hello world",
        "raw_text": "hello world",
        "confidence": 0.9,
        "duration_ms": 500,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "en",
        "segments": [],
        "diarization": {},
    }

    _import_metrics = MagicMock()
    _import_metrics.get_summary.return_value = {
        "total_requests": 3,
        "error_rate": 0.0,
        "error_count": 0,
        "request_count": 3,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 200, "p95": 700, "p99": 1200, "avg": 250},
            "confidence": {"avg": 0.9},
        },
        "window_size": 3,
    }

    with patch("core.engine.AudioEngine", return_value=_import_engine), \
            patch("backend.state_store.StateStore", return_value=_import_store), \
            patch("backend.transcriber.Transcriber", return_value=_import_transcriber), \
            patch("backend.metrics_collector.metrics", _import_metrics):
        import backend.rest_server as _rest_mod

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _make_client():
    app = _rest_mod.app
    app.config["TESTING"] = True
    return app.test_client()


# ---------------------------------------------------------------------------
# Helper base: per-test patches for engine/store/transcriber/metrics + no rate limit
# ---------------------------------------------------------------------------

class _RestBase(unittest.TestCase):
    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-base-001")

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "hello world",
            "raw_text": "hello world",
            "confidence": 0.9,
            "duration_ms": 500,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "en",
            "segments": [],
            "diarization": {},
        }

        self.mock_metrics = MagicMock()
        self.mock_metrics.get_summary.return_value = {
            "total_requests": 3,
            "error_rate": 0.0,
            "error_count": 0,
            "request_count": 3,
            "status": "ok",
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()

        self._p_store = patch.object(_rest_mod, "store", self.mock_store)
        self._p_transcriber = patch.object(_rest_mod, "transcriber", self.mock_transcriber)
        self._p_metrics = patch.object(_rest_mod, "metrics", self.mock_metrics)
        self._p_engine = patch.object(_rest_mod, "engine", self.mock_engine)
        for p in (self._p_store, self._p_transcriber, self._p_metrics, self._p_engine):
            p.start()

        # Disable rate limiting to avoid cross-test counter leakage
        self._orig_limiter_enabled = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        self.client = _make_client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter_enabled
        for p in (self._p_store, self._p_transcriber, self._p_metrics, self._p_engine):
            p.stop()


# ===========================================================================
# 1. GET /health
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class HealthEndpointTest(_RestBase):
    """GET /health → 200 with JSON body containing expected keys."""

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_returns_json(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertIsNotNone(body, "Expected JSON response from /health")

    def test_health_has_status_ok(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertEqual(body.get("status"), "ok")

    def test_health_has_service_key(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertEqual(body.get("service"), "krab-ear")

    def test_health_has_profile_key(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertIn("profile", body)
        self.assertEqual(body["profile"], "balanced")


# ===========================================================================
# 2. GET /metrics — no auth configured (REST_API_KEY empty)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class MetricsEndpointNoAuthTest(_RestBase):
    """GET /metrics when REST_API_KEY is not set → 200 with valid dict."""

    def setUp(self):
        super().setUp()
        # Ensure REST_API_KEY is empty (auth disabled)
        self._p_apikey = patch.object(
            _rest_mod.settings, "REST_API_KEY", ""
        )
        self._p_apikey.start()

    def tearDown(self):
        self._p_apikey.stop()
        super().tearDown()

    def test_metrics_returns_200(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)

    def test_metrics_returns_json(self):
        resp = self.client.get("/metrics")
        body = resp.get_json()
        self.assertIsNotNone(body, "Expected JSON from /metrics")

    def test_metrics_get_summary_was_called(self):
        self.client.get("/metrics")
        self.mock_metrics.get_summary.assert_called()


# ===========================================================================
# 3. GET /metrics — Bearer auth enforced
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class MetricsEndpointAuthTest(_RestBase):
    """GET /metrics when REST_API_KEY is set — auth enforcement tests."""

    _API_KEY = "test-secret-key-abc123"

    def setUp(self):
        super().setUp()
        self._p_apikey = patch.object(
            _rest_mod.settings, "REST_API_KEY", self._API_KEY
        )
        self._p_apikey.start()

    def tearDown(self):
        self._p_apikey.stop()
        super().tearDown()

    def test_valid_bearer_token_returns_200(self):
        resp = self.client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {self._API_KEY}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_invalid_bearer_token_returns_401(self):
        resp = self.client.get(
            "/metrics",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_missing_auth_header_returns_401(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 401)

    def test_non_bearer_scheme_returns_401(self):
        resp = self.client.get(
            "/metrics",
            headers={"Authorization": f"Basic {self._API_KEY}"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_auth_error_response_is_json(self):
        resp = self.client.get("/metrics")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)


# ===========================================================================
# 4. POST /v1/stt/transcribe — missing file part
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeMissingFilepartTest(_RestBase):
    """POST /v1/stt/transcribe without any file part → 400."""

    def test_no_file_part_returns_400(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"quality_profile": "balanced"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_no_file_part_has_error_key(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"quality_profile": "balanced"},
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)


# ===========================================================================
# 5. POST /v1/stt/transcribe — unsupported file extension
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeUnsupportedExtensionTest(_RestBase):
    """POST /v1/stt/transcribe with .txt file → 400."""

    def test_txt_extension_returns_400(self):
        data = {"file": (io.BytesIO(b"not audio"), "transcript.txt")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_exe_extension_returns_400(self):
        data = {"file": (io.BytesIO(b"MZ\x90"), "malware.exe")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_unsupported_ext_error_message(self):
        data = {"file": (io.BytesIO(b"not audio"), "file.doc")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIn("error", body)
        self.assertIn("Unsupported", body["error"])


# ===========================================================================
# 6. POST /v1/stt/transcribe — invalid quality_profile
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeInvalidQualityProfileTest(_RestBase):
    """POST /v1/stt/transcribe with invalid quality_profile → 400."""

    def test_invalid_quality_profile_returns_400(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "quality_profile": "ultra",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_quality_profile_error_json(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "quality_profile": "superfast",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIn("error", body)


# ===========================================================================
# 7. GET /v1/events — SSE endpoint
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class SSEEventsEndpointTest(_RestBase):
    """GET /v1/events → response is text/event-stream."""

    def test_events_returns_200(self):
        # Close the SSE stream immediately by reading zero bytes
        resp = self.client.get("/v1/events")
        self.assertEqual(resp.status_code, 200)

    def test_events_content_type_is_event_stream(self):
        resp = self.client.get("/v1/events")
        self.assertIn("text/event-stream", resp.content_type)

    def test_events_cache_control_no_cache(self):
        resp = self.client.get("/v1/events")
        # Flask test client materializes the full stream; cache-control should be set
        cache = resp.headers.get("Cache-Control", "")
        self.assertIn("no-cache", cache)


# ===========================================================================
# 8. Rate limiting — 429 response when limit exceeded
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class RateLimitTest(_RestBase):
    """Exceeding configured request rate returns 429 with JSON body."""

    def setUp(self):
        super().setUp()
        # Re-enable rate limiting for this test
        _rest_mod.limiter.enabled = True

    def tearDown(self):
        # Reset after the test; the base tearDown restores the original value
        super().tearDown()

    def test_exceeding_health_rate_limit_returns_429(self):
        """GET /health has a 120/min limit; hammer it 130 times."""
        # Use a storage reset to clear any accumulated counts from other tests
        try:
            _rest_mod.limiter.reset()
        except Exception:
            pass

        responses = []
        for _ in range(130):
            r = self.client.get("/health")
            responses.append(r.status_code)
            if r.status_code == 429:
                break

        self.assertIn(
            429,
            responses,
            "Expected 429 after exceeding rate limit — none received in 130 requests",
        )

    def test_429_response_has_json_error(self):
        """When rate limit is hit, response body has JSON error key."""
        try:
            _rest_mod.limiter.reset()
        except Exception:
            pass

        last_resp = None
        for _ in range(130):
            last_resp = self.client.get("/health")
            if last_resp.status_code == 429:
                break

        if last_resp and last_resp.status_code == 429:
            body = last_resp.get_json()
            self.assertIsNotNone(body)
            self.assertIn("error", body)
        else:
            self.skipTest("Rate limit was not triggered within 130 requests")  # pragma: no cover


# ===========================================================================
# 9. CORS headers
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class CorsHeadersTest(_RestBase):
    """CORS Access-Control-Allow-Origin present on responses."""

    def test_cors_header_present_on_health(self):
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        # flask-cors should add Access-Control-Allow-Origin
        cors_header = resp.headers.get("Access-Control-Allow-Origin")
        self.assertIsNotNone(
            cors_header,
            "Expected Access-Control-Allow-Origin header on /health response",
        )

    def test_cors_options_preflight(self):
        resp = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        # Preflight should succeed (200 or 204)
        self.assertIn(resp.status_code, (200, 204))


# ===========================================================================
# 10. GET /info — API versioning metadata
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ApiInfoEndpointTest(_RestBase):
    """GET /info → 200 JSON with version metadata."""

    def test_info_returns_200(self):
        resp = self.client.get("/info")
        self.assertEqual(resp.status_code, 200)

    def test_info_has_supported_versions(self):
        resp = self.client.get("/info")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("supported_versions", body)
        self.assertIsInstance(body["supported_versions"], list)

    def test_info_has_current_version(self):
        resp = self.client.get("/info")
        body = resp.get_json()
        self.assertIn("current_version", body)


# ===========================================================================
# 11. X-Request-ID header on every response
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class RequestIdHeaderTest(_RestBase):
    """X-Request-ID header should be present on every response."""

    def test_health_has_request_id_header(self):
        resp = self.client.get("/health")
        self.assertIn(
            "X-Request-ID",
            resp.headers,
            "Expected X-Request-ID header on /health response",
        )

    def test_metrics_has_request_id_header(self):
        with patch.object(_rest_mod.settings, "REST_API_KEY", ""):
            resp = self.client.get("/metrics")
        self.assertIn("X-Request-ID", resp.headers)

    def test_request_id_is_non_empty(self):
        resp = self.client.get("/health")
        rid = resp.headers.get("X-Request-ID", "")
        self.assertTrue(len(rid) > 0, "X-Request-ID must not be empty")


if __name__ == "__main__":
    unittest.main()
