"""Unit-тесты для HealthCheckService.

Покрывает все 6 handle_* методов + integration smoke тест.
Все тесты работают без запуска backend/models.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.health_check_service import HealthCheckService  # noqa: E402


# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------

class FakeStore:
    """Минимальный фейк StateStore."""

    def __init__(self, data_dir: str = "/tmp/krab_test", count: int = 3,
                 raise_on_count: bool = False) -> None:
        self.data_dir = Path(data_dir)
        self._count = count
        self._raise_on_count = raise_on_count

    def count_active_items(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> int:
        if self._raise_on_count:
            raise RuntimeError("store unavailable")
        return self._count


class FakeRecorder:
    is_recording = False


class FakeHealthChecker:
    def check_all(self) -> dict:
        return {"overall": "ok", "checks": []}


class FakeStartupDiagnostics:
    class Report:
        def to_dict(self) -> dict:
            return {"status": "ok", "checks": [], "startup_time_ms": 12.0, "errors": [], "warnings": []}

    def run_all_checks(self):
        return FakeStartupDiagnostics.Report()


class FakeIntegrityChecker:
    class Check:
        def __init__(self):
            self.name = "ndjson_valid"
            self.status = "ok"
            self.message = "All lines valid"
            self.auto_fixable = False

    class Report:
        status = "ok"
        total_items = 5
        orphaned_tombstones = 0
        invalid_json_lines = 0
        checks = []

    def check_integrity(self, data_dir) -> "FakeIntegrityChecker.Report":
        return FakeIntegrityChecker.Report()


class FakeLLMRewriter:
    _last_latency_ms = 42
    _model = "qwen3-4b"

    def warmup(self) -> bool:
        return True

    def passive_health_check(self) -> tuple[bool, bool]:
        return (True, True)

    def status(self) -> dict:
        return {"enabled": True, "reachable": True, "model": self._model}


class FakeSettingsSvc:
    _cache_ttl = 5
    _cache = {}

    def cached_settings(self, nowait: bool = False) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------

def make_service(**kwargs) -> HealthCheckService:
    defaults: dict[str, Any] = dict(
        store=FakeStore(),
        health_checker=FakeHealthChecker(),
        startup_diagnostics=FakeStartupDiagnostics(),
        integrity_checker=FakeIntegrityChecker(),
        llm_probe=None,
        metrics_collector=None,
        transcriber=None,
        llm_rewriter=None,
        settings_svc=FakeSettingsSvc(),
        start_time=time.monotonic() - 10.0,
        app_version="2.0.3-test",
        recorder=FakeRecorder(),
        last_stt_engine_ref=["mlx-whisper"],
    )
    defaults.update(kwargs)
    return HealthCheckService(**defaults)


# ---------------------------------------------------------------------------
# Tests: handle_ping
# ---------------------------------------------------------------------------

class TestHandlePing(unittest.TestCase):
    """КРИТИЧНО: ping контракт bit-exact — HealthMonitor.swift зависит от полей."""

    def setUp(self):
        self.svc = make_service()

    def test_returns_status_ok(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["status"], "ok")

    def test_returns_correct_service_name(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["service"], "krabear-backend")

    def test_returns_version(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["version"], "2.0.3-test")

    def test_uptime_sec_positive(self):
        result = self.svc.handle_ping({})
        self.assertGreater(result["uptime_sec"], 0)
        self.assertIsInstance(result["uptime_sec"], float)

    def test_is_recording_false_by_default(self):
        result = self.svc.handle_ping({})
        self.assertFalse(result["is_recording"])

    def test_is_recording_true_when_recorder_recording(self):
        rec = FakeRecorder()
        rec.is_recording = True
        svc = make_service(recorder=rec)
        result = svc.handle_ping({})
        self.assertTrue(result["is_recording"])

    def test_history_count_from_store(self):
        result = self.svc.handle_ping({})
        self.assertEqual(result["history_count"], 3)

    def test_history_count_minus1_on_store_error(self):
        svc = make_service(store=FakeStore(count=0, raise_on_count=True))
        result = svc.handle_ping({})
        self.assertEqual(result["history_count"], -1)

    def test_contract_all_required_keys_present(self):
        """Все 6 ключей контракта должны присутствовать."""
        result = self.svc.handle_ping({})
        required = {"status", "service", "version", "uptime_sec", "is_recording", "history_count"}
        self.assertEqual(required, set(result.keys()))


# ---------------------------------------------------------------------------
# Tests: handle_health_check
# ---------------------------------------------------------------------------

class TestHandleHealthCheck(unittest.TestCase):
    def test_delegates_to_health_checker(self):
        svc = make_service()
        result = svc.handle_health_check({})
        self.assertEqual(result["overall"], "ok")
        self.assertIn("checks", result)

    def test_health_checker_error_propagates(self):
        checker = FakeHealthChecker()
        checker.check_all = MagicMock(side_effect=RuntimeError("disk full"))
        svc = make_service(health_checker=checker)
        with self.assertRaises(RuntimeError):
            svc.handle_health_check({})


# ---------------------------------------------------------------------------
# Tests: handle_get_diagnostics
# ---------------------------------------------------------------------------

class TestHandleGetDiagnostics(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def test_returns_system_section(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertIn("system", result)
        self.assertIn("python_version", result["system"])
        self.assertIn("platform", result["system"])
        self.assertIn("uptime_sec", result["system"])

    def test_returns_stt_section(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertIn("stt", result)
        stt = result["stt"]
        self.assertIn("diarization_enabled", stt)
        self.assertIn("last_engine", stt)

    def test_returns_llm_disabled_when_no_rewriter(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertEqual(result["llm"], {"enabled": False})

    def test_returns_llm_status_from_rewriter(self):
        svc = make_service(llm_rewriter=FakeLLMRewriter())
        result = svc.handle_get_diagnostics({})
        self.assertTrue(result["llm"]["enabled"])

    def test_returns_history_section(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertIn("history", result)
        self.assertIn("total_items", result["history"])
        self.assertIn("data_dir", result["history"])

    def test_returns_settings_cache_section(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertIn("settings_cache", result)
        self.assertIn("ttl_sec", result["settings_cache"])
        self.assertIn("cached", result["settings_cache"])

    def test_returns_profiler_section(self):
        result = self.svc.handle_get_diagnostics({})
        self.assertIn("profiler", result)

    def test_last_engine_from_ref(self):
        ref = ["gigaam-v2"]
        svc = make_service(last_stt_engine_ref=ref)
        result = svc.handle_get_diagnostics({})
        self.assertEqual(result["stt"]["last_engine"], "gigaam-v2")

    def test_last_engine_ref_live_update(self):
        """Обновление ref[0] в BackendService должно отразиться в get_diagnostics."""
        ref = ["mlx-whisper"]
        svc = make_service(last_stt_engine_ref=ref)
        ref[0] = "gigaam-v2"  # simulate BackendService update
        result = svc.handle_get_diagnostics({})
        self.assertEqual(result["stt"]["last_engine"], "gigaam-v2")


# ---------------------------------------------------------------------------
# Tests: handle_probe_llm_http
# ---------------------------------------------------------------------------

class TestHandleProbeLlmHttp(unittest.TestCase):
    def test_returns_not_reachable_when_no_rewriter(self):
        svc = make_service(llm_rewriter=None)
        result = svc.handle_probe_llm_http({})
        self.assertFalse(result["reachable"])
        self.assertEqual(result["latency_ms"], 0)
        self.assertIsNone(result["model"])

    def test_returns_reachable_when_warmup_succeeds(self):
        svc = make_service(llm_rewriter=FakeLLMRewriter())
        result = svc.handle_probe_llm_http({})
        self.assertTrue(result["reachable"])

    def test_returns_latency_ms(self):
        # Латентность — ЗАМЕР пассивного пинга, а не хвост последнего rewrite (_last_latency_ms).
        svc = make_service(llm_rewriter=FakeLLMRewriter())
        result = svc.handle_probe_llm_http({})
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)

    def test_returns_model_name(self):
        svc = make_service(llm_rewriter=FakeLLMRewriter())
        result = svc.handle_probe_llm_http({})
        self.assertEqual(result["model"], "qwen3-4b")

    def test_returns_not_reachable_when_passive_check_fails(self):
        rw = FakeLLMRewriter()
        rw.passive_health_check = lambda: (False, False)
        svc = make_service(llm_rewriter=rw)
        result = svc.handle_probe_llm_http({})
        self.assertFalse(result["reachable"])


# ---------------------------------------------------------------------------
# Tests: handle_get_startup_diagnostics
# ---------------------------------------------------------------------------

class TestHandleGetStartupDiagnostics(unittest.TestCase):
    def test_returns_status_ok(self):
        svc = make_service()
        result = svc.handle_get_startup_diagnostics({})
        self.assertEqual(result["status"], "ok")

    def test_returns_checks_list(self):
        svc = make_service()
        result = svc.handle_get_startup_diagnostics({})
        self.assertIn("checks", result)

    def test_returns_startup_time_ms(self):
        svc = make_service()
        result = svc.handle_get_startup_diagnostics({})
        self.assertIn("startup_time_ms", result)

    def test_delegated_to_startup_diagnostics(self):
        sd = FakeStartupDiagnostics()
        sd.run_all_checks = MagicMock(wraps=sd.run_all_checks)
        svc = make_service(startup_diagnostics=sd)
        svc.handle_get_startup_diagnostics({})
        sd.run_all_checks.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: handle_check_integrity
# ---------------------------------------------------------------------------

class TestHandleCheckIntegrity(unittest.TestCase):
    def test_returns_status(self):
        svc = make_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["status"], "ok")

    def test_returns_total_items(self):
        svc = make_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["total_items"], 5)

    def test_returns_orphaned_tombstones(self):
        svc = make_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["orphaned_tombstones"], 0)

    def test_returns_invalid_json_lines(self):
        svc = make_service()
        result = svc.handle_check_integrity({})
        self.assertEqual(result["invalid_json_lines"], 0)

    def test_returns_checks_list(self):
        svc = make_service()
        result = svc.handle_check_integrity({})
        self.assertIsInstance(result["checks"], list)

    def test_check_items_have_required_fields(self):
        """Каждый элемент checks должен иметь 4 поля контракта."""
        checker = FakeIntegrityChecker()
        check = FakeIntegrityChecker.Check()
        checker.check_integrity = MagicMock(
            return_value=type("R", (), {
                "status": "ok",
                "total_items": 1,
                "orphaned_tombstones": 0,
                "invalid_json_lines": 0,
                "checks": [check],
            })()
        )
        svc = make_service(integrity_checker=checker)
        result = svc.handle_check_integrity({})
        self.assertEqual(len(result["checks"]), 1)
        item = result["checks"][0]
        for field in ("name", "status", "message", "auto_fixable"):
            self.assertIn(field, item)

    def test_passes_store_data_dir_to_checker(self):
        ic = FakeIntegrityChecker()
        ic.check_integrity = MagicMock(return_value=FakeIntegrityChecker.Report())
        store = FakeStore(data_dir="/tmp/krab_test_datadir")
        svc = make_service(store=store, integrity_checker=ic)
        svc.handle_check_integrity({})
        ic.check_integrity.assert_called_once_with(Path("/tmp/krab_test_datadir"))


# ---------------------------------------------------------------------------
# Integration smoke test
# ---------------------------------------------------------------------------

class TestHealthCheckServiceIntegration(unittest.TestCase):
    """Smoke test: все 6 методов работают на реальных import'ах (без heavy deps)."""

    def test_all_handlers_callable_and_return_dicts(self):
        svc = make_service()
        handlers = [
            ("ping", {}),
            ("health_check", {}),
            ("get_diagnostics", {}),
            ("probe_llm_http", {}),
            ("get_startup_diagnostics", {}),
            ("check_integrity", {}),
        ]
        for name, params in handlers:
            with self.subTest(handler=name):
                method = getattr(svc, f"handle_{name}")
                result = method(params)
                self.assertIsInstance(result, dict, f"handle_{name} must return dict")

    def test_ping_uptime_increases_over_time(self):
        start = time.monotonic() - 100.0
        svc = make_service(start_time=start)
        r1 = svc.handle_ping({})
        time.sleep(0.01)
        r2 = svc.handle_ping({})
        self.assertGreaterEqual(r2["uptime_sec"], r1["uptime_sec"])


if __name__ == "__main__":
    unittest.main()
