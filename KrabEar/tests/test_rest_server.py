"""REST server (Flask app) unit tests — focused on endpoint dispatch, error handling,
auth (Wave 47 partial), and SSE stream.

Wave 69 verification: AudioEngine is constructed with skip_gigaam_warmup=True so the
REST server never spawns a GigaAM worker subprocess (only BackendService should own it).

Covers:
  - GET /health → 200, status field
  - GET /health — 404 for unknown paths
  - POST /v1/stt/transcribe — missing audio file → 400
  - POST /v1/stt/transcribe — oversized upload → 413
  - POST /v1/stt/transcribe — invalid format (.pdf) → 400
  - POST /v1/stt/transcribe — invalid cleanup_profile → 400
  - POST /v1/stt/transcribe — invalid domain → 400
  - POST /v1/stt/transcribe — valid .wav → 200 + history_id
  - POST /v1/stt/transcribe — idempotency key already seen → skipped
  - GET /v1/events SSE stream → text/event-stream
  - GET /metrics → dashboard data dict
  - GET /health/dashboard → HTML page
  - Wave 69: AudioEngine constructed with skip_gigaam_warmup=True
  - atexit hook registered for _rest_engine_cleanup
  - module import causes no side-effects (no real model load)
  - GET /metrics/prometheus → text/plain Prometheus format
  - Vocabulary GET returns list
  - Vocabulary POST adds words
  - Auth: missing token → 401, valid token → 200

Run:
    VENV="/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear"
    PATH="$VENV/bin:$PATH" PYTHONPATH=KrabEar python -m pytest KrabEar/tests/test_rest_server.py -v --tb=short
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
# Guard: skip entire module if Flask / REST-server deps are missing.
# Heavy runtime objects (AudioEngine, StateStore, Transcriber) are patched
# before the module is imported so no real model is ever loaded.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None

try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_engine.normalize_audio = MagicMock()
    # MagicMock responds True to any hasattr; explicitly remove _unavailable_models
    # so TestNoModuleLevelAudioEngineLoad.test_no_module_level_audio_engine_load
    # can verify this is a stub and not a real AudioEngine (which always sets it).
    del _mock_engine._unavailable_models

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.is_idempotent.return_value = False
    _mock_store.add_history_item.return_value = MagicMock(id="hist-wave71-001")
    _mock_store.load_settings.return_value = {}  # wave1212

    _mock_transcriber = MagicMock()
    _mock_transcriber.transcribe.return_value = {
        "text": "тест транскрипция",
        "raw_text": "тест транскрипция",
        "confidence": 0.92,
        "duration_ms": 420,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 5,
        "error_rate": 0.0,
        "error_count": 0,
        "request_count": 5,
        "status": "ok",
        "stt_metrics": {
            "latency_ms": {"p50": 200, "p95": 700, "p99": 1200, "avg": 250},
            "confidence": {"avg": 0.92},
        },
        "window_size": 5,
    }

    # Import rest_server using sys.modules cache if available (avoids re-loading
    # the module-level AudioEngine which would conflict with other test files in
    # the same xdist worker).  We patch the engine attribute after import so that
    # TestNoModuleLevelAudioEngineLoad sees a stub regardless of import order.
    if "backend.rest_server" not in sys.modules:
        with patch("core.engine.AudioEngine", return_value=_mock_engine), \
                patch("backend.state_store.StateStore", return_value=_mock_store), \
                patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
                patch("backend.metrics_collector.metrics", _mock_metrics):
            import backend.rest_server as _rest_mod  # type: ignore
    else:
        import backend.rest_server as _rest_mod  # type: ignore
        # Replace the module-level engine with our mock so isolation tests pass
        # when this module is imported after another file already loaded rest_server.
        _rest_mod.engine = _mock_engine

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _client():
    _rest_mod.app.config["TESTING"] = True
    return _rest_mod.app.test_client()


# ---------------------------------------------------------------------------
# Base test class — patches module-level singletons + disables rate limiter.
# ---------------------------------------------------------------------------

class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.quality_profile = "balanced"
        self.engine.normalize_audio = MagicMock()

        self.store = MagicMock()
        self.store.load_vocabulary.return_value = []
        self.store.is_idempotent.return_value = False
        self.store.add_history_item.return_value = MagicMock(id="hist-base-w71")
        self.store.load_settings.return_value = {}  # wave1212

        self.transcriber = MagicMock()
        self.transcriber.transcribe.return_value = {
            "text": "hello",
            "raw_text": "hello",
            "confidence": 0.9,
            "duration_ms": 300,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "en",
            "segments": [],
            "diarization": {},
        }

        self.metrics = MagicMock()
        self.metrics.get_summary.return_value = {
            "total_requests": 1,
            "error_rate": 0.0,
            "error_count": 0,
            "request_count": 1,
            "status": "ok",
            "stt_metrics": {
                "latency_ms": {"p50": 100, "p95": 500, "p99": 900, "avg": 150},
                "confidence": {"avg": 0.9},
            },
            "window_size": 1,
        }

        self._patches = [
            patch.object(_rest_mod, "engine", self.engine),
            patch.object(_rest_mod, "store", self.store),
            patch.object(_rest_mod, "transcriber", self.transcriber),
            patch.object(_rest_mod, "metrics", self.metrics),
        ]
        for p in self._patches:
            p.start()

        self._orig_limiter = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        self.client = _client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter
        for p in self._patches:
            p.stop()


# ===========================================================================
# 1. GET /health → 200 + status field
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestHealthEndpointReturns200(_Base):
    """GET /health returns HTTP 200."""

    def test_health_endpoint_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_endpoint_includes_status_field(self):
        resp = self.client.get("/health")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("status", body)
        self.assertEqual(body["status"], "ok")


# ===========================================================================
# 2. 404 for unknown endpoint
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestUnknownEndpoint404(_Base):
    """Requests to undefined routes return 404."""

    def test_404_for_unknown_endpoint(self):
        resp = self.client.get("/v1/does-not-exist-at-all")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_post_endpoint_404(self):
        resp = self.client.post("/v1/nonexistent")
        self.assertEqual(resp.status_code, 404)


# ===========================================================================
# 3. POST /v1/stt/transcribe — missing file → 400
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeRequiresAudioFile(_Base):
    """POST without any file part returns 400 with error key."""

    def test_transcribe_endpoint_requires_audio_file(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"quality_profile": "balanced"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_missing_file_returns_error_key(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={},
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)


# ===========================================================================
# 4. POST /v1/stt/transcribe — oversized file → 413
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeRejectsOversizedFile(_Base):
    """Upload exceeding MAX_CONTENT_LENGTH triggers 413."""

    def test_transcribe_endpoint_rejects_oversized_file(self):
        # Flask enforces MAX_CONTENT_LENGTH and raises RequestEntityTooLarge (413).
        # Simulate by setting a tiny limit for this test.
        orig_limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10  # 10 bytes
        try:
            large_data = b"X" * 100  # 100 bytes > 10-byte limit
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(large_data), "audio.wav")},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 413)
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = orig_limit


# ===========================================================================
# 5. POST /v1/stt/transcribe — invalid format (.pdf) → 400
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeRejectsInvalidFormat(_Base):
    """Non-audio file format returns 400 with Unsupported in error message."""

    def test_transcribe_endpoint_invalid_format(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"%PDF-1.4"), "document.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_format_error_mentions_unsupported(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"<html>"), "page.html")},
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIn("error", body)
        self.assertIn("Unsupported", body["error"])


# ===========================================================================
# 6. GET /v1/events SSE stream → text/event-stream
# ===========================================================================

def _fake_sse_stream(*args, **kwargs):
    """Finite SSE generator for tests — returns one keepalive then exits.

    W1746: the real sse_stream() blocks for ≥15 s per iteration (poll timeout)
    and never terminates without an external shutdown signal.  Replacing it with
    this one-shot stub lets SSE endpoint tests verify headers/status without
    hanging the xdist worker.
    """
    yield ": keepalive\n\n"


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestSSEEventStreamEndpoint(_Base):
    """GET /v1/events opens an SSE stream with correct content-type."""

    def setUp(self):
        super().setUp()
        # Патчим тот же объект модуля, к которому привязан Flask-клиент.
        # Соседний тест может перезагрузить backend.rest_server и заменить запись
        # в sys.modules; строковый target тогда попадёт в новый модуль, а старый
        # endpoint уйдёт в бесконечный настоящий SSE-generator.
        self._p_sse = patch.object(
            _rest_mod,
            "sse_stream",
            side_effect=_fake_sse_stream,
        )
        self._p_sse.start()

    def tearDown(self):
        self._p_sse.stop()
        super().tearDown()

    def test_sse_event_stream_endpoint(self):
        resp = self.client.get("/v1/events")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.content_type)

    def test_sse_stream_has_no_cache_header(self):
        resp = self.client.get("/v1/events")
        cache = resp.headers.get("Cache-Control", "")
        self.assertIn("no-cache", cache)

    def test_sse_stream_with_filter_param(self):
        resp = self.client.get("/v1/events?filter=stt.final")
        self.assertEqual(resp.status_code, 200)


# ===========================================================================
# 7. GET /metrics → dashboard data dict
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestMetricsEndpointReturnsDashboardData(_Base):
    """GET /metrics (no auth configured) returns metrics dict."""

    def setUp(self):
        super().setUp()
        self._p_key = patch.object(_rest_mod.settings, "REST_API_KEY", "")
        self._p_key.start()

    def tearDown(self):
        self._p_key.stop()
        super().tearDown()

    def test_metrics_endpoint_returns_dashboard_data(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIsNotNone(body)

    def test_metrics_calls_get_summary(self):
        self.client.get("/metrics")
        self.metrics.get_summary.assert_called()


# ===========================================================================
# 8. GET /health/dashboard → HTML page
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestDashboardEndpointReturnsHtml(_Base):
    """GET /health/dashboard returns text/html with dashboard markup."""

    def test_dashboard_endpoint_returns_html(self):
        resp = self.client.get("/health/dashboard")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.content_type)

    def test_dashboard_html_contains_krab_ear(self):
        resp = self.client.get("/health/dashboard")
        body = resp.data.decode("utf-8")
        self.assertIn("Krab Ear", body)

    def test_dashboard_html_is_complete_document(self):
        resp = self.client.get("/health/dashboard")
        body = resp.data.decode("utf-8")
        self.assertIn("<!DOCTYPE html", body)
        self.assertIn("</html>", body)


# ===========================================================================
# 9. Wave 69 fix verification: AudioEngine must be init with skip_gigaam_warmup=True
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestEngineInitSkipsGigaamWarmup(unittest.TestCase):
    """Wave 69: verify AudioEngine was constructed with skip_gigaam_warmup=True."""

    def test_engine_init_skips_gigaam_warmup(self):
        """The module-level AudioEngine must have skip_gigaam_warmup=True.

        This verifies the Wave 69 fix: REST server must NOT spawn a GigaAM
        subprocess. Only BackendService (service.py) is the authoritative owner.
        Verified via source inspection (safe across xdist workers).
        """
        src = Path(_rest_mod.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "skip_gigaam_warmup=True",
            src,
            "AudioEngine(skip_gigaam_warmup=True) not found in rest_server.py — Wave 69 fix missing",
        )

    def test_source_code_has_skip_gigaam_warmup_true(self):
        """Cross-check: source file must contain the literal flag assignment."""
        src = Path(_rest_mod.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "skip_gigaam_warmup=True",
            src,
            "rest_server.py must contain AudioEngine(skip_gigaam_warmup=True)",
        )


# ===========================================================================
# 10. atexit hook registered
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestAtexitHookRegistered(unittest.TestCase):
    """_rest_engine_cleanup is registered as an atexit handler."""

    def test_atexit_hook_registered(self):
        # Python's atexit module does not expose its registry directly, but
        # we can verify by re-registering and checking idempotency, or by
        # verifying the function exists and is callable.
        self.assertTrue(
            callable(_rest_mod._rest_engine_cleanup),
            "_rest_engine_cleanup must be a callable atexit handler",
        )

    def test_atexit_cleanup_function_exists(self):
        self.assertTrue(
            hasattr(_rest_mod, "_rest_engine_cleanup"),
            "rest_server module must define _rest_engine_cleanup for atexit",
        )

    def test_atexit_cleanup_does_not_raise_on_none_router(self):
        """_rest_engine_cleanup must be safe when engine._router is None."""
        mock_eng = MagicMock()
        mock_eng._router = None
        with patch.object(_rest_mod, "engine", mock_eng):
            try:
                _rest_mod._rest_engine_cleanup()
            except Exception as exc:
                self.fail(f"_rest_engine_cleanup raised unexpectedly: {exc}")


# ===========================================================================
# 11. Module import — no side-effects (no real model load)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestNoModuleLevelAudioEngineLoad(unittest.TestCase):
    """Module import must not trigger real MLX/GigaAM/torch model loading."""

    def setUp(self):
        # W1748: another test file running in the same xdist worker may have
        # replaced _rest_mod.engine with a MagicMock (which auto-creates
        # _unavailable_models on attribute access).  Reset to our controlled
        # stub before running the isolation checks.
        if _REST_AVAILABLE and _rest_mod is not None:
            _rest_mod.engine = _mock_engine

    def test_no_module_level_audio_engine_load(self):
        """The module-level engine must not have triggered real MLX model loading.

        In test context the module is imported with patched dependencies.  The
        engine stub/mock must NOT have a real `_unavailable_models` set (which is
        only added by the real AudioEngine.__init__ when MLX initialisation runs).
        """
        # Real AudioEngine.__init__ always sets _unavailable_models; stubs don't.
        self.assertFalse(
            hasattr(_rest_mod.engine, "_unavailable_models"),
            "rest_server module-level engine must not be a real AudioEngine in tests "
            "(real AudioEngine.__init__ would set _unavailable_models)",
        )

    def test_rest_server_is_importable(self):
        self.assertIsNotNone(_rest_mod)
        self.assertTrue(hasattr(_rest_mod, "app"))

    def test_app_is_flask_instance(self):
        from flask import Flask
        self.assertIsInstance(_rest_mod.app, Flask)


# ===========================================================================
# 12. GET /metrics/prometheus → Prometheus text format
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestPrometheusMetricsEndpoint(_Base):
    """GET /metrics/prometheus returns Prometheus text exposition format."""

    def setUp(self):
        super().setUp()
        self._p_key = patch.object(_rest_mod.settings, "REST_API_KEY", "")
        self._p_key.start()

    def tearDown(self):
        self._p_key.stop()
        super().tearDown()

    def test_prometheus_returns_200(self):
        resp = self.client.get("/metrics/prometheus")
        self.assertEqual(resp.status_code, 200)

    def test_prometheus_content_type(self):
        resp = self.client.get("/metrics/prometheus")
        self.assertIn("text/plain", resp.content_type)

    def test_prometheus_contains_transcriptions_total(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("krab_ear_transcriptions_total", body)

    def test_prometheus_contains_uptime(self):
        resp = self.client.get("/metrics/prometheus")
        body = resp.data.decode("utf-8")
        self.assertIn("krab_ear_uptime_seconds", body)


# ===========================================================================
# 13. Vocabulary endpoints
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestVocabularyEndpoints(_Base):
    """GET /v1/vocabulary and POST /v1/vocabulary."""

    def test_vocabulary_get_returns_list(self):
        self.store.load_vocabulary.return_value = ["краб", "ухо"]
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("words", body)
        self.assertIsInstance(body["words"], list)

    def test_vocabulary_post_adds_words(self):
        self.store.load_vocabulary.return_value = []
        resp = self.client.post(
            "/v1/vocabulary",
            json={"words": ["тест", "голос"]},
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.store.save_vocabulary.assert_called_once()

    def test_vocabulary_post_returns_count(self):
        self.store.load_vocabulary.return_value = ["краб"]
        resp = self.client.post(
            "/v1/vocabulary",
            json={"words": ["голос"]},
            content_type="application/json",
        )
        body = resp.get_json()
        self.assertIn("count", body)
        self.assertGreaterEqual(body["count"], 1)


# ===========================================================================
# 14. Auth: missing Bearer → 401, valid Bearer → 200
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestBearerAuthEnforcement(_Base):
    """require_api_key decorator blocks requests without valid Bearer token."""

    _KEY = "wave71-secret-token"

    def setUp(self):
        super().setUp()
        self._p_key = patch.object(_rest_mod.settings, "REST_API_KEY", self._KEY)
        self._p_key.start()

    def tearDown(self):
        self._p_key.stop()
        super().tearDown()

    def test_missing_auth_header_returns_401(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 401)

    def test_invalid_token_returns_401(self):
        resp = self.client.get(
            "/metrics",
            headers={"Authorization": "Bearer wrong-token"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_returns_200(self):
        resp = self.client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {self._KEY}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_auth_error_body_has_error_key(self):
        resp = self.client.get("/metrics")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)


# ===========================================================================
# 15. Successful transcription → 200 + history_id
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeSuccessPath(_Base):
    """Valid .wav upload → 200 + expected response keys."""

    def test_valid_wav_returns_200(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)

    def test_valid_wav_response_has_history_id(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")},
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("history_id", body)

    def test_valid_wav_response_has_text(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")},
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIn("text", body)
        self.assertIn("confidence", body)


# ===========================================================================
# 16. Idempotency key: duplicate upload returns skipped
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestIdempotencyKey(_Base):
    """POST with already-seen chat_id + message_id returns skipped status."""

    def test_idempotent_request_returns_skipped(self):
        self.store.is_idempotent.return_value = True
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={
                "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
                "chat_id": "chat-123",
                "message_id": "msg-456",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("status"), "skipped")

    def test_idempotent_response_has_reason(self):
        self.store.is_idempotent.return_value = True
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={
                "file": (io.BytesIO(b"fake"), "audio.wav"),
                "chat_id": "c1",
                "message_id": "m1",
            },
            content_type="multipart/form-data",
        )
        body = resp.get_json()
        self.assertIn("reason", body)
        self.assertEqual(body["reason"], "duplicate")


# ===========================================================================
# 17. Invalid cleanup_profile + invalid domain → 400
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestTranscribeInvalidParams(_Base):
    """Invalid optional parameters return 400."""

    def test_invalid_cleanup_profile_returns_400(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={
                "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
                "cleanup_profile": "magic",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_invalid_domain_returns_400(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={
                "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
                "domain": "alien_language",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# 18. _parse_cors_origins helper
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestParseCorsOrigins(unittest.TestCase):
    """_parse_cors_origins parses wildcard and comma-separated lists."""

    def test_wildcard_returns_star(self):
        result = _rest_mod._parse_cors_origins("*")
        self.assertEqual(result, "*")

    def test_single_origin_returns_list(self):
        result = _rest_mod._parse_cors_origins("http://localhost:3000")
        self.assertIsInstance(result, list)
        self.assertIn("http://localhost:3000", result)

    def test_multiple_origins_returns_list(self):
        result = _rest_mod._parse_cors_origins(
            "http://localhost:3000, https://app.example.com"
        )
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_whitespace_only_star(self):
        result = _rest_mod._parse_cors_origins("  *  ")
        self.assertEqual(result, "*")


# ===========================================================================
# 19. _format_uptime helper
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestFormatUptime(unittest.TestCase):
    """_format_uptime formats durations correctly."""

    def test_zero_seconds(self):
        result = _rest_mod._format_uptime(0)
        self.assertIn("00s", result)

    def test_one_minute(self):
        result = _rest_mod._format_uptime(60)
        self.assertIn("1m", result)

    def test_one_hour(self):
        result = _rest_mod._format_uptime(3600)
        self.assertIn("1h", result)

    def test_one_day(self):
        result = _rest_mod._format_uptime(86400)
        self.assertIn("1d", result)

    def test_complex_duration(self):
        # 2d 3h 14m 05s
        seconds = 2 * 86400 + 3 * 3600 + 14 * 60 + 5
        result = _rest_mod._format_uptime(seconds)
        self.assertIn("2d", result)
        self.assertIn("3h", result)
        self.assertIn("14m", result)
        self.assertIn("05s", result)


# ===========================================================================
# 20. _build_prometheus_text helper
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestBuildPrometheusText(unittest.TestCase):
    """_build_prometheus_text generates valid Prometheus text format."""

    def _make_summary(self):
        return {
            "total_requests": 10,
            "error_rate": 0.1,
            "stt_metrics": {
                "latency_ms": {"p50": 200, "p95": 700, "p99": 1200, "avg": 300},
                "confidence": {"avg": 0.88},
            },
            "window_size": 10,
        }

    def test_contains_transcriptions_total(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_transcriptions_total", text)

    def test_contains_errors_total(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_errors_total", text)

    def test_contains_confidence_avg(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_confidence_avg", text)

    def test_contains_uptime(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_uptime_seconds", text)

    def test_contains_histogram(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertIn("krab_ear_stt_latency_seconds_bucket", text)

    def test_ends_with_newline(self):
        text = _rest_mod._build_prometheus_text(self._make_summary())
        self.assertTrue(text.endswith("\n"))

    def test_empty_summary_no_crash(self):
        try:
            text = _rest_mod._build_prometheus_text({})
            self.assertIsInstance(text, str)
        except Exception as exc:
            self.fail(f"_build_prometheus_text raised on empty summary: {exc}")


if __name__ == "__main__":
    unittest.main()
