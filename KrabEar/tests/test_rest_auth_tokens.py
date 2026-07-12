"""Tests for backend/rest_auth.py and require_api_key decorator."""
from __future__ import annotations
import os
import sys
import tempfile
import types as _types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from backend.rest_auth import RestAuth  # noqa: E402


def _make_auth(tmp_dir) -> RestAuth:
    return RestAuth(data_dir=tmp_dir)


class TestRestAuthCreate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_create_returns_non_empty_raw_token(self):
        raw, meta = self.auth.create_token("my-app")
        self.assertGreater(len(raw), 20)

    def test_create_meta_has_expected_keys(self):
        _, meta = self.auth.create_token("my-app")
        for key in ("id", "name", "created_at", "scopes"):
            self.assertIn(key, meta, f"Missing key: {key}")

    def test_create_meta_no_token_hash(self):
        _, meta = self.auth.create_token("my-app")
        self.assertNotIn("token_hash", meta)

    def test_create_stores_name_correctly(self):
        _, meta = self.auth.create_token("ci-bot")
        self.assertEqual(meta["name"], "ci-bot")


class TestRestAuthList(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_list_initially_empty(self):
        self.assertEqual(self.auth.list_tokens(), [])

    def test_list_after_create(self):
        self.auth.create_token("t1")
        tokens = self.auth.list_tokens()
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0]["name"], "t1")

    def test_list_multiple_tokens(self):
        self.auth.create_token("a")
        self.auth.create_token("b")
        self.assertEqual(len(self.auth.list_tokens()), 2)

    def test_list_tokens_no_hash(self):
        self.auth.create_token("safe")
        for t in self.auth.list_tokens():
            self.assertNotIn("token_hash", t)


class TestRestAuthRevoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_revoke_existing_returns_true(self):
        _, meta = self.auth.create_token("to-revoke")
        self.assertTrue(self.auth.revoke_token(meta["id"]))

    def test_revoke_removes_from_list(self):
        _, meta = self.auth.create_token("gone")
        self.auth.revoke_token(meta["id"])
        self.assertEqual(len(self.auth.list_tokens()), 0)

    def test_revoke_unknown_id_returns_false(self):
        self.assertFalse(self.auth.revoke_token("nonexistent_xyz"))


class TestRestAuthVerify(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_verify_valid_token_returns_meta(self):
        raw, _ = self.auth.create_token("verify-test")
        meta = self.auth.verify_token(raw)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["name"], "verify-test")

    def test_verify_invalid_token_returns_none(self):
        self.auth.create_token("x")
        self.assertIsNone(self.auth.verify_token("completely_wrong_value"))

    def test_verify_empty_string_returns_none(self):
        self.assertIsNone(self.auth.verify_token(""))

    def test_verify_updates_last_used(self):
        raw, meta = self.auth.create_token("last-used")
        self.assertIsNone(meta["last_used"])
        self.auth.verify_token(raw)
        self.assertIsNotNone(self.auth.list_tokens()[0]["last_used"])

    def test_verify_revoked_token_returns_none(self):
        raw, meta = self.auth.create_token("revoke-verify")
        self.auth.revoke_token(meta["id"])
        self.assertIsNone(self.auth.verify_token(raw))


class TestRestAuthScopes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.auth = _make_auth(self._tmp)

    def test_default_scope_is_wildcard(self):
        _, meta = self.auth.create_token("no-scopes")
        self.assertEqual(meta["scopes"], ["*"])

    def test_custom_scopes_stored(self):
        _, meta = self.auth.create_token("scoped", scopes=["read", "write"])
        self.assertIn("read", meta["scopes"])
        self.assertIn("write", meta["scopes"])

    def test_custom_scopes_in_list(self):
        self.auth.create_token("metrics-only", scopes=["metrics"])
        tokens = self.auth.list_tokens()
        self.assertIn("metrics", tokens[0]["scopes"])


class TestRestAuthPersistence(unittest.TestCase):
    def test_tokens_survive_reinstantiation(self):
        tmp = tempfile.mkdtemp()
        auth1 = _make_auth(tmp)
        _, meta = auth1.create_token("persistent")
        auth2 = _make_auth(tmp)
        ids = [t["id"] for t in auth2.list_tokens()]
        self.assertIn(meta["id"], ids)

    def test_verify_survives_reinstantiation(self):
        tmp = tempfile.mkdtemp()
        auth1 = _make_auth(tmp)
        raw, _ = auth1.create_token("persist-verify")
        auth2 = _make_auth(tmp)
        self.assertIsNotNone(auth2.verify_token(raw))


