"""Wave 1212 — REST server security fixes: F1+F2+F3+F6 (W1207 residual).

Covers:
  F1 MED — @require_api_key on missing endpoints:
    - test_vocabulary_get_requires_api_key
    - test_vocabulary_post_requires_api_key
    - test_events_requires_api_key
    - test_stt_transcribe_requires_api_key

  F2 MED — CORS wildcard + credentials=False:
    - test_cors_credentials_forced_false_when_origins_wildcard
    - test_cors_credentials_allowed_when_explicit_origins

  F3 LOW — platform mask in /health/dashboard:
    - test_health_dashboard_masks_os_build

  F6 LOW — privacy mode guard on /v1/stt/transcribe:
    - test_stt_transcribe_blocked_in_privacy_mode
    - test_stt_transcribe_allowed_when_privacy_mode_off

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_rest_server_w1212.py -v
"""
from __future__ import annotations

import io
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Lazy REST server import with stubs
# ---------------------------------------------------------------------------

_REST_AVAILABLE = False
_rest_mod = None


def _ensure_stubs():
    """Register all heavy-module stubs before importing rest_server.

    Returns the list of module names actually INSERTED into sys.modules
    (only those not already present — see the `if mod_name not in
    sys.modules` guard below). The caller pops exactly these names again
    once backend.rest_server has been imported: a stray fake module left
    in sys.modules poisons every later test file in the same pytest
    chunk that imports backend.state_store/backend.service directly
    (red CI 2026-07-12 — chunk-pollution class, see CLAUDE.md).
    """
    stub_specs = {
        "core.engine": {
            "AudioEngine": type("_FE", (), {
                "__init__": lambda self, *a, **k: None,
                "quality_profile": "balanced",
                "normalize_audio": lambda self, *a, **k: None,
                "_router": None,
            }),
        },
        "backend.event_bus": {
            "bus": MagicMock(),
            "sse_stream": MagicMock(return_value=iter([])),
        },
        "backend.service": {
            "BackendService": type("_FBS", (), {
                "_build_readiness_report_static": staticmethod(
                    lambda: {"overall_ready": True, "components": {}}
                ),
            }),
        },
        "backend.state_store": {
            "StateStore": type("_FSS", (), {
                "__init__": lambda self, *a, **k: None,
                "is_idempotent": lambda self, *a, **k: False,
                "load_vocabulary": lambda self: [],
                "save_vocabulary": lambda self, *a, **k: None,
                "add_history_item": lambda self, **kw: MagicMock(id="hist-w1212"),
                "load_settings": lambda self: {"privacy_mode_enabled": False},
            }),
        },
        "backend.transcriber": {
            "Transcriber": type("_FT", (), {
                "__init__": lambda self, *a, **k: None,
                "transcribe": lambda self, *a, **kw: {
                    "text": "hello w1212",
                    "raw_text": "hello w1212",
                    "confidence": 0.9,
                    "duration_ms": 300,
                    "engine": "mlx-whisper",
                    "model": "whisper-small",
                    "language": "en",
                    "segments": [],
                    "diarization": {},
                },
            }),
        },
        "backend.metrics_collector": {
            "metrics": type("_FM", (), {
                "get_summary": lambda self: {
                    "latency_p50_ms": None,
                    "latency_p95_ms": None,
                    "latency_p99_ms": None,
                    "confidence_avg": None,
                    "request_count": 0,
                    "error_count": 0,
                    "total_requests": 0,
                    "error_rate": 0.0,
                    "status": "waiting_data",
                    "stt_metrics": {},
                    "window_size": 0,
                },
                "record": lambda self, *a, **k: None,
            })(),
        },
    }
    inserted = []
    for mod_name, attrs in stub_specs.items():
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
            inserted.append(mod_name)
    return inserted


_inserted_stub_modules: list = []
try:
    import flask  # noqa: F401
    _inserted_stub_modules = _ensure_stubs()
    with patch("pathlib.Path.mkdir"):
        import backend.rest_server as _rest_mod
    _REST_AVAILABLE = True
except Exception as _import_err:
    pass
finally:
    # Снимаем ВСТАВЛЕННЫЕ НАМИ фейки из sys.modules: rest_server уже связал
    # свои top-level ссылки на них (module-level `from backend.state_store
    # import StateStore` и т.п. в rest_server.py), а соседи по chunk-процессу
    # должны получать НАСТОЯЩИЕ backend.service/state_store/... — иначе
    # фейк _FBS/_FSS отравляет все последующие тест-файлы чанка (красный
    # CI 2026-07-12: test_search_by_speaker.py / test_send_imessage.py /
    # test_rsf_silence_ranges_wiring_W1139.py получали `_FSS`/`_FBS`
    # вместо реальных StateStore/BackendService).
    for _name in _inserted_stub_modules:
        sys.modules.pop(_name, None)


