"""Тесты JSON структурированного логирования REST-сервера.

Проверяют:
1. JSON формат лога при LOG_FORMAT=json (поля ts, method, path, status, duration_ms, ip)
2. Текстовый формат лога при LOG_FORMAT=text (без JSON)
3. Наличие заголовка X-Request-ID в каждом ответе
4. Уникальность Request-ID между запросами
5. Корректность полей JSON-лога (типы, значения)
"""

import importlib
import json
import sys
import os
import types
import logging
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup (must precede all local imports)
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Patch heavy / optional dependencies BEFORE importing rest_server.
#
# Wave 1744 test-isolation fix: import the REAL module first so sys.modules
# holds the real object — bare ModuleType stubs leak across xdist workers.
# Only replace the specific heavy class/attr that would be constructed at
# rest_server module-load time.
# ---------------------------------------------------------------------------


def _ensure_real_or_stub(mod_name: str) -> types.ModuleType:
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    try:
        importlib.import_module(mod_name)
    except Exception:
        sys.modules[mod_name] = types.ModuleType(mod_name)
    return sys.modules[mod_name]


# core.engine — swap AudioEngine so rest_server doesn't do heavy MLX warmup.
# Save original and restore after rest_server import to avoid polluting later tests.
_engine_mod = _ensure_real_or_stub("core.engine")
_orig_AudioEngine = getattr(_engine_mod, "AudioEngine", None)


class _FakeEngine:
    quality_profile = "balanced"

    def __init__(self, *args, **kwargs):
        pass

    def normalize_audio(self, *a, **kw):
        pass


_engine_mod.AudioEngine = _FakeEngine  # type: ignore[attr-defined]

# backend.event_bus — ensure bus/sse_stream attrs
_eb = _ensure_real_or_stub("backend.event_bus")
if not hasattr(_eb, "bus"):
    _eb.bus = MagicMock()  # type: ignore[attr-defined]
if not hasattr(_eb, "sse_stream"):
    _eb.sse_stream = MagicMock(return_value=iter([]))  # type: ignore[attr-defined]

# backend.service — swap BackendService (save original)
_service_mod = _ensure_real_or_stub("backend.service")
_orig_BackendService = getattr(_service_mod, "BackendService", None)


class _FakeBackendService:
    @staticmethod
    def _build_readiness_report_static():
        return {"overall_ready": True, "components": {}}


_service_mod.BackendService = _FakeBackendService  # type: ignore[attr-defined]

# backend.state_store — swap StateStore (save original)
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

# backend.transcriber — swap Transcriber (save original)
_transcriber_mod = _ensure_real_or_stub("backend.transcriber")
_orig_Transcriber = getattr(_transcriber_mod, "Transcriber", None)


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_transcriber_mod.Transcriber = _FakeTranscriber  # type: ignore[attr-defined]

# backend.metrics_collector — swap metrics instance (save original)
_metrics_mod = _ensure_real_or_stub("backend.metrics_collector")
_orig_metrics = getattr(_metrics_mod, "metrics", None)


class _FakeMetrics:
    def get_summary(self):
        return {"status": "waiting_data"}

    def record(self, *a, **kw):
        pass


_metrics_mod.metrics = _FakeMetrics()  # type: ignore[attr-defined]

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
from core.config import settings  # noqa: E402,F401

