"""Regression test: privacy_mode_enabled must win BEFORE REST auth on
/v1/tts/synthesize and /v1/stt/transcribe.

Seam-audit finding (2026-07-04, Krab Ear <-> Voice Gateway): both routes used
to check REST auth (@require_api_key) before the inline privacy_mode check
inside the function body. When a caller's Bearer token was missing/wrong
*and* privacy_mode was on, the response was 401 (auth failure), not 403
(privacy_mode) -- because @require_api_key short-circuited before the
function body's privacy check ever ran.

Voice Gateway's fallback chain treats any non-success TTS/STT result as
"try the next engine" UNLESS the error is specifically "privacy_mode" (see
Krab Voice Gateway app/tts_engines.py / app/stt_engines.py). A plain 401
does not match that special case, so a stale/misconfigured
KRAB_EAR_REST_API_KEY on the Voice Gateway side would silently fall through
to cloud STT/TTS providers *even though the user had privacy_mode_enabled*
-- the exact leak the "privacy mode always wins" gate convention exists to
prevent (see CLAUDE.md "Privacy-mode gate pattern").

Fix: a new `_privacy_gate` decorator applied ABOVE @require_api_key on both
routes, so privacy_mode_enabled is checked first regardless of auth state.
"""

import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _ensure_real_or_stub(mod_name: str) -> types.ModuleType:
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    try:
        import importlib
        importlib.import_module(mod_name)
    except Exception:
        sys.modules[mod_name] = types.ModuleType(mod_name)
    return sys.modules[mod_name]


_engine_mod = _ensure_real_or_stub("core.engine")
_orig_AudioEngine = getattr(_engine_mod, "AudioEngine", None)


class _FakeEngine:
    quality_profile = "balanced"

    def __init__(self, *a, **kw):
        pass

    def normalize_audio(self, *a, **kw):
        pass


_engine_mod.AudioEngine = _FakeEngine  # type: ignore[attr-defined]

_eb = _ensure_real_or_stub("backend.event_bus")
if not hasattr(_eb, "bus"):
    _eb.bus = MagicMock()  # type: ignore[attr-defined]
if not hasattr(_eb, "sse_stream"):
    _eb.sse_stream = MagicMock(return_value=iter([]))  # type: ignore[attr-defined]

_service_mod = _ensure_real_or_stub("backend.service")
_orig_BackendService = getattr(_service_mod, "BackendService", None)


class _FakeBackendService:
    @staticmethod
    def _build_readiness_report_static():
        return {"overall_ready": True, "components": {}}


_service_mod.BackendService = _FakeBackendService  # type: ignore[attr-defined]

_state_store_mod = _ensure_real_or_stub("backend.state_store")
_orig_StateStore = getattr(_state_store_mod, "StateStore", None)


class _FakeStateStore:
    def __init__(self, *a, **kw):
        self._settings = {"privacy_mode_enabled": False}

    def is_idempotent(self, *a, **kw):
        return False

    def load_vocabulary(self):
        return []

    def save_vocabulary(self, *a, **kw):
        pass

    def load_settings(self, lock_timeout_sec=None, nowait=False):
        return dict(self._settings)


_state_store_mod.StateStore = _FakeStateStore  # type: ignore[attr-defined]

_transcriber_mod = _ensure_real_or_stub("backend.transcriber")
_orig_Transcriber = getattr(_transcriber_mod, "Transcriber", None)


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_transcriber_mod.Transcriber = _FakeTranscriber  # type: ignore[attr-defined]

_metrics_mod = _ensure_real_or_stub("backend.metrics_collector")
_orig_metrics = getattr(_metrics_mod, "metrics", None)


class _FakeMetrics:
    def get_summary(self):
        return {"status": "waiting_data"}

    def record(self, *a, **kw):
        pass


_metrics_mod.metrics = _FakeMetrics()  # type: ignore[attr-defined]

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

    ma_mod.Schema = _Schema
    ma_mod.fields = _Fields()
    ma_mod.validate = types.SimpleNamespace()
    sys.modules["marshmallow"] = ma_mod

try:
    from werkzeug.utils import secure_filename  # noqa: F401
except ImportError:
    wz_mod = types.ModuleType("werkzeug.utils")
    wz_mod.secure_filename = lambda name: name
    sys.modules["werkzeug.utils"] = wz_mod

from core.config import settings  # noqa: E402

with patch("pathlib.Path.mkdir"):
    import backend.rest_server as rest_server  # noqa: E402

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


class TestPrivacyGateBeatsAuth(unittest.TestCase):
    """privacy_mode_enabled=True must yield 403, never 401, even with bad/missing auth."""

    _TEST_KEY = "test-fake-restkey-privacy-order"

    def setUp(self):
        self._orig_key = settings.REST_API_KEY
        settings.REST_API_KEY = self._TEST_KEY
        self._fake_store = MagicMock()
        self._fake_store.load_settings.return_value = {"privacy_mode_enabled": True}
        self._fake_store.is_idempotent.return_value = False
        self._store_patch = patch.object(rest_server, "store", self._fake_store)
        self._store_patch.start()
        self.client = rest_server.app.test_client()

    def tearDown(self):
        settings.REST_API_KEY = self._orig_key
        self._store_patch.stop()

    def test_tts_synthesize_no_auth_privacy_on_returns_403_not_401(self):
        resp = self.client.post("/v1/tts/synthesize", json={"text": "hello"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json(), {"ok": False, "skipped": "privacy_mode"})

    def test_tts_synthesize_wrong_token_privacy_on_returns_403_not_401(self):
        resp = self.client.post(
            "/v1/tts/synthesize",
            json={"text": "hello"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json(), {"ok": False, "skipped": "privacy_mode"})

    def test_stt_transcribe_no_auth_privacy_on_returns_403_not_401(self):
        resp = self.client.post(
            "/v1/stt/transcribe",
            data={},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.get_json(), {"ok": False, "skipped": "privacy_mode"})

    def test_tts_synthesize_privacy_off_bad_auth_still_401(self):
        """Sanity check: with privacy OFF, bad auth still yields 401 as before."""
        self._fake_store.load_settings.return_value = {"privacy_mode_enabled": False}
        resp = self.client.post("/v1/tts/synthesize", json={"text": "hello"})
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