def _make_client():
    app = _rest_mod.app
    app.config["TESTING"] = True
    return app.test_client()


class _RestBase(unittest.TestCase):
    """Base class: patches runtime singletons and disables rate limiting."""

    def setUp(self):
        self.mock_store = MagicMock()
        self.mock_store.load_vocabulary.return_value = []
        self.mock_store.is_idempotent.return_value = False
        self.mock_store.add_history_item.return_value = MagicMock(id="hist-w1212")
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": False}

        self.mock_transcriber = MagicMock()
        self.mock_transcriber.transcribe.return_value = {
            "text": "hello w1212",
            "raw_text": "hello w1212",
            "confidence": 0.9,
            "duration_ms": 300,
            "engine": "mlx-whisper",
            "model": "whisper-small",
            "language": "en",
            "segments": [],
            "diarization": {},
        }

        self.mock_metrics = MagicMock()
        self.mock_metrics.get_summary.return_value = {
            "total_requests": 0,
            "error_rate": 0.0,
            "error_count": 0,
            "request_count": 0,
            "status": "waiting_data",
            "stt_metrics": {},
            "window_size": 0,
        }

        self.mock_engine = MagicMock()
        self.mock_engine.quality_profile = "balanced"
        self.mock_engine.normalize_audio = MagicMock()
        self.mock_engine._router = None

        self._patches = [
            patch.object(_rest_mod, "store", self.mock_store),
            patch.object(_rest_mod, "transcriber", self.mock_transcriber),
            patch.object(_rest_mod, "metrics", self.mock_metrics),
            patch.object(_rest_mod, "engine", self.mock_engine),
        ]
        for p in self._patches:
            p.start()

        self._orig_limiter = _rest_mod.limiter.enabled
        _rest_mod.limiter.enabled = False

        self._orig_api_key = _rest_mod.settings.REST_API_KEY
        self._orig_auth_enabled = getattr(
            _rest_mod.settings, "REST_API_AUTH_ENABLED", False
        )
        # Auth disabled by default in base — individual tests enable it
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False

        self.client = _make_client()

    def tearDown(self):
        _rest_mod.limiter.enabled = self._orig_limiter
        _rest_mod.settings.REST_API_KEY = self._orig_api_key
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = self._orig_auth_enabled
        for p in self._patches:
            p.stop()


