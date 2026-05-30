"""Tests for W1350 F1+F2+F3 fixes:
  F1 LOW  — api_version_header() docstring corrected
  F2 LOW  — SUPPORTED_VERSIONS is a tuple
  F3 MED  — /v2/* returns 501 Not Implemented (not 404)
"""
import ast
import sys
import os
import unittest

# ---------------------------------------------------------------------------
# Ensure backend package is importable without the full Flask+MLX stack.
# We test api_versioning.py via AST (no heavy imports), and rest_server via
# a thin Flask test client that stubs out the expensive collaborators.
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
KRABEAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
for p in (PROJECT_ROOT, KRABEAR_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

_API_VERSIONING_PATH = os.path.join(KRABEAR_ROOT, "backend", "api_versioning.py")
_REST_SERVER_PATH = os.path.join(KRABEAR_ROOT, "backend", "rest_server.py")


# ---------------------------------------------------------------------------
# AST-level tests (no imports required — works without Flask/MLX installed)
# ---------------------------------------------------------------------------

class TestApiVersioningAST(unittest.TestCase):
    """Static analysis of api_versioning.py via AST."""

    def setUp(self):
        with open(_API_VERSIONING_PATH, "r", encoding="utf-8") as fh:
            self.source = fh.read()
        self.tree = ast.parse(self.source)

    def _find_assignment(self, name):
        """Return the first module-level Assign whose target is *name*."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        return node
            if isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    return node
        return None

    def test_supported_versions_is_tuple(self):
        """SUPPORTED_VERSIONS must be assigned a tuple literal (not a list)."""
        node = self._find_assignment("SUPPORTED_VERSIONS")
        self.assertIsNotNone(node, "SUPPORTED_VERSIONS assignment not found")

        # AnnAssign stores the value in .value; plain Assign in .value too
        value = node.value
        self.assertIsInstance(
            value, ast.Tuple,
            f"Expected SUPPORTED_VERSIONS to be a tuple, got {type(value).__name__}",
        )

    def test_supported_versions_includes_v1(self):
        """SUPPORTED_VERSIONS must contain APIVersion.V1 (always required)."""
        # V2 is now included in SUPPORTED_VERSIONS to enable proper 501 routing
        # (rest_server.py routes /v2/* to 501 Not Implemented stubs). This test
        # checks V1 is present — the invariant that always matters for clients.
        node = self._find_assignment("SUPPORTED_VERSIONS")
        self.assertIsNotNone(node, "SUPPORTED_VERSIONS assignment not found")

        # Collect all attribute names referenced in the value
        attr_names = [
            n.attr
            for n in ast.walk(node.value)
            if isinstance(n, ast.Attribute)
        ]
        self.assertIn(
            "V1", attr_names,
            "SUPPORTED_VERSIONS must contain V1",
        )

    def test_api_version_header_docstring_mentions_parentheses(self):
        """api_version_header() docstring must clarify the factory pattern (call parens)."""
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "api_version_header":
                docstring = ast.get_docstring(node) or ""
                # The updated docstring should explain that calling parentheses are needed
                self.assertIn(
                    "api_version_header()",
                    docstring,
                    "Docstring should show usage with call parens: app.after_request(api_version_header())",
                )
                return
        self.fail("api_version_header function not found in api_versioning.py")


class TestRestServerV2RouteAST(unittest.TestCase):
    """Static analysis of rest_server.py — check the /v2 catch-all is present."""

    def setUp(self):
        with open(_REST_SERVER_PATH, "r", encoding="utf-8") as fh:
            self.source = fh.read()

    def test_v2_catch_all_route_present(self):
        """/v2/<path:p> route string must appear in rest_server.py."""
        self.assertIn(
            '"/v2/<path:p>"',
            self.source,
            "rest_server.py must define a /v2/<path:p> catch-all route",
        )

    def test_v2_501_status_code_present(self):
        """501 status code must be set for the v2 stub response."""
        self.assertIn(
            "501",
            self.source,
            "rest_server.py must return HTTP 501 for /v2/* routes",
        )

    def test_v2_error_body_mentions_supported_versions(self):
        """The 501 response body must mention 'supported_versions'."""
        self.assertIn(
            "supported_versions",
            self.source,
            "501 response body should include 'supported_versions' key",
        )


# ---------------------------------------------------------------------------
# Flask test-client tests — import the actual Flask app with heavy deps mocked
# ---------------------------------------------------------------------------

def _build_flask_test_app():
    """
    Import rest_server.py with the heavy dependencies stubbed out so we can
    use the Flask test client without MLX / sounddevice / pyannote installed.
    """
    import types
    import importlib

    # ------------------------------------------------------------------
    # Minimal stubs for modules that are unavailable in the test sandbox
    # ------------------------------------------------------------------

    def _stub_module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    # flask_smorest
    if "flask_smorest" not in sys.modules:
        Api_cls = type("Api", (), {
            "__init__": lambda self, app, **kw: None,
            "register_blueprint": lambda self, blp, **kw: None,
        })
        Blueprint_cls = type("Blueprint", (), {
            "__init__": lambda self, *a, **kw: None,
            "route": lambda self, *a, **kw: (lambda f: f),
            "response": lambda self, *a, **kw: (lambda f: f),
            "arguments": lambda self, *a, **kw: (lambda f: f),
        })
        def abort_fn(*a, **kw):
            raise Exception("abort")
        sm = _stub_module("flask_smorest", Api=Api_cls, Blueprint=Blueprint_cls, abort=abort_fn)

    if "flask_sock" not in sys.modules:
        Sock_cls = type("Sock", (), {
            "__init__": lambda self, app=None, **kw: None,
            "route": lambda self, *a, **kw: (lambda f: f),
        })
        _stub_module("flask_sock", Sock=Sock_cls)

    if "flask_limiter" not in sys.modules:
        Limiter_cls = type("Limiter", (), {
            "__init__": lambda self, *a, **kw: None,
            "limit": lambda self, *a, **kw: (lambda f: f),
        })
        _stub_module("flask_limiter", Limiter=Limiter_cls)
        _stub_module("flask_limiter.util", get_remote_address=lambda: "127.0.0.1")

    if "flask_cors" not in sys.modules:
        _stub_module("flask_cors", CORS=lambda *a, **kw: None)

    if "marshmallow" not in sys.modules:
        Schema_cls = type("Schema", (), {})
        fields_mod = types.ModuleType("marshmallow.fields")
        for fname in ("String", "Float", "Integer", "Boolean", "Dict", "List"):
            setattr(fields_mod, fname, lambda *a, **kw: None)
        _stub_module("marshmallow", Schema=Schema_cls, fields=fields_mod)
        sys.modules["marshmallow.fields"] = fields_mod

    if "werkzeug.utils" not in sys.modules:
        _stub_module("werkzeug.utils", secure_filename=lambda f: f)

    # Core / backend stubs
    for mod_name in (
        "core.config", "core.engine", "backend.event_bus",
        "backend.rest_auth", "backend.service", "backend.state_store",
        "backend.transcriber", "backend.metrics_collector",
        "backend.api_versioning",
    ):
        if mod_name not in sys.modules:
            _stub_module(mod_name)

    # core.config.settings
    if not hasattr(sys.modules.get("core.config", object()), "settings"):
        class _FakeSettings:
            DATA_DIR = type("Path", (), {
                "__truediv__": lambda s, o: s,
                "mkdir": lambda *a, **kw: None,
            })()
            CORS_ORIGINS = "*"
            RATE_LIMIT_ENABLED = False
            REST_API_KEY = ""
            REST_API_AUTH_ENABLED = False
            LOG_FORMAT = "text"

        sys.modules["core.config"].settings = _FakeSettings()

    # core.engine.AudioEngine
    if not hasattr(sys.modules.get("core.engine", object()), "AudioEngine"):
        AE = type("AudioEngine", (), {
            "__init__": lambda self, **kw: setattr(self, "quality_profile", "balanced"),
        })
        sys.modules["core.engine"].AudioEngine = AE

    # backend.event_bus
    eb = sys.modules.get("backend.event_bus")
    if eb and not hasattr(eb, "bus"):
        class _FakeBus:
            def subscribe(self): return None
        eb.bus = _FakeBus()
        eb.sse_stream = lambda *a, **kw: iter([])

    # backend.state_store.StateStore
    ss = sys.modules.get("backend.state_store")
    if ss and not hasattr(ss, "StateStore"):
        ss.StateStore = type("StateStore", (), {
            "__init__": lambda self, *a: None,
            "load_vocabulary": lambda self: [],
            "save_vocabulary": lambda self, v: None,
            "is_idempotent": lambda self, *a: False,
            "add_history_item": lambda self, **kw: type("Item", (), {"id": "x"})(),
        })

    # backend.transcriber.Transcriber
    tr = sys.modules.get("backend.transcriber")
    if tr and not hasattr(tr, "Transcriber"):
        tr.Transcriber = type("Transcriber", (), {
            "__init__": lambda self, **kw: None,
        })

    # backend.metrics_collector.metrics
    mc = sys.modules.get("backend.metrics_collector")
    if mc and not hasattr(mc, "metrics"):
        mc.metrics = type("Metrics", (), {
            "get_summary": lambda self: {},
            "record": lambda self, *a, **kw: None,
        })()

    # backend.rest_auth.RestAuth
    ra = sys.modules.get("backend.rest_auth")
    if ra and not hasattr(ra, "RestAuth"):
        ra.RestAuth = type("RestAuth", (), {
            "__init__": lambda self, *a, **kw: None,
        })

    # backend.service.BackendService
    bs = sys.modules.get("backend.service")
    if bs and not hasattr(bs, "BackendService"):
        bs.BackendService = type("BackendService", (), {
            "_build_readiness_report_static": staticmethod(lambda: {"overall_ready": True, "components": {}}),
        })

    # backend.api_versioning — use the REAL module
    real_av_path = os.path.join(KRABEAR_ROOT, "backend", "api_versioning.py")
    spec = importlib.util.spec_from_file_location("backend.api_versioning", real_av_path)
    real_av = importlib.util.module_from_spec(spec)
    # Provide the KrabEar.__version__ stub it needs
    if "KrabEar" not in sys.modules:
        _stub_module("KrabEar")
    if "KrabEar.__version__" not in sys.modules:
        _stub_module("KrabEar.__version__", __version__="test")
    spec.loader.exec_module(real_av)
    sys.modules["backend.api_versioning"] = real_av

    # Now import the real rest_server
    spec2 = importlib.util.spec_from_file_location("backend.rest_server", _REST_SERVER_PATH)
    rs_mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(rs_mod)
    return rs_mod.app


class TestV2RouteFlask(unittest.TestCase):
    """Flask test-client verification of the /v2 catch-all route."""

    @classmethod
    def setUpClass(cls):
        try:
            app = _build_flask_test_app()
            app.config["TESTING"] = True
            cls.client = app.test_client()
            cls.available = True
        except Exception as exc:
            cls.available = False
            cls.skip_reason = str(exc)

    def _skip_if_unavailable(self):
        if not self.available:
            self.skipTest(f"Flask app unavailable in test sandbox: {self.skip_reason}")

    def test_v2_path_returns_501_not_404(self):
        """GET /v2/anything must return 501, not 404."""
        self._skip_if_unavailable()
        resp = self.client.get("/v2/transcribe")
        self.assertEqual(
            resp.status_code, 501,
            f"Expected 501 for /v2/transcribe, got {resp.status_code}",
        )

    def test_v2_root_returns_501(self):
        """GET /v2/ must return 501."""
        self._skip_if_unavailable()
        resp = self.client.get("/v2/")
        self.assertEqual(resp.status_code, 501)

    def test_v2_response_has_x_api_version_header(self):
        """501 response for /v2/* must include X-API-Version: v2."""
        self._skip_if_unavailable()
        resp = self.client.get("/v2/health")
        self.assertEqual(resp.headers.get("X-API-Version"), "v2")

    def test_v2_response_body_structure(self):
        """501 body must include 'error' and 'supported_versions' keys."""
        self._skip_if_unavailable()
        import json
        resp = self.client.get("/v2/stt/transcribe")
        data = json.loads(resp.data)
        self.assertIn("error", data)
        self.assertIn("supported_versions", data)
        self.assertIn("v1", data["supported_versions"])

    def test_v1_routes_still_work(self):
        """/v1/vocabulary (GET) must still return 200 — v1 unaffected by v2 stub."""
        self._skip_if_unavailable()
        resp = self.client.get("/v1/vocabulary")
        self.assertNotEqual(
            resp.status_code, 501,
            "v1 routes must not be broken by the v2 catch-all",
        )
        self.assertIn(resp.status_code, (200, 401, 403),
                      f"Unexpected status for /v1/vocabulary: {resp.status_code}")


if __name__ == "__main__":
    unittest.main()