class TestRestAuthFilePermissions(unittest.TestCase):
    def test_file_permissions_are_0600(self):
        tmp = tempfile.mkdtemp()
        auth = _make_auth(tmp)
        auth.create_token("perm-test")
        tokens_file = Path(tmp) / "api_tokens.json"
        self.assertTrue(tokens_file.exists())
        mode = tokens_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, f"Expected 0600, got {oct(mode)}")


# ---------------------------------------------------------------------------
# Decorator integration tests
# ---------------------------------------------------------------------------

def _ensure_rest_server_stubs():
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
    stubs = {
        "core.engine": {"AudioEngine": type("FE", (), {
            "__init__": lambda s, *a, **k: None,
            "quality_profile": "balanced",
            "normalize_audio": lambda s, *a, **k: None,
        })},
        "backend.event_bus": {
            "bus": MagicMock(),
            "sse_stream": MagicMock(return_value=iter([])),
        },
        "backend.service": {"BackendService": type("FBS", (), {
            "_build_readiness_report_static": staticmethod(
                lambda: {"overall_ready": True, "components": {}}
            ),
        })},
        "backend.state_store": {"StateStore": type("FSS", (), {
            "__init__": lambda s, *a, **k: None,
            "is_idempotent": lambda s, *a, **k: False,
            "load_vocabulary": lambda s: [],
            "save_vocabulary": lambda s, *a, **k: None,
        })},
        "backend.transcriber": {"Transcriber": type("FT", (), {
            "__init__": lambda s, *a, **k: None,
        })},
        "backend.metrics_collector": {"metrics": type("FM", (), {
            "get_summary": lambda s: {
                "latency_p50_ms": None, "latency_p95_ms": None,
                "latency_p99_ms": None, "confidence_avg": None,
                "request_count": 0, "error_count": 0,
                "total_requests": 0, "error_rate": 0.0, "status": "waiting_data",
            },
            "record": lambda s, *a, **k: None,
        })()},
    }
    inserted = []
    for mod_name, attrs in stubs.items():
        if mod_name not in sys.modules:
            m = _types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(m, k, v)
            sys.modules[mod_name] = m
            inserted.append(mod_name)
    return inserted


_inserted_stub_modules = _ensure_rest_server_stubs()

try:
    with patch("pathlib.Path.mkdir"):
        from backend.rest_server import app as _rest_app, require_api_key as _require_api_key  # noqa: E402
finally:
    # Снимаем ВСТАВЛЕННЫЕ НАМИ фейки из sys.modules — иначе фейк FBS/FSS
    # отравляет все последующие тест-файлы чанка (см. test_rest_server_w1212.py).
    for _name in _inserted_stub_modules:
        sys.modules.pop(_name, None)


class TestRequireApiKeyAuthEnabled(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import backend.rest_server as _rs
        _rs._rest_auth = None

    def _mock_settings(self, auth_enabled, api_key=""):
        m = MagicMock()
        m.REST_API_AUTH_ENABLED = auth_enabled
        m.REST_API_KEY = api_key
        return m

    def test_auth_disabled_no_header_is_noop(self):
        called = []
        with patch("backend.rest_server.settings", self._mock_settings(False)):
            @_require_api_key
            def view():
                called.append(True)
                return "ok", 200
            with _rest_app.test_request_context("/open"):
                view()
        self.assertTrue(called)

    def test_auth_enabled_missing_header_returns_401(self):
        import backend.rest_server as _rs
        _rs._rest_auth = _make_auth(self._tmp)
        with patch("backend.rest_server.settings", self._mock_settings(True)):
            @_require_api_key
            def view():
                return "ok", 200
            with _rest_app.test_request_context("/guarded"):
                result = view()
        _, code = result
        self.assertEqual(code, 401)

    def test_auth_enabled_valid_token_passes(self):
        real_auth = _make_auth(self._tmp)
        raw, _ = real_auth.create_token("test-client")
        import backend.rest_server as _rs
        _rs._rest_auth = real_auth
        called = []
        with patch("backend.rest_server.settings", self._mock_settings(True)):
            @_require_api_key
            def view():
                called.append(True)
                return "ok", 200
            with _rest_app.test_request_context(
                "/secure", headers={"Authorization": f"Bearer {raw}"}
            ):
                view()
        self.assertTrue(called)

    def test_auth_enabled_invalid_token_returns_401(self):
        real_auth = _make_auth(self._tmp)
        real_auth.create_token("legit")
        import backend.rest_server as _rs
        _rs._rest_auth = real_auth
        with patch("backend.rest_server.settings", self._mock_settings(True)):
            @_require_api_key
            def view():
                return "ok", 200
            with _rest_app.test_request_context(
                "/strict",
                headers={"Authorization": "Bearer totally_wrong_token"},
            ):
                result = view()
        _, code = result
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
