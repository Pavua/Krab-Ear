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
    _import_store.load_settings.return_value = {}  # wave1212

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
        self.mock_store.load_settings.return_value = {}  # wave1212

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


def _fake_sse_stream_endpoint(*args, **kwargs):
    """Finite SSE generator for test_rest_server_endpoints — exits immediately.

    W1748 / W1746: the real sse_stream() blocks for ≥15 s per iteration
    (poll timeout on the internal queue) and never terminates without an
    external shutdown signal.  Under pytest-xdist -n 2, this causes the
    xdist worker process to appear to crash (gw0 node down: Not properly
    terminated) when the test function returns while the generator is still
    blocking.  Replacing sse_stream with this one-shot stub lets SSE endpoint
    tests verify headers/status/cache-control without hanging the worker.
    """
    yield ": keepalive\n\n"


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class SSEEventsEndpointTest(_RestBase):
    """GET /v1/events → response is text/event-stream."""

    def setUp(self):
        super().setUp()
        # W1748: patch sse_stream to a finite stub so the xdist worker does not
        # block on the 15-second poll timeout inside the real generator.
        self._p_sse = patch("backend.rest_server.sse_stream",
                            side_effect=_fake_sse_stream_endpoint)
        self._p_sse.start()

    def tearDown(self):
        self._p_sse.stop()
        super().tearDown()

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
        # Flush limiter counters so unrelated test files that hit /health
        # don't inherit 429 state from this class (test isolation).
        try:
            _rest_mod.limiter.reset()
        except Exception:
            pass
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
        # #1663 hardening: ACAO is only injected for allowlisted origins. The
        # default allowlist is the bare localhost set (no port), so use
        # http://localhost rather than http://localhost:3000 (not allowlisted).
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost"},
        )
        # flask-cors should add Access-Control-Allow-Origin for the allowlisted
        # origin, reflecting it back.
        cors_header = resp.headers.get("Access-Control-Allow-Origin")
        self.assertEqual(
            cors_header,
            "http://localhost",
            "Expected Access-Control-Allow-Origin header on /health response",
        )

    def test_cors_header_absent_for_non_allowlisted_origin(self):
        """A non-allowlisted cross-origin must NOT receive ACAO (#1663)."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

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


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TTSSynthesizeEndpointTest(_RestBase):
    """POST /v1/tts/synthesize tests."""

    def test_valid_request_returns_200(self):
        # 🔴 Pin the patch to `_rest_mod` — the SAME module object _make_client()'s
        # app belongs to. A string target "backend.rest_server.tts_service..." can
        # land on a reloaded module B while the app/route still live on the captured
        # module A → the real macOS `say` runs (absent on Linux → FileNotFoundError
        # in CI). The sentinel engine below proves the mock served the request.
        fake_tts = MagicMock()
        fake_tts.handle_synthesize_speech.return_value = {
            "wav_bytes_b64": "ZmFrZQ==",
            "language": "ru",
            "engine": "mock-engine",
            "byte_count": 4,
        }
        with patch.object(_rest_mod, "tts_service", fake_tts):
            resp = self.client.post(
                "/v1/tts/synthesize",
                json={"text": "привет", "language": "ru"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("wav_bytes_b64", body)
        self.assertEqual(body["engine"], "mock-engine")
        fake_tts.handle_synthesize_speech.assert_called_once()

    def test_missing_text_returns_400(self):
        resp = self.client.post(
            "/v1/tts/synthesize",
            json={"language": "ru"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)

    def test_service_error_returns_400(self):
        fake_tts = MagicMock()
        fake_tts.handle_synthesize_speech.return_value = {
            "ok": False,
            "error": "text exceeds maximum length",
        }
        with patch.object(_rest_mod, "tts_service", fake_tts):
            resp = self.client.post(
                "/v1/tts/synthesize",
                json={"text": "x" * 6000},
            )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)


# ===========================================================================
# 13. GET /v1/models — STT/cloud/LLM catalog (Voice Gateway bridge)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ModelsEndpointTest(_RestBase):
    """GET /v1/models → 200 with catalog of STT engines, cloud providers, LLM models.

    Tests are chunk-pollution-safe: all patching is done via patch.object on the
    _rest_mod object reference captured at module-import time (not string targets),
    following the pattern documented in CLAUDE.md "Reload variant" section.

    mlx-masking safe: we NEVER assert that mlx_whisper is importable or that
    stt_engines is non-empty.  Instead we monkeypatch build_router to return a
    known fake router — deterministic on both py3.14+mlx and py3.12-without-mlx.
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fake_router(self, adapters=None):
        """Return a mock STTRouter whose _adapters list is controllable."""
        router = MagicMock()
        router._adapters = adapters or []
        return router

    def _fake_adapter(self, model_id="whisper-mlx/test", display_name="Test Whisper",
                      available=True, langs=("en", "ru")):
        adapter = MagicMock()
        adapter.model_id = model_id
        adapter.display_name = display_name
        adapter.is_available.return_value = available
        # supports_language: return True only for langs in the probe set
        adapter.supports_language.side_effect = lambda lang: lang in langs
        return adapter

    # ------------------------------------------------------------------
    # 1. Basic 200 + expected top-level keys
    # ------------------------------------------------------------------

    def test_models_returns_200(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 200)

    def test_models_returns_json_with_ok_true(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertTrue(body.get("ok"), "Expected ok=true in /v1/models response")

    def test_models_has_required_keys(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        for key in ("stt_engines", "cloud_stt", "llm_models",
                    "default_stt", "default_llm"):
            self.assertIn(key, body, f"Missing key: {key}")

    def test_stt_engines_is_list(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertIsInstance(body["stt_engines"], list)

    def test_cloud_stt_is_list(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertIsInstance(body["cloud_stt"], list)

    def test_llm_models_is_list(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertIsInstance(body["llm_models"], list)

    # ------------------------------------------------------------------
    # 2. stt_engines content when adapter present
    # ------------------------------------------------------------------

    def test_stt_engines_includes_adapter_when_present(self):
        """A fake adapter in the router appears in stt_engines."""
        fake_adapter = self._fake_adapter(
            model_id="whisper-mlx/whisper-large-v3-mlx",
            display_name="Whisper MLX (whisper-large-v3-mlx)",
            available=True,
            langs=("en", "ru", "es"),
        )
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router([fake_adapter])):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        engines = body["stt_engines"]
        self.assertEqual(len(engines), 1)
        eng = engines[0]
        self.assertEqual(eng["name"], "whisper-mlx/whisper-large-v3-mlx")
        self.assertEqual(eng["display_name"], "Whisper MLX (whisper-large-v3-mlx)")
        self.assertTrue(eng["available"])
        self.assertTrue(eng["enabled"])
        self.assertEqual(eng["type"], "local")
        # languages should include "en", "ru", "es" (from our fake supports_language)
        self.assertIn("en", eng["languages"])
        self.assertIn("ru", eng["languages"])
        self.assertIn("es", eng["languages"])

    def test_stt_engines_adapter_available_false_reflected(self):
        """An unavailable adapter is reported available=False."""
        fake_adapter = self._fake_adapter(available=False)
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router([fake_adapter])):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertFalse(body["stt_engines"][0]["available"])

    # ------------------------------------------------------------------
    # 3. cloud_stt — availability reflects API key presence
    # ------------------------------------------------------------------

    def test_cloud_stt_has_three_providers(self):
        """Exactly 3 cloud providers are listed (openai, deepgram, assemblyai)."""
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        names = {p["name"] for p in body["cloud_stt"]}
        self.assertEqual(names, {"openai", "deepgram", "assemblyai"})

    def test_cloud_stt_openai_available_when_key_set(self):
        """When openai_api_key is present in settings, openai shows available=True."""
        self.mock_store.load_settings.return_value = {"openai_api_key": "sk-test123"}
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        openai_entry = next(p for p in body["cloud_stt"] if p["name"] == "openai")
        self.assertTrue(openai_entry["available"])

    def test_cloud_stt_openai_unavailable_when_no_key(self):
        """When openai_api_key is empty, openai shows available=False."""
        self.mock_store.load_settings.return_value = {"openai_api_key": ""}
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        openai_entry = next(p for p in body["cloud_stt"] if p["name"] == "openai")
        self.assertFalse(openai_entry["available"])

    def test_cloud_stt_providers_have_type_cloud(self):
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        for p in body["cloud_stt"]:
            self.assertEqual(p.get("type"), "cloud")

    # ------------------------------------------------------------------
    # 4. graceful degradation — build_router raises → 200 with empty stt_engines
    # ------------------------------------------------------------------

    def test_models_200_when_build_router_raises(self):
        """If build_router explodes the endpoint still returns 200 with empty list."""
        with patch("core.pipeline.stt_router_factory.build_router",
                   side_effect=RuntimeError("model load failed")):
            resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("ok"))
        self.assertIsInstance(body["stt_engines"], list)

    def test_models_200_when_store_raises(self):
        """Even if store.load_settings raises the endpoint returns 200."""
        self.mock_store.load_settings.side_effect = OSError("disk error")
        with patch("core.pipeline.stt_router_factory.build_router",
                   side_effect=RuntimeError("no store")):
            resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 200)

    # ------------------------------------------------------------------
    # 5. auth enforcement
    # ------------------------------------------------------------------

    def test_models_requires_auth_when_api_key_set(self):
        """With REST_API_KEY configured, unauthenticated GET → 401."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "secret-key-xyz"), \
                patch("core.pipeline.stt_router_factory.build_router",
                      return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        self.assertEqual(resp.status_code, 401)

    def test_models_accepts_valid_bearer_token(self):
        """With REST_API_KEY configured and correct token → 200."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "secret-key-xyz"), \
                patch("core.pipeline.stt_router_factory.build_router",
                      return_value=self._fake_router()):
            resp = self.client.get(
                "/v1/models",
                headers={"Authorization": "Bearer secret-key-xyz"},
            )
        self.assertEqual(resp.status_code, 200)

    def test_models_rejects_wrong_token(self):
        """Wrong Bearer token → 401."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "secret-key-xyz"), \
                patch("core.pipeline.stt_router_factory.build_router",
                      return_value=self._fake_router()):
            resp = self.client.get(
                "/v1/models",
                headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(resp.status_code, 401)

    # ------------------------------------------------------------------
    # 6. default_stt / default_llm from settings
    # ------------------------------------------------------------------

    def test_default_stt_from_settings(self):
        """default_stt is read from stt_ru_primary_model setting."""
        self.mock_store.load_settings.return_value = {
            "stt_ru_primary_model": "mlx-community/whisper-small-mlx",
            "llm_model": "qwen3-4b-abliterated",
        }
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertEqual(body["default_stt"], "mlx-community/whisper-small-mlx")

    def test_default_llm_from_settings(self):
        """default_llm is read from llm_model setting."""
        self.mock_store.load_settings.return_value = {
            "stt_ru_primary_model": "mlx-community/whisper-large-v3-mlx",
            "llm_model": "supergemma4-26b-abliterated",
        }
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertEqual(body["default_llm"], "supergemma4-26b-abliterated")

    def test_default_stt_fallback_when_not_in_settings(self):
        """When stt_ru_primary_model absent, falls back to the hardcoded default."""
        self.mock_store.load_settings.return_value = {}
        with patch("core.pipeline.stt_router_factory.build_router",
                   return_value=self._fake_router()):
            resp = self.client.get("/v1/models")
        body = resp.get_json()
        self.assertEqual(body["default_stt"], "mlx-community/whisper-large-v3-mlx")


if __name__ == "__main__":
    unittest.main()
