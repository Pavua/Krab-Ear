"""Tests for HealthCheckService delegation wiring (W1181 F3 MED / W1187).

Verifies that BackendService delegates all 6 health-check IPC handlers to
HealthCheckService and that no inline duplicate logic remains.
"""

from __future__ import annotations

import ast
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_check_service import HealthCheckService


# ---------------------------------------------------------------------------
# Fake collaborators (reused from test_health_check_service.py pattern)
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, data_dir="/tmp/krab_hcs_w1187"):
        self.data_dir = Path(data_dir)
        self._count = 5

    def count_active_items(self, lock_timeout_sec=None, nowait=False):
        return self._count


class _FakeRecorder:
    is_recording = False


class _FakeHealthChecker:
    def check_all(self):
        return {"status": "healthy", "checks": {}}


class _FakeStartupDiagnostics:
    class _Report:
        status = "ok"
        startup_time_ms = 10.0
        errors = []
        warnings = []
        checks = []

        def to_dict(self):
            return {
                "status": self.status,
                "startup_time_ms": self.startup_time_ms,
                "errors": self.errors,
                "warnings": self.warnings,
                "checks": self.checks,
            }

    def run_all_checks(self):
        return _FakeStartupDiagnostics._Report()


class _FakeIntegrityChecker:
    class _Report:
        status = "ok"
        total_items = 3
        orphaned_tombstones = 0
        invalid_json_lines = 0
        checks = []

    def check_integrity(self, data_dir):
        return _FakeIntegrityChecker._Report()


class _FakeSettingsSvc:
    _cache_ttl = 5
    _cache = {}

    def cached_settings(self, nowait=False):
        return {}


class _FakeLLMRewriter:
    _last_latency_ms = 10
    _model = "test-model"

    def warmup(self):
        return True

    def passive_health_check(self):
        return (True, True)

    def status(self):
        return {"enabled": True, "model": self._model}


def _make_health_check_service(**kwargs):
    defaults = dict(
        store=_FakeStore(),
        health_checker=_FakeHealthChecker(),
        startup_diagnostics=_FakeStartupDiagnostics(),
        integrity_checker=_FakeIntegrityChecker(),
        llm_probe=None,
        metrics_collector=None,
        transcriber=None,
        llm_rewriter=None,
        settings_svc=_FakeSettingsSvc(),
        start_time=time.monotonic() - 5.0,
        app_version="test-W1187",
        recorder=_FakeRecorder(),
        last_stt_engine_ref=["mlx-whisper"],
    )
    defaults.update(kwargs)
    return HealthCheckService(**defaults)


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_ping delegates correctly
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesPing(unittest.TestCase):
    """handle_ping must return correct contract fields via HealthCheckService."""

    def setUp(self):
        self.svc = _make_health_check_service()

    def test_handle_ping_returns_status_ok(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["status"], "ok")

    def test_handle_ping_returns_service_name(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["service"], "krabear-backend")

    def test_handle_ping_returns_version(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["version"], "test-W1187")

    def test_handle_ping_returns_all_contract_keys(self):
        result = self.svc.handle_ping({})
        required = {"status", "service", "version", "uptime_sec", "is_recording", "history_count"}
        self.assertEqual(required, set(result.keys()))

    def test_handle_ping_uptime_positive(self):
        result = self.svc.handle_ping({})
        self.assertGreater(result["uptime_sec"], 0.0)

    def test_handle_ping_history_count_from_store(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["history_count"], 5)


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_health_check delegates to HealthChecker
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesHealthCheck(unittest.TestCase):
    """handle_health_check must delegate to HealthChecker.check_all()."""

    def test_delegates_to_health_checker(self):
        checker = _FakeHealthChecker()
        checker.check_all = MagicMock(return_value={"status": "healthy", "checks": {}})
        svc = _make_health_check_service(health_checker=checker)
        result = svc.handle_health_check({})
        checker.check_all.assert_called_once()
        self.assertEqual(result["status"], "healthy")

    def test_handle_health_check_returns_dict(self):
        svc = _make_health_check_service()
        result = svc.handle_health_check({})
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# Helpers to mock heavy deps not available in Xcode Python 3.9
# ---------------------------------------------------------------------------

def _make_profiler_stub():
    """Return a fake profiler module (avoids numpy import)."""
    fake_profiler = MagicMock()
    fake_profiler.get_profile_report.return_value = {
        "methods": {},
        "slowest_methods": [],
        "total_profiled_time_sec": 0.0,
    }
    return fake_profiler


