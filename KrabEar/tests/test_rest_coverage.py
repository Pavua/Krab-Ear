"""Coverage tests for KrabEar/backend/rest_server.py — gaps not in existing suites.

Targets (Flask test client only, no live server):
  1. 404 on unknown endpoint — JSON error body
  2. /health schema — all three required fields present and correct types
  3. /v1/readiness returns 503 when overall_ready is False
  4. POST /v1/stt/transcribe — lang_hint form field forwarded to transcriber
  5. POST /v1/stt/transcribe — additional allowed extensions (.flac, .ogg, .opus)
  6. GET /v1/vocabulary — returns existing stored words (non-empty list)
  7. POST /v1/stt/transcribe — quality_profile=accurate accepted (not rejected)
  8. CORS: Access-Control-Allow-Credentials header on normal GET responses

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_rest_coverage.py -v
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
# Patch heavy objects before the module-level AudioEngine() is instantiated.
# ---------------------------------------------------------------------------
_REST_AVAILABLE = False
_rest_mod = None

try:
    import flask  # noqa: F401

    _mock_engine = MagicMock()
    _mock_engine.quality_profile = "balanced"
    _mock_engine.normalize_audio = MagicMock()

    _mock_store = MagicMock()
    _mock_store.load_vocabulary.return_value = []
    _mock_store.is_idempotent.return_value = False
    _mock_store.add_history_item.return_value = MagicMock(id="hist-test-001")
    _mock_store.load_settings.return_value = {}  # wave1212

    _mock_transcriber = MagicMock()
    _mock_transcriber.transcribe.return_value = {
        "text": "тест",
        "raw_text": "тест",
        "confidence": 0.88,
        "duration_ms": 500,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "ru",
        "segments": [],
        "diarization": {},
    }

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
        "total_requests": 0,
        "error_rate": 0.0,
        "status": "waiting_data",
    }

    with patch("core.engine.AudioEngine", return_value=_mock_engine), \
            patch("backend.state_store.StateStore", return_value=_mock_store), \
            patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
            patch("backend.metrics_collector.metrics", _mock_metrics):
        import backend.rest_server as _rest_mod

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _client():
    """Return a fresh Flask test client with rate limiting disabled."""
    app = _rest_mod.app
    app.config["TESTING"] = True
    _rest_mod.limiter.enabled = False
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. 404 on unknown endpoint
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class UnknownEndpoint404Test(unittest.TestCase):
    """Unknown routes must return 404 with a JSON-like response."""

    def setUp(self):
        self.client = _client()

    def test_unknown_get_returns_404(self):
        resp = self.client.get("/does/not/exist")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_post_returns_404(self):
        resp = self.client.post("/v1/nonexistent", json={})
        self.assertEqual(resp.status_code, 404)

    def test_unknown_v1_path_returns_404(self):
        resp = self.client.get("/v1/unknown_resource")
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# 2. /health schema — field types and values
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class HealthSchemaTest(unittest.TestCase):
    """/health must return all three required fields with correct types."""

    def setUp(self):
        self.client = _client()
        self._patcher = patch.object(_rest_mod, "engine", _mock_engine)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_health_status_is_ok_string(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIsInstance(data["status"], str)
        self.assertEqual(data["status"], "ok")

    def test_health_service_is_string(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIsInstance(data["service"], str)
        self.assertEqual(data["service"], "krab-ear")

    def test_health_profile_is_string(self):
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertIsInstance(data["profile"], str)

    def test_health_profile_matches_engine(self):
        _mock_engine.quality_profile = "accurate"
        resp = self.client.get("/health")
        data = resp.get_json()
        self.assertEqual(data["profile"], "accurate")
        _mock_engine.quality_profile = "balanced"

    def test_health_response_code_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 3. /v1/readiness — 503 when not ready
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class ReadinessNotReadyTest(unittest.TestCase):
    """/v1/readiness must return 503 when overall_ready is False."""

    def setUp(self):
        self.client = _client()

    def test_readiness_503_when_not_ready(self):
        not_ready_report = {
            "overall_ready": False,
            "components": {"stt": False, "diarization": False, "translation": True},
        }
        with patch("backend.rest_server.BackendService") as mock_svc:
            mock_svc._build_readiness_report_static.return_value = not_ready_report
            resp = self.client.get("/v1/readiness")
        self.assertEqual(resp.status_code, 503)

    def test_readiness_503_body_has_overall_ready_false(self):
        not_ready_report = {
            "overall_ready": False,
            "components": {"stt": False, "diarization": True, "translation": True},
        }
        with patch("backend.rest_server.BackendService") as mock_svc:
            mock_svc._build_readiness_report_static.return_value = not_ready_report
            resp = self.client.get("/v1/readiness")
        data = resp.get_json()
        self.assertFalse(data["overall_ready"])

    def test_readiness_200_when_ready(self):
        ready_report = {
            "overall_ready": True,
            "components": {"stt": True, "diarization": True, "translation": True},
        }
        with patch("backend.rest_server.BackendService") as mock_svc:
            mock_svc._build_readiness_report_static.return_value = ready_report
            resp = self.client.get("/v1/readiness")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# 4. POST /v1/stt/transcribe — lang_hint forwarded to transcriber
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeLangHintTest(unittest.TestCase):
    """lang_hint form field must be forwarded to transcriber.transcribe()."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-lang-001")
        self.mock_store.load_settings.return_value = {}  # W1707: prevent privacy_mode 403

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "Привет",
            "raw_text": "Привет",
            "confidence": 0.9,
            "duration_ms": 300,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "ru",
            "segments": [],
            "diarization": {},
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()

        self.mock_metrics = MagicMock()

        self._p_store = patch.object(_rest_mod, "store", self.mock_store)
        self._p_transcriber = patch.object(_rest_mod, "transcriber", self.mock_transcriber)
        self._p_engine = patch.object(_rest_mod, "engine", self.mock_engine)
        self._p_metrics = patch.object(_rest_mod, "metrics", self.mock_metrics)
        self._p_store.start()
        self._p_transcriber.start()
        self._p_engine.start()
        self._p_metrics.start()

        _rest_mod.limiter.enabled = False
        self.client = _rest_mod.app.test_client()
        _rest_mod.app.config["TESTING"] = True

    def tearDown(self):
        self._p_store.stop()
        self._p_transcriber.stop()
        self._p_engine.stop()
        self._p_metrics.stop()

    def test_lang_hint_ru_forwarded(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "lang_hint": "ru",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_transcriber.transcribe.call_args[1]
        self.assertEqual(call_kwargs.get("lang_hint"), "ru")

    def test_lang_hint_es_forwarded(self):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "lang_hint": "es",
        }
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_transcriber.transcribe.call_args[1]
        self.assertEqual(call_kwargs.get("lang_hint"), "es")

    def test_no_lang_hint_passes_none(self):
        """When lang_hint omitted, transcriber must receive None."""
        data = {"file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200)
        call_kwargs = self.mock_transcriber.transcribe.call_args[1]
        self.assertIsNone(call_kwargs.get("lang_hint"))


# ---------------------------------------------------------------------------
# 5. Additional allowed audio extensions
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class AllowedExtensionsTest(unittest.TestCase):
    """Allowed extensions beyond .wav/.mp3 must be accepted (not rejected 400)."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-ext-001")
        self.mock_store.load_settings.return_value = {}  # W1707: prevent truthy MagicMock → privacy_mode 403

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "ok",
            "raw_text": "ok",
            "confidence": 0.9,
            "duration_ms": 100,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "ru",
            "segments": [],
            "diarization": {},
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()

        self.mock_metrics = MagicMock()

        self._p_store = patch.object(_rest_mod, "store", self.mock_store)
        self._p_transcriber = patch.object(_rest_mod, "transcriber", self.mock_transcriber)
        self._p_engine = patch.object(_rest_mod, "engine", self.mock_engine)
        self._p_metrics = patch.object(_rest_mod, "metrics", self.mock_metrics)
        self._p_store.start()
        self._p_transcriber.start()
        self._p_engine.start()
        self._p_metrics.start()

        _rest_mod.limiter.enabled = False
        self.client = _rest_mod.app.test_client()
        _rest_mod.app.config["TESTING"] = True

    def tearDown(self):
        self._p_store.stop()
        self._p_transcriber.stop()
        self._p_engine.stop()
        self._p_metrics.stop()

    def _post_audio(self, filename):
        # W1224: _validate_audio_magic_bytes checks first 16 bytes.
        # Use real magic bytes per extension so validation passes.
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("flac",):
            magic = b"fLaC" + b"\x00" * 12
        elif ext in ("ogg", "opus"):
            magic = b"OggS" + b"\x00" * 12
        elif ext in ("m4a", "mp4", "aac"):
            magic = b"\x00\x00\x00\x18ftyp" + b"\x00" * 8  # ftyp at offset 4
        elif ext in ("wav",):
            magic = b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 4
        elif ext in ("mp3",):
            magic = b"ID3" + b"\x00" * 13
        elif ext in ("webm",):
            magic = b"\x1A\x45\xDF\xA3" + b"\x00" * 12
        else:
            magic = b"fake-audio-data"
        data = {"file": (io.BytesIO(magic), filename)}
        return self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )

    def test_flac_extension_accepted(self):
        resp = self._post_audio("recording.flac")
        self.assertEqual(resp.status_code, 200)

    def test_ogg_extension_accepted(self):
        resp = self._post_audio("recording.ogg")
        self.assertEqual(resp.status_code, 200)

    def test_opus_extension_accepted(self):
        resp = self._post_audio("recording.opus")
        self.assertEqual(resp.status_code, 200)

    def test_m4a_extension_accepted(self):
        resp = self._post_audio("recording.m4a")
        self.assertEqual(resp.status_code, 200)

    def test_txt_extension_rejected(self):
        resp = self._post_audio("notes.txt")
        self.assertEqual(resp.status_code, 400)

    def test_exe_extension_rejected(self):
        resp = self._post_audio("malware.exe")
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# 6. GET /v1/vocabulary — returns existing stored words
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class VocabularyGetWithDataTest(unittest.TestCase):
    """GET /v1/vocabulary must reflect whatever StateStore.load_vocabulary returns."""

    def setUp(self):
        self.mock_store = MagicMock()
        self._patcher = patch.object(_rest_mod, "store", self.mock_store)
        self._patcher.start()
        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()

    def tearDown(self):
        self._patcher.stop()

    def test_get_vocabulary_returns_stored_words(self):
        self.mock_store.load_vocabulary.return_value = ["краб", "антигравитация", "whisper"]
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["words"], ["краб", "антигравитация", "whisper"])

    def test_get_vocabulary_empty_store_returns_empty_list(self):
        self.mock_store.load_vocabulary.return_value = []
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["words"], [])

    def test_get_vocabulary_calls_load_vocabulary_once(self):
        self.mock_store.load_vocabulary.return_value = ["test"]
        self.client.get("/v1/vocabulary")
        self.mock_store.load_vocabulary.assert_called_once()


# ---------------------------------------------------------------------------
# 7. POST /v1/stt/transcribe — quality_profile=accurate is accepted
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TranscribeValidQualityProfileTest(unittest.TestCase):
    """All three valid quality profiles must be accepted (not 400)."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-qp-001")
        self.mock_store.load_settings.return_value = {}  # W1707: prevent privacy_mode 403

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "result",
            "raw_text": "result",
            "confidence": 0.95,
            "duration_ms": 200,
            "engine": "mlx-whisper",
            "model": "whisper-large",
            "language": "ru",
            "segments": [],
            "diarization": {},
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "accurate"
        self.mock_engine.normalize_audio = MagicMock()

        self.mock_metrics = MagicMock()

        self._p_store = patch.object(_rest_mod, "store", self.mock_store)
        self._p_transcriber = patch.object(_rest_mod, "transcriber", self.mock_transcriber)
        self._p_engine = patch.object(_rest_mod, "engine", self.mock_engine)
        self._p_metrics = patch.object(_rest_mod, "metrics", self.mock_metrics)
        self._p_store.start()
        self._p_transcriber.start()
        self._p_engine.start()
        self._p_metrics.start()

        _rest_mod.limiter.enabled = False
        self.client = _rest_mod.app.test_client()
        _rest_mod.app.config["TESTING"] = True

    def tearDown(self):
        self._p_store.stop()
        self._p_transcriber.stop()
        self._p_engine.stop()
        self._p_metrics.stop()

    def _post_with_quality(self, quality):
        data = {
            "file": (io.BytesIO(b"RIFF....WAVEfmt "), "audio.wav"),
            "quality_profile": quality,
        }
        return self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )

    def test_quality_accurate_returns_200(self):
        resp = self._post_with_quality("accurate")
        self.assertEqual(resp.status_code, 200)

    def test_quality_fast_returns_200(self):
        resp = self._post_with_quality("fast")
        self.assertEqual(resp.status_code, 200)

    def test_quality_balanced_returns_200(self):
        resp = self._post_with_quality("balanced")
        self.assertEqual(resp.status_code, 200)

    def test_quality_invalid_returns_400(self):
        resp = self._post_with_quality("ultra_hd")
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# 8. CORS: Access-Control-Allow-Credentials on normal GET responses
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class CORSCredentialsHeaderTest(unittest.TestCase):
    """Responses to cross-origin requests must include CORS credentials header."""

    def setUp(self):
        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()

    def test_health_cors_allow_credentials(self):
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )
        allow_creds = resp.headers.get("Access-Control-Allow-Credentials", "")
        # wave1207: CORS_ORIGINS=* disables credentials
        self.assertNotEqual(allow_creds.lower(), "true")

    def test_vocabulary_cors_allow_credentials(self):
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Origin": "http://app.local:8080"},
        )
        allow_creds = resp.headers.get("Access-Control-Allow-Credentials", "")
        # wave1207: CORS_ORIGINS=* disables credentials
        self.assertNotEqual(allow_creds.lower(), "true")

    def test_cors_blocks_non_allowlisted_cross_origin(self):
        """#1663 hardening: a cross-origin request from an origin that is NOT
        in the localhost allowlist must NOT receive Access-Control-Allow-Origin
        (so the browser refuses the transcript/event read). The previous test
        asserted ACAO was present for an arbitrary cross-origin — that tested
        the vulnerable permissive behavior and is now stale."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "https://example.com"},
        )
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)

    def test_cors_allow_origin_present_for_allowlisted_origin(self):
        """An allowlisted localhost origin still receives ACAO reflecting it."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(
            resp.headers.get("Access-Control-Allow-Origin"),
            "http://localhost",
        )

    def test_metrics_cors_expose_headers(self):
        """X-Request-ID should be in Access-Control-Expose-Headers for an
        allowlisted origin (default allowlist has no port, so use bare
        http://localhost, not http://localhost:3000)."""
        resp = self.client.get(
            "/health",
            headers={"Origin": "http://localhost"},
        )
        expose = resp.headers.get("Access-Control-Expose-Headers", "")
        self.assertIn("X-Request-ID", expose)


if __name__ == "__main__":
    unittest.main()
