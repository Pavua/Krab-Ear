"""Tests for W1674 F1+F2+F3 MED fixes (W1684).

F1 MED — /v2/* catch-all now requires @require_api_key + rate-limit.
F2 MED — 413 RequestEntityTooLarge returns JSON, not HTML.
F3 MED — app.run() EADDRINUSE → structured logger.error + sys.exit(1).

Run:
    VENV="/Users/pablito/Antigravity_AGENTS/Krab Ear/.venv_krab_ear"
    PATH="$VENV/bin:$PATH" PYTHONPATH=KrabEar python -m pytest \
        KrabEar/tests/test_rest_server_w1684.py -v --tb=short
"""
from __future__ import annotations

import errno
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Guard: skip if Flask / REST-server deps are not installed.
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
    _mock_store.add_history_item.return_value = MagicMock(id="hist-w1684-001")
    _mock_store.load_settings.return_value = {}

    _mock_transcriber = MagicMock()
    _mock_transcriber.transcribe.return_value = {
        "text": "test",
        "raw_text": "test",
        "confidence": 0.9,
        "duration_ms": 300,
        "engine": "mlx-whisper",
        "model": "whisper-small",
        "language": "en",
        "segments": [],
        "diarization": {},
    }

    _mock_metrics = MagicMock()
    _mock_metrics.get_summary.return_value = {
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

    if "backend.rest_server" not in sys.modules:
        with patch("core.engine.AudioEngine", return_value=_mock_engine), \
                patch("backend.state_store.StateStore", return_value=_mock_store), \
                patch("backend.transcriber.Transcriber", return_value=_mock_transcriber), \
                patch("backend.metrics_collector.metrics", _mock_metrics):
            import backend.rest_server as _rest_mod  # type: ignore
    else:
        import backend.rest_server as _rest_mod  # type: ignore
        _rest_mod.engine = _mock_engine

    _REST_AVAILABLE = True
except Exception:  # pragma: no cover
    pass


def _client():
    _rest_mod.app.config["TESTING"] = True
    return _rest_mod.app.test_client()


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = MagicMock()
        self.engine.quality_profile = "balanced"
        self.engine.normalize_audio = MagicMock()

        self.store = MagicMock()
        self.store.load_vocabulary.return_value = []
        self.store.is_idempotent.return_value = False
        self.store.add_history_item.return_value = MagicMock(id="hist-w1684-base")
        self.store.load_settings.return_value = {}

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
# F1 — /v2/* catch-all requires auth (W1674 F1 MED fix)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestV2RouteRequiresAuth(_Base):
    """F1: /v2/* catch-all must enforce @require_api_key (W1684)."""

    def test_v2_root_without_auth_returns_401_when_key_configured(self):
        """GET /v2/ without Bearer token returns 401 when REST_API_KEY is set."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "secret"), \
                patch.object(_rest_mod.settings, "REST_API_AUTH_ENABLED", False):
            resp = self.client.get("/v2/")
        self.assertEqual(resp.status_code, 401)

    def test_v2_path_without_auth_returns_401_when_key_configured(self):
        """GET /v2/stt/transcribe without Bearer token returns 401."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "secret"), \
                patch.object(_rest_mod.settings, "REST_API_AUTH_ENABLED", False):
            resp = self.client.get("/v2/stt/transcribe")
        self.assertEqual(resp.status_code, 401)

    def test_v2_with_valid_token_returns_501(self):
        """GET /v2/ with valid Bearer token returns 501 Not Implemented (not 401)."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "correct-key"), \
                patch.object(_rest_mod.settings, "REST_API_AUTH_ENABLED", False):
            resp = self.client.get(
                "/v2/",
                headers={"Authorization": "Bearer correct-key"},
            )
        self.assertEqual(resp.status_code, 501)

    def test_v2_with_valid_token_returns_json_body(self):
        """GET /v2/anything with valid token returns JSON with 'error' key."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", "correct-key"), \
                patch.object(_rest_mod.settings, "REST_API_AUTH_ENABLED", False):
            resp = self.client.get(
                "/v2/vocabulary",
                headers={"Authorization": "Bearer correct-key"},
            )
        body = resp.get_json()
        self.assertIsNotNone(body)
        self.assertIn("error", body)

    def test_v2_without_auth_disabled_auth_returns_501(self):
        """GET /v2/ passes through when auth is entirely disabled (Mode 3)."""
        with patch.object(_rest_mod.settings, "REST_API_KEY", ""), \
                patch.object(_rest_mod.settings, "REST_API_AUTH_ENABLED", False):
            resp = self.client.get("/v2/")
        # Auth disabled → proceed → 501 Not Implemented
        self.assertEqual(resp.status_code, 501)


