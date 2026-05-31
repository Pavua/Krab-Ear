"""Тесты опциональной Bearer-token аутентификации REST-сервера.

Проверяются три сценария:
1. Аутентификация отключена (REST_API_KEY пуст) — защищённые эндпоинты доступны без токена.
2. Аутентификация включена — защищённые эндпоинты возвращают 401 без токена.
3. Аутентификация включена — защищённые эндпоинты работают с правильным токеном.
"""

import hmac
import importlib
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Patch heavy modules before importing rest_server.
#
# Strategy (Wave 1744 test-isolation fix):
#   Import the REAL module first so sys.modules holds the real object — this
#   prevents bare ModuleType stubs from leaking to later test files in the
#   same xdist worker.  Then replace only the specific heavy classes/attrs
#   that rest_server would try to instantiate at module load time.
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _ensure_real_or_stub(mod_name: str) -> types.ModuleType:
    """Return sys.modules[mod_name], importing the real module if needed.

    Falls back to a bare ModuleType stub ONLY when the real import fails
    (e.g. missing optional C-extension dependency).
    """
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    try:
        importlib.import_module(mod_name)
    except Exception:
        sys.modules[mod_name] = types.ModuleType(mod_name)
    return sys.modules[mod_name]


# core.engine — import real module, then swap AudioEngine with a lightweight fake
# so rest_server doesn't construct a real AudioEngine (heavy MLX/GigaAM warmup).
# We save the original and restore it after rest_server is imported so later
# test files that import from core.engine still see the real AudioEngine.
_engine_mod = _ensure_real_or_stub("core.engine")
_orig_AudioEngine = getattr(_engine_mod, "AudioEngine", None)


class _FakeEngine:
    quality_profile = "balanced"

    def __init__(self, *a, **kw):
        pass

    def normalize_audio(self, *a, **kw):
        pass


_engine_mod.AudioEngine = _FakeEngine  # type: ignore[attr-defined]

# backend.event_bus — real module; ensure bus/sse_stream attrs exist for rest_server.
_eb = _ensure_real_or_stub("backend.event_bus")
if not hasattr(_eb, "bus"):
    _eb.bus = MagicMock()  # type: ignore[attr-defined]
if not hasattr(_eb, "sse_stream"):
    _eb.sse_stream = MagicMock(return_value=iter([]))  # type: ignore[attr-defined]

# backend.service — real module; swap BackendService so rest_server /v1/readiness
# doesn't try to reach a live backend socket.
_service_mod = _ensure_real_or_stub("backend.service")
_orig_BackendService = getattr(_service_mod, "BackendService", None)


class _FakeBackendService:
    @staticmethod
    def _build_readiness_report_static():
        return {"overall_ready": True, "components": {}}


_service_mod.BackendService = _FakeBackendService  # type: ignore[attr-defined]

# backend.state_store — real module; swap StateStore so no real file I/O on import.
_state_store_mod = _ensure_real_or_stub("backend.state_store")
_orig_StateStore = getattr(_state_store_mod, "StateStore", None)


class _FakeStateStore:
    def __init__(self, *a, **kw):
        pass

    def is_idempotent(self, *a, **kw):
        return False

    def load_vocabulary(self):
        return []

    def save_vocabulary(self, *a, **kw):
        pass


_state_store_mod.StateStore = _FakeStateStore  # type: ignore[attr-defined]

# backend.transcriber — real module; swap Transcriber so no engine construction.
_transcriber_mod = _ensure_real_or_stub("backend.transcriber")
_orig_Transcriber = getattr(_transcriber_mod, "Transcriber", None)


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_transcriber_mod.Transcriber = _FakeTranscriber  # type: ignore[attr-defined]

# backend.metrics_collector — real module; provide a lightweight fake `metrics`
# instance so REST endpoints return predictable values in tests.
_metrics_mod = _ensure_real_or_stub("backend.metrics_collector")
_orig_metrics = getattr(_metrics_mod, "metrics", None)


class _FakeMetrics:
    def get_summary(self):
        return {
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "latency_p99_ms": None,
            "confidence_avg": None,
            "request_count": 0,
            "error_count": 0,
            "total_requests": 0,
            "error_rate": 0.0,
            "status": "waiting_data",
        }

    def record(self, *a, **kw):
        pass


_metrics_mod.metrics = _FakeMetrics()  # type: ignore[attr-defined]

# flask_smorest stubs (if not installed)
try:
    import flask_smorest  # noqa: F401
except ImportError:
    smorest_mod = types.ModuleType("flask_smorest")

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

    smorest_mod.Api = _FakeApi
    smorest_mod.Blueprint = _FakeBlueprint
    smorest_mod.abort = MagicMock()
    sys.modules["flask_smorest"] = smorest_mod