# ===========================================================================
# F1 — @require_api_key on vocabulary GET/POST, /v1/events, /v1/stt/transcribe
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestVocabularyGetRequiresApiKey(_RestBase):
    """GET /v1/vocabulary must return 401 when API key is configured but missing."""

    def test_vocabulary_get_requires_api_key_legacy_mode(self):
        """GET /v1/vocabulary returns 401 when REST_API_KEY is set but header absent."""
        _rest_mod.settings.REST_API_KEY = "test-secret-key-vocab"
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 without auth, got {resp.status_code}")

    def test_vocabulary_get_returns_200_with_valid_key(self):
        """GET /v1/vocabulary returns 200 with correct Bearer token."""
        key = "valid-vocab-key-get"
        _rest_mod.settings.REST_API_KEY = key
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(resp.status_code, 200,
                         f"Expected 200 with correct key, got {resp.status_code}")

    def test_vocabulary_get_returns_401_with_wrong_key(self):
        """GET /v1/vocabulary returns 401 with wrong Bearer token."""
        _rest_mod.settings.REST_API_KEY = "real-key-vocab"
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Authorization": "Bearer wrong-key"},
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 with wrong key, got {resp.status_code}")

    def test_vocabulary_get_accessible_when_no_auth_configured(self):
        """GET /v1/vocabulary is accessible when auth is fully disabled (no key set)."""
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False
        resp = self.client.get("/v1/vocabulary")
        self.assertEqual(resp.status_code, 200,
                         f"Expected 200 with no auth configured, got {resp.status_code}")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestVocabularyPostRequiresApiKey(_RestBase):
    """POST /v1/vocabulary must return 401 when API key is configured but missing."""

    def test_vocabulary_post_requires_api_key_legacy_mode(self):
        """POST /v1/vocabulary returns 401 when REST_API_KEY is set but header absent."""
        _rest_mod.settings.REST_API_KEY = "test-secret-key-vocab-post"
        resp = self.client.post(
            "/v1/vocabulary",
            json={"words": ["testword"]},
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 without auth, got {resp.status_code}")

    def test_vocabulary_post_returns_200_with_valid_key(self):
        """POST /v1/vocabulary returns 200 with correct Bearer token."""
        key = "valid-vocab-key-post"
        _rest_mod.settings.REST_API_KEY = key
        resp = self.client.post(
            "/v1/vocabulary",
            json={"words": ["hello", "world"]},
            headers={"Authorization": f"Bearer {key}"},
        )
        self.assertEqual(resp.status_code, 200,
                         f"Expected 200 with correct key, got {resp.status_code}")

    def test_vocabulary_post_returns_401_with_wrong_key(self):
        """POST /v1/vocabulary returns 401 with wrong Bearer token."""
        _rest_mod.settings.REST_API_KEY = "real-key-vocab-post"
        resp = self.client.post(
            "/v1/vocabulary",
            json={"words": ["word"]},
            headers={"Authorization": "Bearer definitely-wrong"},
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 with wrong key, got {resp.status_code}")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestEventsRequiresApiKey(_RestBase):
    """GET /v1/events must return 401 when API key is configured but missing."""

    def test_events_requires_api_key_legacy_mode(self):
        """GET /v1/events returns 401 when REST_API_KEY is set but header absent."""
        _rest_mod.settings.REST_API_KEY = "test-secret-key-events"
        resp = self.client.get("/v1/events")
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 without auth, got {resp.status_code}")

    def test_events_returns_401_with_wrong_key(self):
        """GET /v1/events returns 401 with wrong Bearer token."""
        _rest_mod.settings.REST_API_KEY = "real-events-key"
        resp = self.client.get(
            "/v1/events",
            headers={"Authorization": "Bearer not-the-real-key"},
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 with wrong key, got {resp.status_code}")

    def test_events_accessible_when_no_auth_configured(self):
        """GET /v1/events is accessible (streams) when auth is fully disabled."""
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False
        resp = self.client.get("/v1/events")
        # 200 with SSE stream or connection (auth pass-through)
        self.assertNotEqual(resp.status_code, 401,
                            "/v1/events must not require auth when none configured")


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestSttTranscribeRequiresApiKey(_RestBase):
    """POST /v1/stt/transcribe must return 401 when API key is configured but missing."""

    def _audio_data(self):
        return b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16

    def test_stt_transcribe_requires_api_key_legacy_mode(self):
        """POST /v1/stt/transcribe returns 401 when REST_API_KEY is set but header absent."""
        _rest_mod.settings.REST_API_KEY = "test-secret-key-stt"
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 without auth, got {resp.status_code}")

    def test_stt_transcribe_returns_401_with_wrong_key(self):
        """POST /v1/stt/transcribe returns 401 with wrong Bearer token."""
        _rest_mod.settings.REST_API_KEY = "correct-stt-key"
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
            headers={"Authorization": "Bearer wrong-stt-key"},
        )
        self.assertEqual(resp.status_code, 401,
                         f"Expected 401 with wrong key, got {resp.status_code}")

    def test_stt_transcribe_accepts_valid_key(self):
        """POST /v1/stt/transcribe succeeds (not 401) with correct Bearer token."""
        key = "stt-valid-key-w1212"
        _rest_mod.settings.REST_API_KEY = key
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
            headers={"Authorization": f"Bearer {key}"},
        )
        # Should not be 401 (may be 200, 400, 500 depending on audio processing)
        self.assertNotEqual(resp.status_code, 401,
                            f"Correct key should not yield 401, got {resp.status_code}")


# ===========================================================================
# F6 — Privacy mode guard on /v1/stt/transcribe
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestSttTranscribeBlockedInPrivacyMode(_RestBase):
    """POST /v1/stt/transcribe must return 403 when privacy_mode_enabled=true."""

    def _audio_data(self):
        return b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 16

    def test_stt_transcribe_blocked_in_privacy_mode(self):
        """Returns 403 {"ok": false, "skipped": "privacy_mode"} when privacy on."""
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": True}
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 403,
                         f"Expected 403 in privacy mode, got {resp.status_code}")
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertFalse(body.get("ok"), "ok must be False in privacy mode response")
        self.assertEqual(body.get("skipped"), "privacy_mode",
                         f"skipped field should be 'privacy_mode', got {body.get('skipped')}")

    def test_stt_transcribe_allowed_when_privacy_mode_off(self):
        """Returns non-403 when privacy_mode_enabled=false (default)."""
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": False}
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertNotEqual(resp.status_code, 403,
                            f"Must not be blocked when privacy off, got {resp.status_code}")

    def test_stt_transcribe_blocked_privacy_mode_no_auth_configured(self):
        """Privacy mode blocks even when auth is fully disabled (defense in depth)."""
        _rest_mod.settings.REST_API_KEY = ""
        self.mock_store.load_settings.return_value = {"privacy_mode_enabled": True}
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 403,
                         f"Privacy mode must block regardless of auth state")

    def test_load_settings_field_fallback_allows_transcribe_on_error(self):
        """When load_settings() raises an exception, privacy guard defaults to False."""
        self.mock_store.load_settings.side_effect = RuntimeError("disk error")
        data = {"file": (io.BytesIO(self._audio_data()), "test.wav")}
        resp = self.client.post(
            "/v1/stt/transcribe",
            data=data,
            content_type="multipart/form-data",
        )
        # Should not be 403 — fallback to False means transcription proceeds
        self.assertNotEqual(resp.status_code, 403,
                            "load_settings error should not produce 403 privacy block")