def _make_config_stub():
    """Return a fake config module."""
    fake_config = MagicMock()
    fake_config.settings.MODEL_BALANCED = "mlx-whisper-base"
    fake_config.settings.MODEL_MAX_CANDIDATES = ["mlx-whisper-large"]
    fake_config.settings.DIARIZATION_ENABLED = False
    return fake_config


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_get_diagnostics
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesDiagnostics(unittest.TestCase):
    """handle_get_diagnostics must return all required sections."""

    def _call_with_mocks(self, svc):
        """Call handle_get_diagnostics with mocked heavy dependencies."""
        fake_profiler_module = types.ModuleType("backend.performance_profiler")
        fake_profiler_instance = MagicMock()
        fake_profiler_instance.get_profile_report.return_value = {
            "methods": {},
            "slowest_methods": [],
            "total_profiled_time_sec": 0.0,
        }
        fake_profiler_module.profiler = fake_profiler_instance

        fake_config_module = types.ModuleType("core.config")
        fake_settings = MagicMock()
        fake_settings.MODEL_BALANCED = "mlx-whisper-base"
        fake_settings.MODEL_MAX_CANDIDATES = ["mlx-whisper-large"]
        fake_settings.DIARIZATION_ENABLED = False
        fake_config_module.settings = fake_settings

        with patch.dict(sys.modules, {
            "backend.performance_profiler": fake_profiler_module,
            "core.config": fake_config_module,
        }):
            return svc.handle_get_diagnostics({})

    def setUp(self):
        self.svc = _make_health_check_service()

    def test_has_system_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("system", result)

    def test_has_stt_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("stt", result)

    def test_has_llm_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("llm", result)

    def test_has_history_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("history", result)

    def test_has_settings_cache_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("settings_cache", result)

    def test_has_profiler_section(self):
        result = self._call_with_mocks(self.svc)
        self.assertIn("profiler", result)

    def test_llm_disabled_when_no_rewriter(self):
        result = self._call_with_mocks(self.svc)
        self.assertEqual(result["llm"], {"enabled": False})

    def test_last_engine_from_ref(self):
        ref = ["gigaam-v2"]
        svc = _make_health_check_service(last_stt_engine_ref=ref)
        result = self._call_with_mocks(svc)
        self.assertEqual(result["stt"]["last_engine"], "gigaam-v2")


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_probe_llm_http
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesProbe(unittest.TestCase):
    def test_returns_not_reachable_when_no_rewriter(self):
        svc = _make_health_check_service(llm_rewriter=None)
        result = svc.handle_probe_llm_http({})
        self.assertFalse(result["reachable"])

    def test_returns_reachable_when_warmup_succeeds(self):
        svc = _make_health_check_service(llm_rewriter=_FakeLLMRewriter())
        result = svc.handle_probe_llm_http({})
        self.assertTrue(result["reachable"])

    def test_returns_model_name(self):
        svc = _make_health_check_service(llm_rewriter=_FakeLLMRewriter())
        result = svc.handle_probe_llm_http({})
        self.assertEqual(result["model"], "test-model")


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_get_startup_diagnostics
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesStartupDiagnostics(unittest.TestCase):
    def test_returns_status_ok(self):
        svc = _make_health_check_service()
        result = svc.handle_get_startup_diagnostics({})
        self.assertEqual(result["status"], "ok")

    def test_delegates_to_startup_diagnostics(self):
        sd = _FakeStartupDiagnostics()
        sd.run_all_checks = MagicMock(wraps=sd.run_all_checks)
        svc = _make_health_check_service(startup_diagnostics=sd)
        svc.handle_get_startup_diagnostics({})
        sd.run_all_checks.assert_called_once()


# ---------------------------------------------------------------------------
# Test: HealthCheckService.handle_check_integrity
# ---------------------------------------------------------------------------

class TestHealthCheckServiceDelegatesCheckIntegrity(unittest.TestCase):
    def test_returns_status_ok(self):
        svc = _make_health_check_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["status"], "ok")

    def test_returns_total_items(self):
        svc = _make_health_check_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["total_items"], 3)

    def test_returns_checks_list(self):
        svc = _make_health_check_service()
        result = svc.handle_check_integrity({})
        self.assertIsInstance(result["checks"], list)

    def test_passes_store_data_dir_to_checker(self):
        ic = _FakeIntegrityChecker()
        ic.check_integrity = MagicMock(return_value=_FakeIntegrityChecker._Report())
        store = _FakeStore(data_dir="/tmp/krab_w1187_integrity")
        svc = _make_health_check_service(store=store, integrity_checker=ic)
        svc.handle_check_integrity({})
        ic.check_integrity.assert_called_once_with(Path("/tmp/krab_w1187_integrity"))


# ---------------------------------------------------------------------------
# Test: no inline duplicate health logic remains in service.py
# ---------------------------------------------------------------------------

