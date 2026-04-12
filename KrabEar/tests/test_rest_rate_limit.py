"""Тесты rate limiting для REST-сервера Krab Ear.

Проверяет:
- Наличие заголовков X-RateLimit-* в ответах при включённом лимитере
- 429 при превышении лимита
- Отключение лимитов через limiter.enabled = False
"""

import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Stub heavy dependencies BEFORE importing rest_server (same pattern as
# test_rest_logging.py) — only needed if not yet imported in this process.
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


_state_store_mod.StateStore = _FakeStateStore
sys.modules.setdefault("backend.state_store", _state_store_mod)

# backend.transcriber stub
_transcriber_mod = types.ModuleType("backend.transcriber")


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_transcriber_mod.Transcriber = _FakeTranscriber
sys.modules.setdefault("backend.transcriber", _transcriber_mod)

# backend.metrics_collector stub
_metrics_mod = types.ModuleType("backend.metrics_collector")


class _FakeMetrics:
    def get_summary(self):
        return {"status": "waiting_data"}

    def record(self, *a, **kw):
        pass


_metrics_mod.metrics = _FakeMetrics()
sys.modules.setdefault("backend.metrics_collector", _metrics_mod)

# flask_smorest stub (if not installed)
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
            def deco(f):
                return f
            return deco

        def response(self, *a, **kw):
            def deco(f):
                return f
            return deco

        def alt_response(self, *a, **kw):
            def deco(f):
                return f
            return deco

        def arguments(self, *a, **kw):
            def deco(f):
                return f
            return deco

    _smorest_mod.Api = _FakeApi
    _smorest_mod.Blueprint = _FakeBlueprint
    _smorest_mod.abort = MagicMock()
    sys.modules["flask_smorest"] = _smorest_mod

# flask_sock stub (if not installed)
try:
    import flask_sock  # noqa: F401
except ImportError:
    _sock_mod = types.ModuleType("flask_sock")

    class _FakeSock:
        def __init__(self, app):
            pass

        def route(self, *a, **kw):
            def deco(f):
                return f
            return deco

    _sock_mod.Sock = _FakeSock
    sys.modules["flask_sock"] = _sock_mod

# ---------------------------------------------------------------------------
# Import app under test
# ---------------------------------------------------------------------------
from core.config import settings  # noqa: E402

with patch("pathlib.Path.mkdir"):
    import backend.rest_server as rest_mod  # noqa: E402

_app = rest_mod.app
_limiter = rest_mod.limiter


# ---------------------------------------------------------------------------
# Helper: reset limiter storage between tests so counts don't carry over
# ---------------------------------------------------------------------------

def _reset_limiter():
    """Очищает счётчики in-memory limiter'а между тестами."""
    try:
        _limiter.reset()
    except Exception:
        pass
    try:
        # flask-limiter 3.x: _storage.reset()
        _limiter._storage.reset()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestRateLimitHeaders(unittest.TestCase):
    """Заголовки X-RateLimit-* должны присутствовать когда лимитер включён."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()
        # Убеждаемся что лимитер включён
        _limiter.enabled = True
        _reset_limiter()

    def test_health_returns_ratelimit_headers(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        headers = {k.lower(): v for k, v in resp.headers}
        self.assertIn("x-ratelimit-limit", headers,
                      f"Expected X-RateLimit-Limit header; got: {list(resp.headers)}")
        self.assertIn("x-ratelimit-remaining", headers)

    def test_metrics_returns_ratelimit_headers(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        headers = {k.lower(): v for k, v in resp.headers}
        self.assertIn("x-ratelimit-limit", headers)


class TestRateLimitExceeded(unittest.TestCase):
    """429 при превышении лимита с корректным JSON-телом."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()
        _limiter.enabled = True
        _reset_limiter()

    def _exhaust_and_hit_limit(self, url, limit_per_minute):
        """Делает limit запросов (должны пройти), затем ещё один (должен вернуть 429)."""
        for i in range(limit_per_minute):
            resp = self.client.get(url)
            self.assertNotEqual(resp.status_code, 429,
                                f"Got 429 too early at request {i + 1} (limit={limit_per_minute})")
        return self.client.get(url)

    def test_default_limit_produces_ratelimit_headers(self):
        """Проверяем что заголовки присутствуют (не тестируем точный лимит)."""
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        # Проверяем X-RateLimit-Limit существует и является числом
        limit_header = resp.headers.get("X-RateLimit-Limit")
        self.assertIsNotNone(limit_header, "X-RateLimit-Limit header missing")

    def test_429_json_body_structure(self):
        """429 должен возвращать JSON с error и retry_after."""
        # Тестируем обработчик ошибки напрямую
        with _app.test_request_context("/health"):
            from flask import jsonify as flask_jsonify
            # Тестируем сам обработчик ошибки
            mock_exc = MagicMock()
            mock_exc.description.retry_after.total_seconds.return_value = 45.3
            resp = rest_mod._rate_limit_exceeded_handler(mock_exc)
            import json
            data = json.loads(resp.get_data(as_text=True))
            self.assertEqual(data["error"], "rate_limit_exceeded")
            self.assertEqual(data["retry_after"], 46)  # ceil(45.3)
            self.assertEqual(resp.status_code, 429)
            self.assertIn("Retry-After", resp.headers)

    def test_429_retry_after_header_present(self):
        """Retry-After заголовок должен присутствовать в 429."""
        with _app.test_request_context("/health"):
            mock_exc = MagicMock()
            mock_exc.description.retry_after.total_seconds.return_value = 30.0
            resp = rest_mod._rate_limit_exceeded_handler(mock_exc)
            self.assertEqual(resp.status_code, 429)
            self.assertIn("Retry-After", resp.headers)
            self.assertEqual(resp.headers["Retry-After"], "30")

    def test_429_handler_fallback_on_bad_exception(self):
        """При неожиданной структуре исключения retry_after должен быть 60."""
        with _app.test_request_context("/health"):
            mock_exc = MagicMock()
            # description.retry_after.total_seconds() выбрасывает исключение
            mock_exc.description.retry_after.total_seconds.side_effect = AttributeError("no attr")
            resp = rest_mod._rate_limit_exceeded_handler(mock_exc)
            import json
            data = json.loads(resp.get_data(as_text=True))
            self.assertEqual(data["error"], "rate_limit_exceeded")
            self.assertEqual(data["retry_after"], 60)


class TestRateLimitDisabled(unittest.TestCase):
    """Когда limiter.enabled = False — запросы проходят без 429."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()
        _limiter.enabled = False
        _reset_limiter()

    def tearDown(self):
        # Восстанавливаем состояние для других тестов
        _limiter.enabled = settings.RATE_LIMIT_ENABLED

    def test_no_429_when_disabled(self):
        """Много запросов подряд не должны давать 429 когда лимитер выключен."""
        for i in range(10):
            resp = self.client.get("/health")
            self.assertNotEqual(resp.status_code, 429,
                                f"Unexpected 429 on request {i + 1} with limiter disabled")

    def test_no_ratelimit_headers_when_disabled(self):
        """flask-limiter не добавляет X-RateLimit-* заголовки когда выключен."""
        resp = self.client.get("/health")
        headers = {k.lower(): v for k, v in resp.headers}
        self.assertNotIn("x-ratelimit-limit", headers,
                         "X-RateLimit-Limit header present even though limiter is disabled")


class TestRateLimitConfig(unittest.TestCase):
    """Проверяет что RATE_LIMIT_ENABLED добавлен в настройки."""

    def test_settings_has_rate_limit_enabled(self):
        self.assertTrue(hasattr(settings, "RATE_LIMIT_ENABLED"),
                        "settings.RATE_LIMIT_ENABLED missing from core/config.py")

    def test_rate_limit_enabled_default_is_true(self):
        from core.config import Settings
        s = Settings()
        self.assertTrue(s.RATE_LIMIT_ENABLED)

    def test_rest_server_has_limiter(self):
        self.assertIsNotNone(rest_mod.limiter,
                             "rest_server.limiter not defined")


if __name__ == "__main__":
    unittest.main()