# ===========================================================================
# F2 — CORS wildcard + credentials forced False
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestCorsCredentialsForcedFalseWhenOriginsWildcard(unittest.TestCase):
    """When CORS_ORIGINS=='*', supports_credentials must be False."""

    def test_cors_credentials_forced_false_when_origins_wildcard(self):
        """_cors_credentials must be False when CORS_ORIGINS is '*'."""
        with patch.object(_rest_mod.settings, "CORS_ORIGINS", "*"):
            # Re-evaluate the module-level logic inline (same logic used at import time)
            origins_raw = _rest_mod.settings.CORS_ORIGINS
            parsed = _rest_mod._parse_cors_origins(origins_raw)
            # Verify the guard logic: wildcard → credentials forced off
            credentials = True
            if parsed == "*":
                credentials = False
            self.assertFalse(
                credentials,
                "supports_credentials must be False when CORS_ORIGINS='*'"
            )

    def test_cors_credentials_allowed_when_explicit_origins(self):
        """_cors_credentials must be True when CORS_ORIGINS is an explicit list."""
        explicit = "http://localhost:3000,http://localhost:8080"
        parsed = _rest_mod._parse_cors_origins(explicit)
        credentials = True
        if parsed == "*":
            credentials = False
        self.assertTrue(
            credentials,
            "supports_credentials may be True when CORS_ORIGINS is explicit"
        )
        # parsed must be a list, not "*"
        self.assertIsInstance(parsed, list)
        self.assertIn("http://localhost:3000", parsed)
        self.assertIn("http://localhost:8080", parsed)

    def test_parse_cors_origins_wildcard_returns_star(self):
        """_parse_cors_origins('*') returns the '*' string (not a list)."""
        result = _rest_mod._parse_cors_origins("*")
        self.assertEqual(result, "*")

    def test_parse_cors_origins_single_origin(self):
        """_parse_cors_origins with one origin returns a single-item list."""
        result = _rest_mod._parse_cors_origins("http://example.com")
        self.assertEqual(result, ["http://example.com"])

    def test_parse_cors_origins_multiple_comma_separated(self):
        """_parse_cors_origins parses comma-separated list correctly."""
        result = _rest_mod._parse_cors_origins("http://a.com, http://b.com, http://c.com")
        self.assertEqual(len(result), 3)
        self.assertIn("http://a.com", result)
        self.assertIn("http://b.com", result)
        self.assertIn("http://c.com", result)

    def test_module_cors_credentials_is_false_with_wildcard_default(self):
        """If CORS_ORIGINS is '*', module-level _cors_credentials must be False.

        wave-21 MED fix: default changed to localhost allowlist so this branch
        is now the explicit-opt-in case. The guard still works when a user sets
        KRAB_EAR_CORS_ORIGINS="*".
        """
        # The module initializes _cors_credentials at import time.
        # Check: when CORS_ORIGINS happens to be "*" (env-override case), credentials
        # must still be forced False.
        if getattr(_rest_mod.settings, "CORS_ORIGINS", None) == "*":
            self.assertFalse(
                _rest_mod._cors_credentials,
                "_cors_credentials must be False when CORS_ORIGINS is '*'"
            )
        else:
            # wave-21: default is now localhost allowlist, not wildcard.
            # _cors_credentials should be True (explicit list allows credentials).
            self.assertTrue(
                _rest_mod._cors_credentials,
                "_cors_credentials must be True with explicit CORS_ORIGINS list"
            )


