"""Тесты JSON структурированного логирования REST-сервера.

Проверяют:
1. JSON формат лога при LOG_FORMAT=json (поля ts, method, path, status, duration_ms, ip)
2. Текстовый формат лога при LOG_FORMAT=text (без JSON)
3. Наличие заголовка X-Request-ID в каждом ответе
4. Уникальность Request-ID между запросами
5. Корректность полей JSON-лога (типы, значения)
"""

import json
import sys
import os
import types
import logging
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup (must precede all local imports)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Stub heavy / optional dependencies BEFORE importing rest_server
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

# flask_smorest stub
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

# flask_limiter stub
try:
    import flask_limiter  # noqa: F401
except ImportError:
    _limiter_mod = types.ModuleType("flask_limiter")
    _limiter_util_mod = types.ModuleType("flask_limiter.util")

    class _FakeLimiter:
        def __init__(self, *a, **kw):
            pass

        def limit(self, *a, **kw):
            def deco(f):
                return f
            return deco

    _limiter_mod.Limiter = _FakeLimiter
    _limiter_util_mod.get_remote_address = lambda: "127.0.0.1"
    sys.modules["flask_limiter"] = _limiter_mod
    sys.modules["flask_limiter.util"] = _limiter_util_mod

# flask_sock stub
try:
    import flask_sock  # noqa: F401
except ImportError:
    _sock_mod = types.ModuleType("flask_sock")

    class _FakeSock:
        def __init__(self, *a, **kw):
            pass

        def route(self, *a, **kw):
            def deco(f):
                return f
            return deco

    _sock_mod.Sock = _FakeSock
    sys.modules["flask_sock"] = _sock_mod

# marshmallow stub
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

    class _Validate:
        pass

    _ma_mod.Schema = _Schema
    _ma_mod.fields = _Fields()
    _ma_mod.validate = _Validate()
    sys.modules["marshmallow"] = _ma_mod

# werkzeug stub
try:
    from werkzeug.utils import secure_filename  # noqa: F401
except ImportError:
    _wz_mod = types.ModuleType("werkzeug.utils")
    _wz_mod.secure_filename = lambda name: name
    sys.modules["werkzeug.utils"] = _wz_mod

# ---------------------------------------------------------------------------
# Import app under test (with patched filesystem ops)
# ---------------------------------------------------------------------------
from core.config import settings  # noqa: E402

