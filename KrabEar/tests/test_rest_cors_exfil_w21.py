"""wave-21 MED: REST server CORS transcript-exfiltration fix tests.

Finding: default CORS_ORIGINS="*" allowed any web page on the user's machine
to open EventSource("http://127.0.0.1:5005/v1/events") and read live
transcripts cross-origin.  The localhost bind does NOT defend against this
because the browser itself runs on localhost.

Fix:
  1. core/config.py — default CORS_ORIGINS changed from "*" to
     "http://127.0.0.1,http://localhost" (explicit allowlist).
  2. rest_server.py — _is_origin_allowed() + @_block_cross_origin_reads
     decorator applied to /v1/events and /v1/vocabulary; inline Origin-gate
     applied to /ws/events.

Tests:
  - Default CORS_ORIGINS is NOT "*"
  - _is_origin_allowed(): evil origin rejected, localhost origins permitted
  - /v1/events: evil Origin → 403; absent/localhost Origin → not 403
  - /v1/vocabulary GET: evil Origin → 403; absent Origin → not 403
  - /v1/vocabulary GET: even when CORS_ORIGINS forced to "*", evil Origin → 403
  - Existing same-origin flow unaffected

Run:
    cd "/Users/pablito/Antigravity_AGENTS/Krab Ear" && \\
    PYTHONPATH="$(pwd)/KrabEar" .venv_krab_ear/bin/python -m pytest \\
    KrabEar/tests/test_rest_cors_exfil_w21.py -p no:xdist -q
"""
from __future__ import annotations

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
# Lazy REST server import with minimal stubs
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
    (sibling of the red CI 2026-07-12 chunk-pollution class fixed in
    test_rest_server_w1212.py / test_rest_wave31_hardening.py — same
    unguarded stub pattern, see CLAUDE.md).
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
                "load_vocabulary": lambda self: ["hello", "world"],
                "save_vocabulary": lambda self, *a, **k: None,
                "add_history_item": lambda self, **kw: MagicMock(id="hist-w21"),
                "load_settings": lambda self: {"privacy_mode_enabled": False},
            }),
        },
        "backend.transcriber": {
            "Transcriber": type("_FT", (), {
                "__init__": lambda self, *a, **k: None,
                "transcribe": lambda self, *a, **kw: {
                    "text": "w21 test",
                    "raw_text": "w21 test",
                    "confidence": 0.9,
                    "duration_ms": 300,
                    "engine": "mlx-whisper",
                    "model": "whisper-small",
                    "language": "ru",
                    "segments": [],
                    "diarization": {},
                },
            }),
        },
        "backend.metrics_collector": {
            "metrics": type("_FM", (), {
                "get_summary": lambda self: {
                    "total_requests": 0, "error_rate": 0.0,
                    "error_count": 0, "request_count": 0,
                    "status": "waiting_data", "stt_metrics": {}, "window_size": 0,
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
except Exception:
    pass
finally:
    # Снимаем ВСТАВЛЕННЫЕ НАМИ фейки из sys.modules — иначе фейк _FBS/_FSS
    # отравляет все последующие тест-файлы чанка (см. test_rest_server_w1212.py).
    for _name in _inserted_stub_modules:
        sys.modules.pop(_name, None)


# ---------------------------------------------------------------------------
# Config-level tests (no Flask needed)
# ---------------------------------------------------------------------------

class TestCorsOriginsDefault(unittest.TestCase):
    """The CORS_ORIGINS default must NOT be "*" after wave-21 fix."""

    def test_cors_origins_default_not_wildcard(self):
        """Default CORS_ORIGINS must not be '*' — wildcard enables transcript exfiltration."""
        import os as _os
        from core.config import Settings
        with patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop("KRAB_EAR_CORS_ORIGINS", None)
            s = Settings()
        self.assertNotEqual(
            s.CORS_ORIGINS, "*",
            "CORS_ORIGINS default must not be '*' (wave-21 MED fix): "
            "wildcard allows any browser page to read transcripts via EventSource"
        )

    def test_cors_origins_default_includes_localhost(self):
        """Default CORS_ORIGINS must include localhost variants."""
        import os as _os
        from core.config import Settings
        with patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop("KRAB_EAR_CORS_ORIGINS", None)
            s = Settings()
        # Must cover at least 127.0.0.1 so native local clients work
        self.assertIn(
            "127.0.0.1", s.CORS_ORIGINS,
            "CORS_ORIGINS default must include 127.0.0.1"
        )

    def test_cors_origins_env_override_to_wildcard_still_works(self):
        """Operator can still opt-in to '*' via KRAB_EAR_CORS_ORIGINS env var."""
        import os as _os
        from core.config import Settings
        with patch.dict(_os.environ, {"KRAB_EAR_CORS_ORIGINS": "*"}):
            s = Settings()
        self.assertEqual(s.CORS_ORIGINS, "*",
                         "env override to '*' must be respected")


# ---------------------------------------------------------------------------
# _is_origin_allowed() unit tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestIsOriginAllowed(unittest.TestCase):
    """Unit tests for _is_origin_allowed() with various config states."""

    def test_evil_origin_rejected_when_wildcard_config(self):
        """Evil origin must be rejected even when CORS_ORIGINS is '*'."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertFalse(
                _rest_mod._is_origin_allowed("http://evil.test"),
                "evil.test must be blocked even when _cors_origins='*'"
            )

    def test_localhost_allowed_when_wildcard_config(self):
        """http://localhost must be allowed when CORS_ORIGINS is '*'."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://localhost"),
                "http://localhost must be allowed under wildcard config"
            )

    def test_127_0_0_1_allowed_when_wildcard_config(self):
        """http://127.0.0.1 must be allowed when CORS_ORIGINS is '*'."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://127.0.0.1"),
                "http://127.0.0.1 must be allowed under wildcard config"
            )

    def test_localhost_with_port_allowed_when_wildcard_config(self):
        """http://localhost:3000 must be allowed (dev server)."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://localhost:3000"),
                "http://localhost:3000 must be allowed under wildcard config"
            )

    def test_127_0_0_1_with_port_allowed_when_wildcard_config(self):
        """http://127.0.0.1:5005 must be allowed."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://127.0.0.1:5005"),
                "http://127.0.0.1:5005 must be allowed under wildcard config"
            )

    def test_empty_origin_always_allowed(self):
        """Empty Origin (no browser / non-browser client) must always pass."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertTrue(
                _rest_mod._is_origin_allowed(""),
                "empty Origin must always be allowed (non-browser caller)"
            )
        # Also with explicit list
        with patch.object(_rest_mod, "_cors_origins", ["http://localhost"]):
            self.assertTrue(
                _rest_mod._is_origin_allowed(""),
                "empty Origin must always be allowed with explicit list too"
            )

    def test_evil_origin_rejected_with_explicit_list(self):
        """Evil origin rejected when CORS_ORIGINS is an explicit list."""
        explicit_list = ["http://localhost", "http://127.0.0.1"]
        with patch.object(_rest_mod, "_cors_origins", explicit_list):
            self.assertFalse(
                _rest_mod._is_origin_allowed("http://evil.test"),
                "evil.test must be blocked with explicit allowlist"
            )

    def test_allowed_origin_passes_with_explicit_list(self):
        """Allowed origin passes with explicit list."""
        explicit_list = ["http://localhost", "http://127.0.0.1"]
        with patch.object(_rest_mod, "_cors_origins", explicit_list):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://localhost"),
                "http://localhost must pass with explicit list"
            )

    def test_trailing_slash_normalized(self):
        """Origin with trailing slash is normalized before comparison."""
        explicit_list = ["http://localhost"]
        with patch.object(_rest_mod, "_cors_origins", explicit_list):
            self.assertTrue(
                _rest_mod._is_origin_allowed("http://localhost/"),
                "trailing slash should be stripped before comparison"
            )

    def test_https_evil_origin_rejected(self):
        """HTTPS evil origin is also rejected."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            self.assertFalse(
                _rest_mod._is_origin_allowed("https://evil.test"),
                "https://evil.test must be rejected"
            )


# ---------------------------------------------------------------------------
# HTTP endpoint integration tests
# ---------------------------------------------------------------------------

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestEventsExfilGuard(unittest.TestCase):
    """GET /v1/events: evil Origin is blocked, localhost + absent Origin pass."""

    def setUp(self):
        self._mock_store = MagicMock()
        self._mock_store.load_vocabulary.return_value = []
        self._mock_store.load_settings.return_value = {"privacy_mode_enabled": False}
        self._mock_metrics = MagicMock()
        self._mock_engine = MagicMock()
        self._mock_engine.quality_profile = "balanced"
        self._mock_engine._router = None

        self._patches = [
            patch.object(_rest_mod, "store", self._mock_store),
            patch.object(_rest_mod, "metrics", self._mock_metrics),
            patch.object(_rest_mod, "engine", self._mock_engine),
        ]
        for p in self._patches:
            p.start()
        _rest_mod.limiter.enabled = False
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False

        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_evil_origin_returns_403_on_events(self):
        """GET /v1/events from evil.test Origin must be blocked (403)."""
        resp = self.client.get(
            "/v1/events",
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual(
            resp.status_code, 403,
            f"Expected 403 for evil Origin on /v1/events, got {resp.status_code}"
        )

    def test_absent_origin_not_blocked_on_events(self):
        """GET /v1/events without Origin header (curl/native) must not be blocked."""
        resp = self.client.get("/v1/events")
        # Should not be 403 — native clients have no Origin
        self.assertNotEqual(
            resp.status_code, 403,
            "No-Origin request to /v1/events must not be blocked"
        )

    def test_localhost_origin_not_blocked_on_events(self):
        """GET /v1/events from http://localhost Origin must be allowed."""
        resp = self.client.get(
            "/v1/events",
            headers={"Origin": "http://localhost"},
        )
        self.assertNotEqual(
            resp.status_code, 403,
            "http://localhost Origin must not be blocked on /v1/events"
        )

    def test_127_0_0_1_origin_not_blocked_on_events(self):
        """GET /v1/events from http://127.0.0.1 Origin must be allowed."""
        resp = self.client.get(
            "/v1/events",
            headers={"Origin": "http://127.0.0.1"},
        )
        self.assertNotEqual(
            resp.status_code, 403,
            "http://127.0.0.1 Origin must not be blocked on /v1/events"
        )

    def test_evil_origin_still_blocked_when_cors_origins_forced_wildcard(self):
        """Even when _cors_origins is forced to '*', evil Origin is blocked."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            resp = self.client.get(
                "/v1/events",
                headers={"Origin": "http://evil.test"},
            )
        self.assertEqual(
            resp.status_code, 403,
            "Evil Origin must be blocked even under '*' config"
        )


@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestVocabularyExfilGuard(unittest.TestCase):
    """GET /v1/vocabulary: evil Origin is blocked, absent/localhost pass."""

    def setUp(self):
        self._mock_store = MagicMock()
        self._mock_store.load_vocabulary.return_value = ["secret_word"]
        self._mock_store.load_settings.return_value = {"privacy_mode_enabled": False}
        self._mock_metrics = MagicMock()
        self._mock_engine = MagicMock()
        self._mock_engine.quality_profile = "balanced"
        self._mock_engine._router = None

        self._patches = [
            patch.object(_rest_mod, "store", self._mock_store),
            patch.object(_rest_mod, "metrics", self._mock_metrics),
            patch.object(_rest_mod, "engine", self._mock_engine),
        ]
        for p in self._patches:
            p.start()
        _rest_mod.limiter.enabled = False
        _rest_mod.settings.REST_API_KEY = ""
        if hasattr(_rest_mod.settings, "REST_API_AUTH_ENABLED"):
            _rest_mod.settings.REST_API_AUTH_ENABLED = False

        _rest_mod.app.config["TESTING"] = True
        self.client = _rest_mod.app.test_client()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_evil_origin_returns_403_on_vocabulary_get(self):
        """GET /v1/vocabulary from evil.test Origin must be blocked (403)."""
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual(
            resp.status_code, 403,
            f"Expected 403 for evil Origin on GET /v1/vocabulary, got {resp.status_code}"
        )

    def test_absent_origin_not_blocked_on_vocabulary(self):
        """GET /v1/vocabulary without Origin header must not be blocked."""
        resp = self.client.get("/v1/vocabulary")
        self.assertNotEqual(
            resp.status_code, 403,
            "No-Origin request to GET /v1/vocabulary must not be blocked"
        )

    def test_localhost_origin_not_blocked_on_vocabulary(self):
        """GET /v1/vocabulary from http://localhost must be allowed."""
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Origin": "http://localhost"},
        )
        self.assertNotEqual(
            resp.status_code, 403,
            "http://localhost Origin must not be blocked on GET /v1/vocabulary"
        )

    def test_evil_origin_still_blocked_when_cors_origins_forced_wildcard(self):
        """Even when _cors_origins is '*', evil Origin is blocked on vocabulary."""
        with patch.object(_rest_mod, "_cors_origins", "*"):
            resp = self.client.get(
                "/v1/vocabulary",
                headers={"Origin": "http://evil.test"},
            )
        self.assertEqual(
            resp.status_code, 403,
            "Evil Origin must be blocked on /v1/vocabulary even under '*' config"
        )

    def test_evil_origin_403_body_is_json(self):
        """403 response for evil Origin must have JSON body."""
        resp = self.client.get(
            "/v1/vocabulary",
            headers={"Origin": "http://evil.test"},
        )
        self.assertEqual(resp.status_code, 403)
        body = resp.get_json()
        self.assertIsNotNone(body, "403 response must have JSON body")
        self.assertIn("error", body, "403 JSON body must have 'error' field")


if __name__ == "__main__":
    unittest.main()