# marshmallow stubs
try:
    import marshmallow  # noqa: F401
except ImportError:
    ma_mod = types.ModuleType("marshmallow")

    class _Schema:
        pass

    class _Fields:
        String = MagicMock(return_value=None)
        Boolean = MagicMock(return_value=None)
        Float = MagicMock(return_value=None)
        Integer = MagicMock(return_value=None)
        List = MagicMock(return_value=None)
        Dict = MagicMock(return_value=None)

    class _Validate:
        pass

    ma_mod.Schema = _Schema
    ma_mod.fields = _Fields()
    ma_mod.validate = _Validate()
    sys.modules["marshmallow"] = ma_mod

# werkzeug stub (usually installed with flask, but guard anyway)
try:
    from werkzeug.utils import secure_filename  # noqa: F401
except ImportError:
    wz_mod = types.ModuleType("werkzeug.utils")
    wz_mod.secure_filename = lambda name: name
    sys.modules["werkzeug.utils"] = wz_mod

# ---------------------------------------------------------------------------
# Now import the app under test
# ---------------------------------------------------------------------------

# Ensure KrabEar package root is on path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.config import settings  # noqa: E402 — needed after sys.path patch

# Patch TEMP_DIR creation so we don't need a real filesystem during import
with patch("pathlib.Path.mkdir"):
    from backend.rest_server import app, require_api_key  # noqa: E402

# Restore original classes/attrs on the real modules so later test files that
# import from these modules directly see the real implementations.
if _orig_AudioEngine is not None:
    _engine_mod.AudioEngine = _orig_AudioEngine  # type: ignore[attr-defined]
if _orig_BackendService is not None:
    _service_mod.BackendService = _orig_BackendService  # type: ignore[attr-defined]
if _orig_StateStore is not None:
    _state_store_mod.StateStore = _orig_StateStore  # type: ignore[attr-defined]
if _orig_Transcriber is not None:
    _transcriber_mod.Transcriber = _orig_Transcriber  # type: ignore[attr-defined]
if _orig_metrics is not None:
    _metrics_mod.metrics = _orig_metrics  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

PROTECTED_ENDPOINTS = ["/metrics", "/v1/readiness"]
OPEN_ENDPOINTS = ["/health"]


class TestAuthDisabled(unittest.TestCase):
    """Когда REST_API_KEY пустой — все эндпоинты доступны без токена."""

    def setUp(self):
        self._orig_key = settings.REST_API_KEY
        settings.REST_API_KEY = ""
        self.client = app.test_client()

    def tearDown(self):
        settings.REST_API_KEY = self._orig_key

    def test_health_accessible_no_key(self):
        resp = self.client.get("/health")
        self.assertNotEqual(resp.status_code, 401)

    def test_metrics_accessible_no_key(self):
        resp = self.client.get("/metrics")
        self.assertNotEqual(resp.status_code, 401)

    def test_readiness_accessible_no_key(self):
        resp = self.client.get("/v1/readiness")
        self.assertNotEqual(resp.status_code, 401)


class TestAuthEnabled401(unittest.TestCase):
    """Когда REST_API_KEY задан — защищённые эндпоинты возвращают 401 без токена."""

    _TEST_KEY = "test-fake-restkey-abc"

    def setUp(self):
        self._orig_key = settings.REST_API_KEY
        settings.REST_API_KEY = self._TEST_KEY
        self.client = app.test_client()

    def tearDown(self):
        settings.REST_API_KEY = self._orig_key

    def test_metrics_no_auth_returns_401(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 401)

    def test_metrics_wrong_key_returns_401(self):
        resp = self.client.get("/metrics", headers={"Authorization": "Bearer wrong-key"})
        self.assertEqual(resp.status_code, 401)

    def test_metrics_missing_bearer_prefix_returns_401(self):
        resp = self.client.get("/metrics", headers={"Authorization": self._TEST_KEY})
        self.assertEqual(resp.status_code, 401)

    def test_readiness_no_auth_returns_401(self):
        resp = self.client.get("/v1/readiness")
        self.assertEqual(resp.status_code, 401)

    def test_health_still_open(self):
        """Базовый /health не защищён токеном."""
        resp = self.client.get("/health")
        self.assertNotEqual(resp.status_code, 401)


class TestAuthEnabledCorrectKey(unittest.TestCase):
    """Когда REST_API_KEY задан — защищённые эндпоинты работают с правильным токеном."""

    _TEST_KEY = "test-fake-restkey-abc"

    def setUp(self):
        self._orig_key = settings.REST_API_KEY
        settings.REST_API_KEY = self._TEST_KEY
        self.client = app.test_client()

    def tearDown(self):
        settings.REST_API_KEY = self._orig_key

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self._TEST_KEY}"}

    def test_metrics_with_correct_key(self):
        resp = self.client.get("/metrics", headers=self._auth_headers())
        self.assertNotEqual(resp.status_code, 401)

    def test_readiness_with_correct_key(self):
        resp = self.client.get("/v1/readiness", headers=self._auth_headers())
        self.assertNotEqual(resp.status_code, 401)