class TestNoInlineDuplicateHealthLogicRemains(unittest.TestCase):
    """AST-based check: service.py's 6 handler stubs must only delegate.

    Each _handle_* method body must contain exactly one expression statement
    that is a return of a _health_check_svc.handle_* call.
    """

    @classmethod
    def setUpClass(cls):
        service_path = PROJECT_ROOT / "backend" / "service.py"
        cls.source = service_path.read_text()
        cls.tree = ast.parse(cls.source)

    def _get_method_body(self, method_name: str) -> list[ast.stmt]:
        """Return AST body statements for a method named method_name."""
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == method_name:
                    # Skip docstring
                    body = node.body
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                        body = body[1:]
                    return body
        return []

    def _is_delegation_call(self, stmt: ast.stmt, svc_method: str) -> bool:
        """Check stmt is: return self._health_check_svc.<svc_method>(params)"""
        if not isinstance(stmt, ast.Return):
            return False
        call = stmt.value
        if not isinstance(call, ast.Call):
            return False
        func = call.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr != svc_method:
            return False
        obj = func.value
        if not isinstance(obj, ast.Attribute):
            return False
        return obj.attr == "_health_check_svc"

    def test_handle_ping_is_single_delegation(self):
        body = self._get_method_body("_handle_ping")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_ping")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_ping"),
            f"Expected delegation to _health_check_svc.handle_ping, got: {ast.dump(body[0])}"
        )

    def test_handle_health_check_is_single_delegation(self):
        body = self._get_method_body("_handle_health_check")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_health_check")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_health_check"),
            f"Expected delegation to _health_check_svc.handle_health_check, got: {ast.dump(body[0])}"
        )

    def test_handle_get_diagnostics_is_single_delegation(self):
        body = self._get_method_body("_handle_get_diagnostics")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_get_diagnostics")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_get_diagnostics"),
            f"Expected delegation to _health_check_svc.handle_get_diagnostics, got: {ast.dump(body[0])}"
        )

    def test_handle_probe_llm_http_is_single_delegation(self):
        body = self._get_method_body("_handle_probe_llm_http")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_probe_llm_http")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_probe_llm_http"),
            f"Expected delegation to _health_check_svc.handle_probe_llm_http, got: {ast.dump(body[0])}"
        )

    def test_handle_get_startup_diagnostics_is_single_delegation(self):
        body = self._get_method_body("_handle_get_startup_diagnostics")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_get_startup_diagnostics")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_get_startup_diagnostics"),
            f"Expected delegation to _health_check_svc.handle_get_startup_diagnostics, got: {ast.dump(body[0])}"
        )

    def test_handle_check_integrity_is_single_delegation(self):
        body = self._get_method_body("_handle_check_integrity")
        self.assertEqual(len(body), 1, "Expected exactly 1 statement in _handle_check_integrity")
        self.assertTrue(
            self._is_delegation_call(body[0], "handle_check_integrity"),
            f"Expected delegation to _health_check_svc.handle_check_integrity, got: {ast.dump(body[0])}"
        )

    def test_health_check_svc_is_instantiated_in_init(self):
        """_health_check_svc must be assigned in __init__."""
        self.assertIn("self._health_check_svc = HealthCheckService(", self.source)

    def test_health_check_service_imported_in_init(self):
        """HealthCheckService must be imported inside __init__ body."""
        self.assertIn("from backend.health_check_service import HealthCheckService", self.source)


# ---------------------------------------------------------------------------
# Integration: all 6 HealthCheckService handlers return dicts
# ---------------------------------------------------------------------------

class TestHealthCheckServiceAllHandlersReturnDicts(unittest.TestCase):
    """Smoke: all 6 methods are callable and return dicts (no imports needed from heavy deps)."""

    def setUp(self):
        self.svc = _make_health_check_service()
        # Build stub modules for heavy deps not in Xcode Python 3.9 venv
        self._fake_profiler_module = types.ModuleType("backend.performance_profiler")
        fake_profiler_instance = MagicMock()
        fake_profiler_instance.get_profile_report.return_value = {
            "methods": {}, "slowest_methods": [], "total_profiled_time_sec": 0.0
        }
        self._fake_profiler_module.profiler = fake_profiler_instance
        self._fake_config_module = types.ModuleType("core.config")
        fake_settings = MagicMock()
        fake_settings.MODEL_BALANCED = "mlx-whisper-base"
        fake_settings.MODEL_MAX_CANDIDATES = []
        fake_settings.DIARIZATION_ENABLED = False
        self._fake_config_module.settings = fake_settings

    def test_all_handlers_return_dicts(self):
        handlers = [
            ("handle_ping", {}),
            ("handle_health_check", {}),
            ("handle_get_diagnostics", {}),
            ("handle_probe_llm_http", {}),
            ("handle_get_startup_diagnostics", {}),
            ("handle_check_integrity", {}),
        ]
        with patch.dict(sys.modules, {
            "backend.performance_profiler": self._fake_profiler_module,
            "core.config": self._fake_config_module,
        }):
            for name, params in handlers:
                with self.subTest(handler=name):
                    method = getattr(self.svc, name)
                    result = method(params)
                    self.assertIsInstance(result, dict, f"{name} must return dict")


if __name__ == "__main__":
    unittest.main()