with patch("pathlib.Path.mkdir"):
    import backend.rest_server as rest_mod  # noqa: E402
    _app = rest_mod.app


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestRestLoggingJSON(unittest.TestCase):
    """LOG_FORMAT=json: log_request emits valid JSON lines."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()
        self.log_stream = StringIO()
        self.handler = logging.StreamHandler(self.log_stream)
        self.handler.setLevel(logging.DEBUG)
        rest_mod.logger.addHandler(self.handler)
        rest_mod.logger.setLevel(logging.DEBUG)
        self._orig_format = rest_mod.settings.LOG_FORMAT
        rest_mod.settings.LOG_FORMAT = "json"

    def tearDown(self):
        rest_mod.logger.removeHandler(self.handler)
        rest_mod.settings.LOG_FORMAT = self._orig_format

    def _last_json_log(self):
        self.handler.flush()
        lines = [l for l in self.log_stream.getvalue().splitlines() if l.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def test_json_log_has_required_fields(self):
        """JSON log contains all required fields."""
        self.client.get("/health")
        record = self._last_json_log()
        self.assertIsNotNone(record, "No JSON log record found")
        for field in ("ts", "request_id", "method", "path", "status", "duration_ms", "ip", "content_length"):
            self.assertIn(field, record, f"Missing field: {field}")

    def test_json_log_field_types_and_values(self):
        """JSON log field types match specification."""
        self.client.get("/health")
        record = self._last_json_log()
        self.assertIsNotNone(record)
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/health")
        self.assertEqual(record["status"], 200)
        self.assertIsInstance(record["duration_ms"], int)
        self.assertGreaterEqual(record["duration_ms"], 0)
        # ts must be ISO8601 string ending with Z
        self.assertTrue(record["ts"].endswith("Z"), f"ts not ISO8601Z: {record['ts']}")
        # request_id must look like a UUID (contains hyphens)
        self.assertIn("-", record["request_id"])

    def test_json_log_status_reflects_actual_response(self):
        """JSON log status matches the HTTP response status code."""
        resp = self.client.get("/nonexistent_route_xyz")
        record = self._last_json_log()
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], resp.status_code)

    def test_json_log_post_includes_content_length(self):
        """JSON log for POST includes content_length field (int, >= 0)."""
        self.client.post("/v1/vocabulary", json={"words": ["тест"]},
                         content_type="application/json")
        record = self._last_json_log()
        self.assertIsNotNone(record)
        self.assertIsInstance(record["content_length"], int)
        self.assertGreaterEqual(record["content_length"], 0)


class TestRestLoggingText(unittest.TestCase):
    """LOG_FORMAT=text: log_request does NOT emit JSON."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()
        self.log_stream = StringIO()
        self.handler = logging.StreamHandler(self.log_stream)
        self.handler.setLevel(logging.DEBUG)
        rest_mod.logger.addHandler(self.handler)
        rest_mod.logger.setLevel(logging.DEBUG)
        self._orig_format = rest_mod.settings.LOG_FORMAT
        rest_mod.settings.LOG_FORMAT = "text"

    def tearDown(self):
        rest_mod.logger.removeHandler(self.handler)
        rest_mod.settings.LOG_FORMAT = self._orig_format

    def test_text_log_is_not_json(self):
        """In text mode, log lines are plain text, not JSON objects."""
        self.client.get("/health")
        self.handler.flush()
        lines = [l for l in self.log_stream.getvalue().splitlines() if l.strip()]
        self.assertTrue(len(lines) > 0, "No log lines emitted")
        last = lines[-1]
        try:
            json.loads(last)
            self.fail(f"Expected non-JSON log line in text mode, got: {last}")
        except json.JSONDecodeError:
            pass  # expected

    def test_text_log_contains_path(self):
        """In text mode, log line includes the request path."""
        self.client.get("/health")
        self.handler.flush()
        self.assertIn("/health", self.log_stream.getvalue())

    def test_text_log_contains_request_id(self):
        """In text mode, log line includes the request ID in brackets."""
        self.client.get("/health")
        self.handler.flush()
        output = self.log_stream.getvalue()
        # Request ID is in UUID format — detect by hyphen pattern
        import re
        self.assertRegex(output, r"[0-9a-f]{8}-[0-9a-f]{4}")


class TestRestRequestID(unittest.TestCase):
    """X-Request-ID header is present and unique per request."""

    def setUp(self):
        _app.config["TESTING"] = True
        self.client = _app.test_client()

    def test_response_has_x_request_id_header(self):
        """Every response includes X-Request-ID header."""
        resp = self.client.get("/health")
        self.assertIn("X-Request-ID", resp.headers, "X-Request-ID header missing")

    def test_request_ids_are_unique(self):
        """Each request gets a different X-Request-ID."""
        ids = {self.client.get("/health").headers.get("X-Request-ID") for _ in range(5)}
        self.assertEqual(len(ids), 5, "Request IDs are not unique")

    def test_request_id_is_uuid_format(self):
        """X-Request-ID matches UUID v4 format."""
        import re
        resp = self.client.get("/health")
        rid = resp.headers.get("X-Request-ID", "")
        self.assertRegex(
            rid,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            f"X-Request-ID not UUID format: {rid}",
        )

    def test_json_log_request_id_matches_response_header(self):
        """request_id in JSON log matches X-Request-ID response header."""
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.INFO)
        orig_format = rest_mod.settings.LOG_FORMAT
        rest_mod.settings.LOG_FORMAT = "json"
        rest_mod.logger.addHandler(handler)
        rest_mod.logger.setLevel(logging.DEBUG)
        try:
            resp = self.client.get("/health")
            handler.flush()
            lines = [l for l in log_stream.getvalue().splitlines() if l.strip()]
            record = None
            for line in reversed(lines):
                try:
                    record = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            self.assertIsNotNone(record)
            self.assertEqual(record["request_id"], resp.headers.get("X-Request-ID"))
        finally:
            rest_mod.logger.removeHandler(handler)
            rest_mod.settings.LOG_FORMAT = orig_format


if __name__ == "__main__":
    unittest.main()