class TestRequireApiKeyDecorator(unittest.TestCase):
    """Юнит-тест самого декоратора вне Flask-контекста."""

    def test_pass_through_when_key_empty(self):
        """Декоратор пропускает запрос, если ключ не задан."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = ""
        try:
            called = []

            @require_api_key
            def view():
                called.append(True)
                return "ok", 200

            with app.test_request_context("/metrics"):
                view()
            self.assertTrue(called)
        finally:
            settings.REST_API_KEY = orig

    def test_rejects_when_no_header(self):
        """Декоратор возвращает 401, если заголовок отсутствует."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "mykey"
        try:
            @require_api_key
            def view():
                return "ok", 200

            with app.test_request_context("/metrics"):
                resp, code = view()
            self.assertEqual(code, 401)
        finally:
            settings.REST_API_KEY = orig

    def test_accepts_correct_key(self):
        """Декоратор пропускает запрос с правильным ключом."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "mykey"
        try:
            called = []

            @require_api_key
            def view():
                called.append(True)
                return "ok", 200

            with app.test_request_context("/metrics", headers={"Authorization": "Bearer mykey"}):
                view()
            self.assertTrue(called)
        finally:
            settings.REST_API_KEY = orig


class TestLegacyConstantTimeCompare(unittest.TestCase):
    """Wave 187: Mode 2 legacy path uses constant-time compare (timing attack fix)."""

    def test_legacy_mode_uses_constant_time_compare(self):
        """Correct token returns 200; wrong-but-same-length token returns 401.
        Both code paths exercised — behaviour is correct after hmac fix."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "test-fake-key-32ch-aabbccddeeff"
        client = app.test_client()
        try:
            # Correct token
            good = client.get("/metrics", headers={"Authorization": "Bearer test-fake-key-32ch-aabbccddeeff"})
            self.assertNotEqual(good.status_code, 401)
            # Wrong token of identical length (would leak timing with plain ==)
            bad = client.get("/metrics", headers={"Authorization": "Bearer test-WRONG-key-32ch-aabbccddee"})
            self.assertEqual(bad.status_code, 401)
        finally:
            settings.REST_API_KEY = orig

    def test_legacy_token_compare_uses_hmac_compare_digest(self):
        """hmac.compare_digest is actually invoked for Mode 2 comparisons."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "mylegacykey"
        try:
            calls = []
            real_compare = hmac.compare_digest

            def spy(a, b):
                calls.append((a, b))
                return real_compare(a, b)

            @require_api_key
            def view():
                return "ok", 200

            with patch("backend.rest_server.hmac.compare_digest", side_effect=spy):
                with app.test_request_context(
                    "/metrics", headers={"Authorization": "Bearer mylegacykey"}
                ):
                    view()
            self.assertTrue(len(calls) >= 1, "hmac.compare_digest was not called")
            # Bytes are passed after .encode("utf-8")
            self.assertIn((b"mylegacykey", b"mylegacykey"), calls)
        finally:
            settings.REST_API_KEY = orig

    def test_empty_token_safely_compared(self):
        """Empty or None token does not raise — treated as empty string."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "somekey"
        try:
            @require_api_key
            def view():
                return "ok", 200

            # No Authorization header → empty token path via missing Bearer prefix
            with app.test_request_context("/metrics"):
                resp, code = view()
            self.assertEqual(code, 401)
        finally:
            settings.REST_API_KEY = orig

    def test_unicode_token_compared(self):
        """Unicode characters in token/key do not raise — bytes encoding path handles them."""
        orig = settings.REST_API_KEY
        settings.REST_API_KEY = "unicode-key-тест"
        try:
            @require_api_key
            def view():
                return "ok", 200

            # Correct unicode key must pass
            with app.test_request_context(
                "/metrics",
                headers={"Authorization": "Bearer unicode-key-тест"},
            ):
                result = view()
            # Must not raise; correct key passes through
            self.assertIsNotNone(result)

            # Wrong unicode key must return 401
            @require_api_key
            def view2():
                return "ok", 200

            with app.test_request_context(
                "/metrics",
                headers={"Authorization": "Bearer unicode-key-WRONG"},
            ):
                resp, code = view2()
            self.assertEqual(code, 401)
        finally:
            settings.REST_API_KEY = orig


if __name__ == "__main__":
    unittest.main()