with patch("pathlib.Path.mkdir"):
    import backend.rest_server as rest_mod  # noqa: E402
    _app = rest_mod.app

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
        lines = [ln for ln in self.log_stream.getvalue().splitlines() if ln.strip()]
        for line in reversed(lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def test_json_log_has_required_fields(self):
        """JSON log contains all required fields.

        S3/Задача 7b: /health больше не пишет access-лог на 2xx (см.
        HealthLogNoiseSuppressionTest ниже) — общий контракт формата логов
        проверяем на нейтральном эндпойнте /info, который такому подавлению
        не подлежит.
        """
        self.client.get("/info")
        record = self._last_json_log()
        self.assertIsNotNone(record, "No JSON log record found")
        for field in ("ts", "request_id", "method", "path", "status", "duration_ms", "ip", "content_length"):
            self.assertIn(field, record, f"Missing field: {field}")

    def test_json_log_field_types_and_values(self):
        """JSON log field types match specification."""
        self.client.get("/info")
        record = self._last_json_log()
        self.assertIsNotNone(record)
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/info")
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
        """In text mode, log lines are plain text, not JSON objects.

        S3/Задача 7b: /health на 2xx больше не пишет access-лог вовсе — общий
        контракт текстового формата проверяем на /info (см.
        HealthLogNoiseSuppressionTest ниже про сам /health).
        """
        self.client.get("/info")
        self.handler.flush()
        lines = [ln for ln in self.log_stream.getvalue().splitlines() if ln.strip()]
        self.assertTrue(len(lines) > 0, "No log lines emitted")
        last = lines[-1]
        try:
            json.loads(last)
            self.fail(f"Expected non-JSON log line in text mode, got: {last}")
        except json.JSONDecodeError:
            pass  # expected

    def test_text_log_contains_path(self):
        """In text mode, log line includes the request path."""
        self.client.get("/info")
        self.handler.flush()
        self.assertIn("/info", self.log_stream.getvalue())

    def test_text_log_contains_request_id(self):
        """In text mode, log line includes the request ID in brackets."""
        self.client.get("/info")
        self.handler.flush()
        output = self.log_stream.getvalue()
        # Request ID is in UUID format — detect by hyphen pattern
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
        resp = self.client.get("/health")
        rid = resp.headers.get("X-Request-ID", "")
        self.assertRegex(
            rid,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
            f"X-Request-ID not UUID format: {rid}",
        )

    def test_json_log_request_id_matches_response_header(self):
        """request_id in JSON log matches X-Request-ID response header.

        S3/Задача 7b: /health на 2xx больше не пишет access-лог — используем
        /info, где сопоставление лог-записи и заголовка по-прежнему
        применимо.
        """
        log_stream = StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.INFO)
        orig_format = rest_mod.settings.LOG_FORMAT
        rest_mod.settings.LOG_FORMAT = "json"
        rest_mod.logger.addHandler(handler)
        rest_mod.logger.setLevel(logging.DEBUG)
        try:
            resp = self.client.get("/info")
            handler.flush()
            lines = [ln for ln in log_stream.getvalue().splitlines() if ln.strip()]
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


class HealthLogNoiseSuppressionTest(unittest.TestCase):
    """S3/Задача 7b, п.6: сторож REST опрашивает /health раз в 30с — здоровый
    ответ не пишет access-лог (~2880 строк/сутки навсегда без единого
    полезного бита). X-Request-ID трассировка не должна пострадать."""

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

    def _log_lines(self):
        self.handler.flush()
        return [ln for ln in self.log_stream.getvalue().splitlines() if ln.strip()]

    def test_healthy_health_probe_is_not_logged(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._log_lines(), [])

    def test_health_response_still_carries_request_id_header(self):
        # Подавление лога не должно утащить за собой трассировку.
        resp = self.client.get("/health")
        self.assertIn("X-Request-ID", resp.headers)

    def test_non_health_endpoint_still_logs_as_before(self):
        # Регрессия: подавление касается ТОЛЬКО /health — остальные пути
        # логируются как раньше.
        self.client.get("/info")
        self.assertEqual(len(self._log_lines()), 1)

    def test_non_2xx_health_response_is_still_logged(self):
        # Белый ящик: log_request() напрямую с искусственным 500-ответом на
        # /health — /health не имеет штатной ошибочной ветки, воспроизвести
        # реальный отказ через сеть нестабильно.
        with _app.test_request_context("/health"):
            rest_mod.start_timer()
            resp = _app.response_class(response="err", status=500)
            rest_mod.log_request(resp)
        self.assertEqual(len(self._log_lines()), 1)


if __name__ == "__main__":
    unittest.main()