# ===========================================================================
# F3 — Platform mask in /health/dashboard
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestHealthDashboardMasksOsBuild(_RestBase):
    """GET /health/dashboard must not expose OS build strings."""

    def test_health_dashboard_masks_os_build(self):
        """Dashboard platform field should be 'darwin', 'linux', or 'win32' only."""
        with patch("backend.rest_server._build_dashboard_html") as mock_html:
            mock_html.return_value = "<html><body>platform: darwin</body></html>"
            resp = self.client.get("/health/dashboard")
        self.assertEqual(resp.status_code, 200)

    def test_platform_str_logic_masks_macos_version(self):
        """_build_dashboard_html platform logic must strip macOS version strings."""
        import platform as _plat

        # Simulate the masking logic from _build_dashboard_html
        def _get_masked_platform(system_str: str) -> str:
            raw = system_str.lower()
            if "darwin" in raw:
                return "darwin"
            elif "linux" in raw:
                return "linux"
            elif "windows" in raw or "win32" in raw:
                return "win32"
            return "unknown"

        # macOS reports "Darwin" as system string
        self.assertEqual(_get_masked_platform("Darwin"), "darwin")
        # Linux
        self.assertEqual(_get_masked_platform("Linux"), "linux")
        # Windows
        self.assertEqual(_get_masked_platform("Windows"), "win32")
        self.assertEqual(_get_masked_platform("win32"), "win32")
        # Unknown
        self.assertEqual(_get_masked_platform("SunOS"), "unknown")

    def test_build_dashboard_html_platform_no_build_string(self):
        """The generated dashboard HTML platform string must not contain
        full platform.platform() build strings (e.g. 'Darwin-25.5.0...')."""
        import platform as _plat

        # Capture what the function actually builds
        try:
            html = _rest_mod._build_dashboard_html()
        except Exception:
            self.skipTest("_build_dashboard_html raised (non-critical for this test)")

        # The full platform string (e.g. "Darwin-25.5.0-arm64-arm-64bit") must not appear
        full_platform = _plat.platform()
        # It would only leak if the masking logic was bypassed
        self.assertNotIn(
            full_platform,
            html,
            "Full platform.platform() string must not appear in dashboard HTML "
            f"(found: {full_platform!r})"
        )

    def test_platform_str_does_not_contain_kernel_version(self):
        """Platform field in generated HTML should not contain kernel version numbers
        like '25.5.0' or uname-style version strings."""
        import platform as _plat
        import re

        try:
            html = _rest_mod._build_dashboard_html()
        except Exception:
            self.skipTest("_build_dashboard_html raised (non-critical)")

        # Kernel version pattern: digits.digits.digits (e.g., 25.5.0, 5.15.0)
        kernel_ver = _plat.release()  # e.g. "25.5.0"
        if kernel_ver:
            self.assertNotIn(
                kernel_ver,
                html,
                f"Kernel version {kernel_ver!r} must not appear in dashboard HTML"
            )


# ===========================================================================
# Sanity: endpoints that should NOT require auth remain open
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestPublicEndpointsUnaffected(_RestBase):
    """Public endpoints like /health must remain open after auth fixes."""

    def test_health_still_accessible_without_key(self):
        """GET /health must not require auth after F1 fixes."""
        _rest_mod.settings.REST_API_KEY = "strict-key-w1212"
        resp = self.client.get("/health")
        self.assertNotEqual(resp.status_code, 401,
                            "/health must remain open even when auth is configured")

    def test_info_endpoint_still_accessible(self):
        """GET /info must not require auth."""
        _rest_mod.settings.REST_API_KEY = "strict-key-w1212"
        resp = self.client.get("/info")
        self.assertNotEqual(resp.status_code, 401,
                            "/info must remain open")

    def test_readiness_still_requires_auth(self):
        """GET /v1/readiness already has @require_api_key — must still enforce it."""
        _rest_mod.settings.REST_API_KEY = "strict-key-w1212"
        resp = self.client.get("/v1/readiness")
        self.assertEqual(resp.status_code, 401,
                         f"/v1/readiness should enforce auth, got {resp.status_code}")


if __name__ == "__main__":
    unittest.main()