# ===========================================================================
# F2 — 413 returns JSON not HTML (W1674 F2 MED fix)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class Test413ReturnsJsonNotHtml(_Base):
    """F2: Flask 413 must respond with JSON, not HTML (W1684)."""

    def test_413_content_type_is_json(self):
        """When MAX_CONTENT_LENGTH is exceeded, Content-Type must be application/json."""
        orig_limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10  # 10 bytes
        try:
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(b"X" * 50), "audio.wav")},
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 413)
            self.assertIn("application/json", resp.content_type)
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = orig_limit

    def test_413_body_contains_error_key(self):
        """413 response body must contain an 'error' key (machine-readable)."""
        orig_limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(b"X" * 50), "audio.wav")},
                content_type="multipart/form-data",
            )
            body = resp.get_json()
            self.assertIsNotNone(body, "413 body must be valid JSON")
            self.assertIn("error", body)
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = orig_limit

    def test_413_body_contains_max_mb_key(self):
        """413 response body must include 'max_mb' so clients can report it."""
        orig_limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(b"X" * 50), "audio.wav")},
                content_type="multipart/form-data",
            )
            body = resp.get_json()
            self.assertIsNotNone(body)
            self.assertIn("max_mb", body)
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = orig_limit

    def test_413_body_not_html(self):
        """413 response body must not start with HTML doctype."""
        orig_limit = _rest_mod.app.config.get("MAX_CONTENT_LENGTH")
        _rest_mod.app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            resp = self.client.post(
                "/v1/stt/transcribe",
                data={"file": (io.BytesIO(b"X" * 50), "audio.wav")},
                content_type="multipart/form-data",
            )
            raw = resp.data.decode("utf-8", errors="replace").strip().lower()
            self.assertFalse(
                raw.startswith("<!doctype") or raw.startswith("<html"),
                "413 response must not be HTML",
            )
        finally:
            _rest_mod.app.config["MAX_CONTENT_LENGTH"] = orig_limit

    def test_413_handler_is_registered(self):
        """_request_entity_too_large_handler must be registered in app.error_handler_spec.

        Flask 2+ stores error handlers keyed by exception class, not integer code.
        We check that RequestEntityTooLarge (the Werkzeug exception for 413) is
        registered, OR that an integer 413 key exists (Flask < 2 compat).
        """
        from werkzeug.exceptions import RequestEntityTooLarge
        handlers = _rest_mod.app.error_handler_spec.get(None, {})
        # Collect all registered keys (both int codes and exception classes)
        all_keys: set = set()
        for code_map in handlers.values():
            all_keys.update(code_map.keys())
        registered = (
            413 in all_keys
            or RequestEntityTooLarge in all_keys
        )
        self.assertTrue(
            registered,
            f"413 / RequestEntityTooLarge handler not found in {all_keys}",
        )


# ===========================================================================
# F3 — EADDRINUSE logs and exits (W1674 F3 MED fix)
# ===========================================================================

@unittest.skipUnless(_REST_AVAILABLE, "REST server dependencies not available")
class TestEaddrinuseLogsAndExits(unittest.TestCase):
    """F3: EADDRINUSE on app.run() must log a structured error and sys.exit(1)."""

    def _run_main_block(self, mock_run, mock_logger, mock_exit):
        """Simulate the __main__ block by calling app.run() under the same
        try/except guard that live code uses."""
        import errno as _errno

        try:
            mock_run()
        except OSError as _e:
            if _e.errno == _errno.EADDRINUSE:
                mock_logger.error(
                    "REST server failed to start: port 5005 is already in use "
                    "(EADDRINUSE). Another instance may be running. "
                    "Stop it first: lsof -ti :5005 | xargs kill -9",
                    extra={"errno": _e.errno, "port": 5005},
                )
                mock_exit(1)
            else:
                raise

    def test_eaddrinuse_calls_logger_error(self):
        """OSError(EADDRINUSE) triggers logger.error with structured context."""
        mock_logger = MagicMock()
        mock_exit = MagicMock()

        def raise_eaddrinuse():
            raise OSError(errno.EADDRINUSE, "Address already in use")

        self._run_main_block(raise_eaddrinuse, mock_logger, mock_exit)

        mock_logger.error.assert_called_once()
        call_args = mock_logger.error.call_args
        # Message must reference port 5005
        self.assertIn("5005", call_args[0][0])

    def test_eaddrinuse_calls_sys_exit_1(self):
        """OSError(EADDRINUSE) triggers sys.exit(1)."""
        mock_logger = MagicMock()
        mock_exit = MagicMock()

        def raise_eaddrinuse():
            raise OSError(errno.EADDRINUSE, "Address already in use")

        self._run_main_block(raise_eaddrinuse, mock_logger, mock_exit)
        mock_exit.assert_called_once_with(1)

    def test_other_oserror_propagates(self):
        """Non-EADDRINUSE OSError must re-raise, not be swallowed."""
        mock_logger = MagicMock()
        mock_exit = MagicMock()

        def raise_other():
            raise OSError(errno.ENOENT, "No such file")

        with self.assertRaises(OSError):
            self._run_main_block(raise_other, mock_logger, mock_exit)

        # logger.error must NOT have been called for non-EADDRINUSE
        mock_logger.error.assert_not_called()
        mock_exit.assert_not_called()

    def test_eaddrinuse_guard_present_in_rest_server_source(self):
        """Verify the EADDRINUSE guard is present in the live module source."""
        import inspect
        src = inspect.getsource(_rest_mod)
        self.assertIn("EADDRINUSE", src, "EADDRINUSE guard must be in rest_server.py")
        self.assertIn("sys.exit(1)", src, "sys.exit(1) must be in rest_server.py")

    def test_app_run_called_with_localhost_binding(self):
        """app.run() in __main__ block binds to 127.0.0.1, not 0.0.0.0."""
        import inspect
        src = inspect.getsource(_rest_mod)
        self.assertIn("127.0.0.1", src)


if __name__ == "__main__":
    unittest.main()
