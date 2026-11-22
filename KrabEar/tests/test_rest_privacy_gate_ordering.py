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
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# Откат импортных подмен (раньше: заглушки sys.modules жили вечно; атрибуты возвращались только
# при `_orig is not None` — «отсутствовал» не восстанавливался; при исключении импорта
# rest_server откат вообще не выполнялся). Всё, что вставлено/подменено, учитывается здесь и
# снимается в finally (атрибуты) и в teardown_module (заглушки, свежеимпортированный rest_server).
_MISSING = object()
_INSERTED_STUBS: list = []
_PATCHED_ATTRS: list = []  # (module, attr, original-or-_MISSING)
_REST_WAS_IMPORTED = "backend.rest_server" in sys.modules


def _patch_attr(mod, attr, value):
    _PATCHED_ATTRS.append((mod, attr, getattr(mod, attr, _MISSING)))
    setattr(mod, attr, value)


def _restore_patched_attrs() -> None:
    while _PATCHED_ATTRS:
        mod, attr, orig = _PATCHED_ATTRS.pop()
        if orig is _MISSING:
            if hasattr(mod, attr):
                delattr(mod, attr)
        else:
            setattr(mod, attr, orig)


def _ensure_real_or_stub(mod_name: str) -> types.ModuleType:
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    try:
        import importlib
        importlib.import_module(mod_name)
    except Exception:
        sys.modules[mod_name] = types.ModuleType(mod_name)
        _INSERTED_STUBS.append(mod_name)
    return sys.modules[mod_name]


def teardown_module(module=None):
    """Снять заглушки sys.modules, вставленные этим файлом, и свежеимпортированный rest_server."""
    _restore_patched_attrs()
    if not _REST_WAS_IMPORTED:
        sys.modules.pop("backend.rest_server", None)
    for name in reversed(_INSERTED_STUBS):
        sys.modules.pop(name, None)
    _INSERTED_STUBS.clear()


tearDownModule = teardown_module  # для запуска через unittest


_engine_mod = _ensure_real_or_stub("core.engine")


class _FakeEngine:
    quality_profile = "balanced"

    def __init__(self, *a, **kw):
        pass

    def normalize_audio(self, *a, **kw):
        pass


_patch_attr(_engine_mod, "AudioEngine", _FakeEngine)

_eb = _ensure_real_or_stub("backend.event_bus")
if not hasattr(_eb, "bus"):
    _patch_attr(_eb, "bus", MagicMock())
if not hasattr(_eb, "sse_stream"):
    _patch_attr(_eb, "sse_stream", MagicMock(return_value=iter([])))

_service_mod = _ensure_real_or_stub("backend.service")


class _FakeBackendService:
    @staticmethod
    def _build_readiness_report_static():
        return {"overall_ready": True, "components": {}}


_patch_attr(_service_mod, "BackendService", _FakeBackendService)

_state_store_mod = _ensure_real_or_stub("backend.state_store")


class _FakeStateStore:
    data_dir = Path("/tmp")

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


_patch_attr(_state_store_mod, "StateStore", _FakeStateStore)

_transcriber_mod = _ensure_real_or_stub("backend.transcriber")


class _FakeTranscriber:
    def __init__(self, *a, **kw):
        pass


_patch_attr(_transcriber_mod, "Transcriber", _FakeTranscriber)

_metrics_mod = _ensure_real_or_stub("backend.metrics_collector")


class _FakeMetrics:
    def get_summary(self):
        return {"status": "waiting_data"}

    def record(self, *a, **kw):
        pass


_patch_attr(_metrics_mod, "metrics", _FakeMetrics())

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
    _INSERTED_STUBS.append("flask_smorest")

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
    _INSERTED_STUBS.append("marshmallow")

try:
    from werkzeug.utils import secure_filename  # noqa: F401
except ImportError:
    wz_mod = types.ModuleType("werkzeug.utils")
    wz_mod.secure_filename = lambda name: name
    sys.modules["werkzeug.utils"] = wz_mod
    _INSERTED_STUBS.append("werkzeug.utils")

from core.config import settings  # noqa: E402

try:
    with patch("pathlib.Path.mkdir"):
        import backend.rest_server as rest_server  # noqa: E402
finally:
    # Подмены атрибутов возвращаются СРАЗУ после импорта и при исключении импорта
    _restore_patched_attrs()


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
