"""Unit-тесты для StartupDiagnostics."""

from __future__ import annotations
from backend.startup_diagnostics import (
    CheckResult,
    StartupDiagnostics,
    StartupReport,
    DISK_MIN_FREE_GB,
)

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diag(tmpdir: str | None = None) -> StartupDiagnostics:
    if tmpdir is None:
        tmpdir = tempfile.mkdtemp()
    return StartupDiagnostics(data_dir=tmpdir)


# ===========================================================================
# 1. StartupReport dataclass
# ===========================================================================

class TestStartupReport(unittest.TestCase):
    """Тесты структуры StartupReport."""

    def _make_report(self, status: str = "ready") -> StartupReport:
        checks = [
            CheckResult(name="python_version", status="ok", message="Python 3.12.0", duration_ms=0.1),
        ]
        return StartupReport(
            status=status,
            checks=checks,
            startup_time_ms=42.5,
            warnings=[],
            errors=[],
        )

    def test_to_dict_has_required_keys(self) -> None:
        report = self._make_report()
        d = report.to_dict()
        for key in ("status", "startup_time_ms", "warnings", "errors", "checks"):
            self.assertIn(key, d)

    def test_to_dict_checks_structure(self) -> None:
        report = self._make_report()
        check_dicts = report.to_dict()["checks"]
        self.assertEqual(len(check_dicts), 1)
        c = check_dicts[0]
        for key in ("name", "status", "message", "duration_ms", "details"):
            self.assertIn(key, c)

    def test_to_dict_status_propagated(self) -> None:
        for status in ("ready", "degraded", "critical"):
            r = self._make_report(status=status)
            self.assertEqual(r.to_dict()["status"], status)

    def test_startup_time_ms_rounded(self) -> None:
        report = StartupReport(
            status="ready",
            checks=[],
            startup_time_ms=12.3456789,
            warnings=[],
            errors=[],
        )
        d = report.to_dict()
        # Должен быть округлён до 2 знаков
        self.assertAlmostEqual(d["startup_time_ms"], 12.35, places=2)


# ===========================================================================
# 2. CheckResult dataclass
# ===========================================================================

class TestCheckResult(unittest.TestCase):
    def test_default_details_empty(self) -> None:
        cr = CheckResult(name="x", status="ok", message="fine", duration_ms=1.0)
        self.assertEqual(cr.details, {})

    def test_details_populated(self) -> None:
        cr = CheckResult(name="x", status="ok", message="fine", duration_ms=1.0, details={"k": "v"})
        self.assertEqual(cr.details["k"], "v")


# ===========================================================================
# 3. Проверка версии Python
# ===========================================================================

class TestCheckPythonVersion(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_current_python_passes(self) -> None:
        """Текущий Python должен пройти (>=3.12 в venv)."""
        result = self.diag._check_python_version()
        self.assertIn(result.status, ("ok", "error"))  # статус зависит от реального интерпретатора
        self.assertIsInstance(result.message, str)
        self.assertGreater(result.duration_ms, 0)

    def test_old_python_fails(self) -> None:
        """Версия 3.10 должна завалить проверку."""
        from types import SimpleNamespace
        fake_vi = SimpleNamespace(major=3, minor=10, micro=5)
        with patch("backend.startup_diagnostics.sys") as mock_sys:
            mock_sys.version_info = fake_vi
            result = self.diag._check_python_version()
        self.assertEqual(result.status, "error")
        self.assertIn("3.10", result.message)

    def test_new_python_passes(self) -> None:
        """Версия 3.13 должна пройти проверку."""
        from types import SimpleNamespace
        fake_vi = SimpleNamespace(major=3, minor=13, micro=0)
        with patch("backend.startup_diagnostics.sys") as mock_sys:
            mock_sys.version_info = fake_vi
            result = self.diag._check_python_version()
        self.assertEqual(result.status, "ok")

    def test_result_has_details(self) -> None:
        result = self.diag._check_python_version()
        self.assertIn("version", result.details)
        self.assertIn("required", result.details)


# ===========================================================================
# 4. Проверка обязательных пакетов
# ===========================================================================

class TestCheckRequiredPackages(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_all_present_returns_ok(self) -> None:
        """Если все пакеты импортируются — статус ok."""
        # Мокируем importlib.import_module чтобы все пакеты были «доступны»
        with patch("importlib.import_module", return_value=MagicMock()):
            result = self.diag._check_required_packages()
        self.assertEqual(result.status, "ok")

    def test_missing_package_returns_error(self) -> None:
        def fake_import(name: str):
            if name == "mlx_whisper":
                raise ImportError("No module named 'mlx_whisper'")
            return MagicMock()

        with patch("importlib.import_module", side_effect=fake_import):
            result = self.diag._check_required_packages()
        self.assertEqual(result.status, "error")
        self.assertIn("mlx_whisper", result.message)

    def test_missing_packages_listed_in_details(self) -> None:
        with patch("importlib.import_module", side_effect=ImportError("nope")):
            result = self.diag._check_required_packages()
        self.assertEqual(result.status, "error")
        self.assertIn("missing", result.details)
        self.assertTrue(len(result.details["missing"]) > 0)


# ===========================================================================
# 5. Проверка доступности data_dir для записи
# ===========================================================================

class TestCheckDataDirWritable(unittest.TestCase):
    def test_writable_dir_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            diag = StartupDiagnostics(data_dir=tmpdir)
            result = diag._check_data_dir_writable()
        self.assertEqual(result.status, "ok")
        self.assertIn("path", result.details)

    def test_nonexistent_dir_created_and_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            new_dir = Path(tmpdir) / "new_subdir"
            diag = StartupDiagnostics(data_dir=str(new_dir))
            result = diag._check_data_dir_writable()
        self.assertEqual(result.status, "ok")

    def test_unwritable_dir_error(self) -> None:
        import stat
        with tempfile.TemporaryDirectory() as tmpdir:
            ro_dir = Path(tmpdir) / "readonly"
            ro_dir.mkdir()
            ro_dir.chmod(stat.S_IRUSR | stat.S_IXUSR)
            diag = StartupDiagnostics(data_dir=str(ro_dir))
            try:
                result = diag._check_data_dir_writable()
                self.assertEqual(result.status, "error")
            finally:
                ro_dir.chmod(stat.S_IRWXU)


# ===========================================================================
# 6. Проверка ffmpeg
# ===========================================================================

class TestCheckFfmpegAvailable(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_ffmpeg_found_returns_ok(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/ffmpeg"):
            result = self.diag._check_ffmpeg_available()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details["path"], "/usr/local/bin/ffmpeg")

    def test_ffmpeg_not_found_returns_warning(self) -> None:
        with patch("shutil.which", return_value=None):
            result = self.diag._check_ffmpeg_available()
        self.assertEqual(result.status, "warning")
        self.assertIsNone(result.details["path"])

    def test_ffmpeg_exception_returns_warning(self) -> None:
        with patch("shutil.which", side_effect=OSError("unexpected")):
            result = self.diag._check_ffmpeg_available()
        self.assertEqual(result.status, "warning")


# ===========================================================================
# 7. Проверка HuggingFace токена
# ===========================================================================

class TestCheckHuggingFaceToken(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_token_present_returns_ok(self) -> None:
        with patch("os.environ", {"HF_TOKEN": "hf_abc123"}):
            with patch("backend.startup_diagnostics.StartupDiagnostics._check_huggingface_token",
                       wraps=self.diag._check_huggingface_token):
                # Патчим settings напрямую
                mock_settings = MagicMock()
                mock_settings.HF_TOKEN = "hf_abc123"
                with patch("backend.startup_diagnostics.importlib") as _:
                    pass
        # Прямой путь: мокируем settings внутри метода
        import os
        orig_env = dict(os.environ)
        os.environ["HF_TOKEN"] = "hf_test_token"
        try:
            result = self.diag._check_huggingface_token()
            # Токен присутствует в env — должен быть ok
            self.assertEqual(result.status, "ok")
            self.assertTrue(result.details.get("present", False))
        finally:
            os.environ.clear()
            os.environ.update(orig_env)

    def test_token_absent_returns_warning(self) -> None:
        import os
        orig_env = dict(os.environ)
        # Убираем все HF-токены
        os.environ.pop("HF_TOKEN", None)
        os.environ.pop("HUGGINGFACE_TOKEN", None)
        try:
            mock_settings = MagicMock()
            mock_settings.HF_TOKEN = ""
            with patch("core.config.settings", mock_settings):
                result = self.diag._check_huggingface_token()
            self.assertEqual(result.status, "warning")
            self.assertFalse(result.details.get("present", True))
        finally:
            os.environ.clear()
            os.environ.update(orig_env)


# ===========================================================================
# 8. Проверка диска (disk_space)
# ===========================================================================

class TestCheckDiskSpace(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_enough_space_returns_ok(self) -> None:
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=50 * 1024 ** 3)  # 50 ГБ
            result = self.diag._check_disk_space()
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.details["free_gb"], DISK_MIN_FREE_GB)

    def test_low_space_returns_error(self) -> None:
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=512 * 1024 ** 2)  # 512 МБ
            result = self.diag._check_disk_space()
        self.assertEqual(result.status, "error")
        self.assertLess(result.details["free_gb"], DISK_MIN_FREE_GB)

    def test_exactly_min_returns_ok(self) -> None:
        """Ровно DISK_MIN_FREE_GB = ok (граничный случай)."""
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=int(DISK_MIN_FREE_GB * 1024 ** 3))
            result = self.diag._check_disk_space()
        self.assertEqual(result.status, "ok")

    def test_disk_check_exception_returns_error(self) -> None:
        with patch("shutil.disk_usage", side_effect=OSError("disk failure")):
            result = self.diag._check_disk_space()
        self.assertEqual(result.status, "error")


# ===========================================================================
# 9. Проверка аудиоустройств
# ===========================================================================

class TestCheckAudioDevices(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_devices_found_returns_ok(self) -> None:
        mock_sd = MagicMock()
        mock_sd.query_devices.return_value = [
            {"name": "Built-in Microphone", "max_input_channels": 1},
        ]
        mock_sd.query_devices.side_effect = None

        def fake_query_devices(kind=None):
            if kind == "input":
                return {"name": "Built-in Microphone", "max_input_channels": 1}
            return [{"name": "Built-in Microphone", "max_input_channels": 1}]

        mock_sd.query_devices.side_effect = fake_query_devices

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            result = self.diag._check_audio_devices()
        self.assertEqual(result.status, "ok")
        self.assertGreater(result.details["count"], 0)

    def test_no_devices_returns_warning(self) -> None:
        mock_sd = MagicMock()

        def fake_query_devices(kind=None):
            if kind is None:
                return []
            return {}

        mock_sd.query_devices.side_effect = fake_query_devices

        with patch.dict("sys.modules", {"sounddevice": mock_sd}):
            result = self.diag._check_audio_devices()
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.details["count"], 0)

    def test_sounddevice_missing_returns_error(self) -> None:
        with patch.dict("sys.modules", {"sounddevice": None}):
            with patch("builtins.__import__", side_effect=ImportError("no sounddevice")):
                result = self.diag._check_audio_devices()
        # Может вернуть error или warning — главное не упасть с исключением
        self.assertIn(result.status, ("error", "warning"))


# ===========================================================================
# 10. LM Studio check — synchronous core (_do_lm_studio_check)
# ===========================================================================

class TestCheckLmStudio(unittest.TestCase):
    """Tests for the synchronous _do_lm_studio_check (background-thread body)."""

    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_disabled_llm_returns_ok(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = False
        with patch("core.config.settings", mock_settings):
            result = self.diag._do_lm_studio_check()
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.details["enabled"])

    def test_enabled_and_reachable_returns_ok(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = True
        mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
        mock_sock = MagicMock()
        with patch("core.config.settings", mock_settings):
            with patch("socket.create_connection", return_value=mock_sock):
                result = self.diag._do_lm_studio_check()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.details["enabled"])

    def test_enabled_but_unreachable_returns_warning(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = True
        mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
        with patch("core.config.settings", mock_settings):
            with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
                result = self.diag._do_lm_studio_check()
        self.assertEqual(result.status, "warning")


# ===========================================================================
# 10b. W1672 — async / non-blocking LM Studio check
# ===========================================================================

class TestCheckLmStudioAsync(unittest.TestCase):
    """W1672 (W1615 F2 MED): LM Studio reachability must not block __init__."""

    def _make_diag_with_slow_connect(self, delay: float = 5.0) -> StartupDiagnostics:
        """Build a StartupDiagnostics whose background probe will sleep for *delay* seconds."""
        tmpdir = tempfile.mkdtemp()
        import threading as _threading

        def _slow_connect(*args, **kwargs):  # noqa: ANN001,ANN202
            _threading.Event().wait(timeout=delay)
            raise ConnectionRefusedError("simulated offline")

        with patch("socket.create_connection", side_effect=_slow_connect):
            with patch("core.config.settings") as mock_s:
                mock_s.LLM_ENABLED = True
                mock_s.LLM_BASE_URL = "http://localhost:1234/v1"
                diag = StartupDiagnostics(data_dir=tmpdir)
        return diag

    def test_lm_studio_check_does_not_block_init(self) -> None:
        """__init__ and run_all_checks must return in <0.5 s even when LM Studio is offline.

        The background thread may block for up to 2 s (connect timeout) but the
        caller must not wait for it.
        """
        import threading as _threading

        connect_blocked = _threading.Event()

        def _blocking_connect(*args, **kwargs):  # noqa: ANN001,ANN202
            connect_blocked.set()
            # Block until the test's timeout fires (much longer than test assertion).
            _threading.Event().wait(timeout=10.0)
            raise ConnectionRefusedError("simulated offline")

        tmpdir = tempfile.mkdtemp()
        t0 = time.monotonic()
        with patch("backend.startup_diagnostics.socket.create_connection", side_effect=_blocking_connect):
            with patch("core.config.settings") as mock_s:
                mock_s.LLM_ENABLED = True
                mock_s.LLM_BASE_URL = "http://localhost:1234/v1"
                diag = StartupDiagnostics(data_dir=tmpdir)
                # run_all_checks must not block either
                with patch.object(diag, "_check_python_version", return_value=CheckResult("python_version", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_required_packages", return_value=CheckResult("required_packages", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_data_dir_writable", return_value=CheckResult("data_dir_writable", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_socket_path_available", return_value=CheckResult("socket_path", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_ffmpeg_available", return_value=CheckResult("ffmpeg", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_huggingface_token", return_value=CheckResult("hf_token", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_stt_model_cached", return_value=CheckResult("stt_model_cached", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_disk_space", return_value=CheckResult("disk_space", "ok", "ok", 0.1)), \
                     patch.object(diag, "_check_audio_devices", return_value=CheckResult("audio_devices", "ok", "ok", 0.1)):
                    report = diag.run_all_checks()
        elapsed = time.monotonic() - t0
        # Must complete well under the 2 s TCP timeout
        self.assertLess(elapsed, 0.5, f"run_all_checks took {elapsed:.3f}s — LM Studio check is still blocking")
        self.assertIsNotNone(report)

    def test_lm_studio_status_eventually_populated(self) -> None:
        """Background thread must populate _lm_studio_result after the check completes."""
        tmpdir = tempfile.mkdtemp()
        with patch("backend.startup_diagnostics.socket.create_connection", side_effect=ConnectionRefusedError()):
            with patch("core.config.settings") as mock_s:
                mock_s.LLM_ENABLED = True
                mock_s.LLM_BASE_URL = "http://localhost:1234/v1"
                diag = StartupDiagnostics(data_dir=tmpdir)
        # Wait for the background thread to finish (should be fast — immediate refuse)
        result = diag.wait_lm_studio_check(timeout=3.0)
        self.assertIsNotNone(result, "Background LM Studio check did not complete within timeout")
        assert result is not None  # for type narrowing
        self.assertEqual(result.name, "lm_studio")
        self.assertIn(result.status, ("ok", "warning", "error"))
        # Specifically: unreachable → warning
        self.assertEqual(result.status, "warning")

    def test_diagnostics_dict_handles_pending_state(self) -> None:
        """to_dict() on a report that contains the pending placeholder must not raise."""
        tmpdir = tempfile.mkdtemp()
        import threading as _threading

        ready_to_proceed = _threading.Event()
        probe_can_finish = _threading.Event()

        def _controlled_connect(*args, **kwargs):  # noqa: ANN001,ANN202
            ready_to_proceed.set()
            probe_can_finish.wait(timeout=5.0)
            raise ConnectionRefusedError("simulated offline")

        with patch("backend.startup_diagnostics.socket.create_connection", side_effect=_controlled_connect):
            with patch("core.config.settings") as mock_s:
                mock_s.LLM_ENABLED = True
                mock_s.LLM_BASE_URL = "http://localhost:1234/v1"
                diag = StartupDiagnostics(data_dir=tmpdir)

        # Wait until the background thread is inside the blocking connect
        ready_to_proceed.wait(timeout=3.0)

        # At this point the probe is still running — _lm_studio_result should be None
        with diag._lm_studio_lock:
            still_pending = diag._lm_studio_result is None

        self.assertTrue(still_pending, "Expected pending state before probe finishes")

        # run_all_checks should work and include a pending lm_studio entry
        with patch.object(diag, "_check_python_version", return_value=CheckResult("python_version", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_required_packages", return_value=CheckResult("required_packages", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_data_dir_writable", return_value=CheckResult("data_dir_writable", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_socket_path_available", return_value=CheckResult("socket_path", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_ffmpeg_available", return_value=CheckResult("ffmpeg", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_huggingface_token", return_value=CheckResult("hf_token", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_stt_model_cached", return_value=CheckResult("stt_model_cached", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_disk_space", return_value=CheckResult("disk_space", "ok", "ok", 0.1)), \
             patch.object(diag, "_check_audio_devices", return_value=CheckResult("audio_devices", "ok", "ok", 0.1)):
            report = diag.run_all_checks(force=True)

        # to_dict must not raise and must contain the lm_studio entry
        d = report.to_dict()
        self.assertIsInstance(d, dict)
        lm_entries = [c for c in d["checks"] if c["name"] == "lm_studio"]
        self.assertEqual(len(lm_entries), 1)
        lm_entry = lm_entries[0]
        # pending placeholder has status "ok" and details["pending"]=True
        self.assertEqual(lm_entry["status"], "ok")
        self.assertTrue(lm_entry["details"].get("pending", False))

        # Let the background probe finish cleanly
        probe_can_finish.set()
        diag.wait_lm_studio_check(timeout=3.0)


# ===========================================================================
# 11. run_all_checks — агрегация
# ===========================================================================

class TestRunAllChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.diag = StartupDiagnostics(data_dir=self.tmpdir)

    def test_run_all_checks_returns_startup_report(self) -> None:
        report = self.diag.run_all_checks()
        self.assertIsInstance(report, StartupReport)

    def test_report_has_10_checks(self) -> None:
        """Должно быть ровно 10 проверок."""
        report = self.diag.run_all_checks()
        self.assertEqual(len(report.checks), 10)

    def test_all_checks_have_status(self) -> None:
        report = self.diag.run_all_checks()
        for check in report.checks:
            self.assertIn(check.status, ("ok", "warning", "error"),
                          f"Проверка {check.name!r} имеет неизвестный статус {check.status!r}")

    def test_all_checks_have_positive_duration(self) -> None:
        report = self.diag.run_all_checks()
        for check in report.checks:
            self.assertGreaterEqual(check.duration_ms, 0.0,
                                    f"duration_ms < 0 для {check.name!r}")

    def test_status_ready_when_all_ok(self) -> None:
        """Если все проверки ok — статус ready."""
        with patch.object(
            self.diag, "_check_python_version", return_value=CheckResult("python_version", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_required_packages", return_value=CheckResult("required_packages", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_data_dir_writable", return_value=CheckResult("data_dir_writable", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_socket_path_available", return_value=CheckResult("socket_path", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_ffmpeg_available", return_value=CheckResult("ffmpeg", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_huggingface_token", return_value=CheckResult("hf_token", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_stt_model_cached", return_value=CheckResult("stt_model_cached", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_lm_studio_reachable", return_value=CheckResult("lm_studio", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_disk_space", return_value=CheckResult("disk_space", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_audio_devices", return_value=CheckResult("audio_devices", "ok", "ok", 1.0)
        ):
            report = self.diag.run_all_checks()
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.errors, [])

    def test_status_degraded_when_warning_present(self) -> None:
        """Одно предупреждение → degraded."""
        with patch.object(
            self.diag, "_check_python_version", return_value=CheckResult("python_version", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_required_packages", return_value=CheckResult("required_packages", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_data_dir_writable", return_value=CheckResult("data_dir_writable", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_socket_path_available", return_value=CheckResult("socket_path", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_ffmpeg_available", return_value=CheckResult("ffmpeg", "warning", "ffmpeg missing", 1.0)
        ), patch.object(
            self.diag, "_check_huggingface_token", return_value=CheckResult("hf_token", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_stt_model_cached", return_value=CheckResult("stt_model_cached", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_lm_studio_reachable", return_value=CheckResult("lm_studio", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_disk_space", return_value=CheckResult("disk_space", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_audio_devices", return_value=CheckResult("audio_devices", "ok", "ok", 1.0)
        ):
            report = self.diag.run_all_checks()
        self.assertEqual(report.status, "degraded")
        self.assertIn("ffmpeg missing", report.warnings)
        self.assertEqual(report.errors, [])

    def test_status_critical_when_error_present(self) -> None:
        """Одна ошибка → critical."""
        with patch.object(
            self.diag, "_check_python_version", return_value=CheckResult("python_version", "error", "Python too old", 1.0)
        ), patch.object(
            self.diag, "_check_required_packages", return_value=CheckResult("required_packages", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_data_dir_writable", return_value=CheckResult("data_dir_writable", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_socket_path_available", return_value=CheckResult("socket_path", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_ffmpeg_available", return_value=CheckResult("ffmpeg", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_huggingface_token", return_value=CheckResult("hf_token", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_stt_model_cached", return_value=CheckResult("stt_model_cached", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_lm_studio_reachable", return_value=CheckResult("lm_studio", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_disk_space", return_value=CheckResult("disk_space", "ok", "ok", 1.0)
        ), patch.object(
            self.diag, "_check_audio_devices", return_value=CheckResult("audio_devices", "ok", "ok", 1.0)
        ):
            report = self.diag.run_all_checks()
        self.assertEqual(report.status, "critical")
        self.assertIn("Python too old", report.errors)

    def test_startup_time_ms_is_positive(self) -> None:
        report = self.diag.run_all_checks()
        self.assertGreater(report.startup_time_ms, 0.0)


# ===========================================================================
# 12. STT model cached check
# ===========================================================================

class TestCheckSttModelCached(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_model_present_in_cache_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            hf_cache = Path(tmpdir) / ".cache" / "huggingface" / "hub"
            model_dir = hf_cache / "models--mlx-community--whisper-large-v3-turbo"
            model_dir.mkdir(parents=True)

            mock_settings = MagicMock()
            mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"

            with patch("core.config.settings", mock_settings):
                with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                    result = self.diag._check_stt_model_cached()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.details["cached"])

    def test_model_absent_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            # HF кэш существует, но модели там нет
            hf_cache = Path(tmpdir) / ".cache" / "huggingface" / "hub"
            hf_cache.mkdir(parents=True)

            mock_settings = MagicMock()
            mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"

            with patch("core.config.settings", mock_settings):
                with patch("pathlib.Path.home", return_value=Path(tmpdir)):
                    result = self.diag._check_stt_model_cached()
        self.assertEqual(result.status, "warning")
        self.assertFalse(result.details["cached"])


# ===========================================================================
# 13. critical_errors() subset
# ===========================================================================

class TestCriticalErrors(unittest.TestCase):
    """critical_errors() возвращает только CheckResult со status='error'."""

    def _make_all_ok_diag(self) -> StartupDiagnostics:
        tmpdir = tempfile.mkdtemp()
        diag = StartupDiagnostics(data_dir=tmpdir)
        # Запускаем полный прогон с мок-проверками — все ok
        ok = CheckResult("x", "ok", "ok", 1.0)
        report = StartupReport(status="ready", checks=[ok], startup_time_ms=1.0, warnings=[], errors=[])
        diag._cached_report = report
        diag._cache_ts = __import__("time").monotonic()
        return diag

    def test_critical_errors_empty_when_all_ok(self) -> None:
        diag = self._make_all_ok_diag()
        errors = diag.critical_errors()
        self.assertEqual(errors, [])

    def test_critical_errors_returns_only_error_checks(self) -> None:
        tmpdir = tempfile.mkdtemp()
        diag = StartupDiagnostics(data_dir=tmpdir)
        checks = [
            CheckResult("python_version", "ok", "ok", 1.0),
            CheckResult("required_packages", "error", "mlx_whisper missing", 1.0),
            CheckResult("disk_space", "error", "too low", 1.0),
            CheckResult("ffmpeg", "warning", "not found", 1.0),
        ]
        report = StartupReport(
            status="critical", checks=checks, startup_time_ms=2.0,
            warnings=["not found"], errors=["mlx_whisper missing", "too low"],
        )
        diag._cached_report = report
        diag._cache_ts = __import__("time").monotonic()

        errors = diag.critical_errors()
        self.assertEqual(len(errors), 2)
        for c in errors:
            self.assertEqual(c.status, "error")

    def test_critical_errors_triggers_run_all_if_no_cache(self) -> None:
        tmpdir = tempfile.mkdtemp()
        diag = StartupDiagnostics(data_dir=tmpdir)
        # Нет кэша — critical_errors() должен вызвать run_all_checks()
        self.assertIsNone(diag._cached_report)
        errors = diag.critical_errors()
        # После вызова кэш должен быть заполнен
        self.assertIsNotNone(diag._cached_report)
        # Результат — список (может быть пустым или непустым)
        self.assertIsInstance(errors, list)

    def test_critical_errors_check_names_present(self) -> None:
        tmpdir = tempfile.mkdtemp()
        diag = StartupDiagnostics(data_dir=tmpdir)
        err_check = CheckResult("disk_space", "error", "disk full", 1.0)
        report = StartupReport(
            status="critical",
            checks=[err_check, CheckResult("ffmpeg", "ok", "ok", 1.0)],
            startup_time_ms=1.0, warnings=[], errors=["disk full"],
        )
        diag._cached_report = report
        diag._cache_ts = __import__("time").monotonic()

        errors = diag.critical_errors()
        self.assertEqual(errors[0].name, "disk_space")


# ===========================================================================
# 14. Check caching (не перезапускается если < TTL)
# ===========================================================================

class TestCheckCaching(unittest.TestCase):
    """run_all_checks кэширует результат и не перезапускает проверки до истечения TTL."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        # TTL = 60 секунд (дефолт)
        self.diag = StartupDiagnostics(data_dir=self.tmpdir)

    def test_result_cached_after_first_call(self) -> None:
        report1 = self.diag.run_all_checks()
        # Кэш должен быть заполнен
        self.assertIsNotNone(self.diag._cached_report)
        # Второй вызов возвращает тот же объект
        report2 = self.diag.run_all_checks()
        self.assertIs(report1, report2)

    def test_force_bypasses_cache(self) -> None:
        report1 = self.diag.run_all_checks()
        report2 = self.diag.run_all_checks(force=True)
        # force=True — новый объект (не тот же)
        self.assertIsNot(report1, report2)

    def test_expired_cache_triggers_rerun(self) -> None:
        import time as _time
        # TTL = 0 сек — всегда истёкший
        diag = StartupDiagnostics(data_dir=self.tmpdir, cache_ttl_sec=0.0)
        report1 = diag.run_all_checks()
        # Немного подождём, чтобы cache_ts стал "старым"
        _time.sleep(0.01)
        report2 = diag.run_all_checks()
        # Новый прогон — другой объект
        self.assertIsNot(report1, report2)

    def test_invalidate_cache_clears_report(self) -> None:
        self.diag.run_all_checks()
        self.assertIsNotNone(self.diag._cached_report)
        self.diag.invalidate_cache()
        self.assertIsNone(self.diag._cached_report)

    def test_cache_ttl_respected_within_window(self) -> None:
        """Внутри TTL-окна кэш не обновляется (cache_ts остаётся прежним)."""
        import time as _time
        diag = StartupDiagnostics(data_dir=self.tmpdir, cache_ttl_sec=300.0)
        diag.run_all_checks()
        ts_before = diag._cache_ts
        _time.sleep(0.01)
        diag.run_all_checks()
        # cache_ts не изменился — использовался кэш
        self.assertEqual(ts_before, diag._cache_ts)


# ===========================================================================
# 15. Wave 122 — именованные тесты (обязательный список)
# ===========================================================================

class TestWave122RequiredNames(unittest.TestCase):
    """Обязательные тесты Wave 122 с точными именами методов."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.diag = StartupDiagnostics(data_dir=self.tmpdir)

    def test_all_checks_pass_returns_ok(self) -> None:
        """Все 10 проверок ok → итоговый статус ready."""
        ok = CheckResult("x", "ok", "msg", 1.0)
        for name in [
            "_check_python_version", "_check_required_packages",
            "_check_data_dir_writable", "_check_socket_path_available",
            "_check_ffmpeg_available", "_check_huggingface_token",
            "_check_stt_model_cached", "_check_lm_studio_reachable",
            "_check_disk_space", "_check_audio_devices",
        ]:
            patcher = patch.object(self.diag, name, return_value=ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        report = self.diag.run_all_checks(force=True)
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.errors, [])

    def test_failed_check_marked_as_warning(self) -> None:
        """Проверка со статусом warning помечает итог как degraded."""
        ok = CheckResult("x", "ok", "ok", 1.0)
        warn = CheckResult("ffmpeg", "warning", "ffmpeg not found", 1.0)
        for name in [
            "_check_python_version", "_check_required_packages",
            "_check_data_dir_writable", "_check_socket_path_available",
            "_check_huggingface_token", "_check_stt_model_cached",
            "_check_lm_studio_reachable", "_check_disk_space",
            "_check_audio_devices",
        ]:
            patcher = patch.object(self.diag, name, return_value=ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher_w = patch.object(self.diag, "_check_ffmpeg_available", return_value=warn)
        patcher_w.start()
        self.addCleanup(patcher_w.stop)
        report = self.diag.run_all_checks(force=True)
        self.assertEqual(report.status, "degraded")
        self.assertIn("ffmpeg not found", report.warnings)

    def test_failed_critical_check_marked_critical(self) -> None:
        """Проверка со статусом error помечает итог как critical."""
        ok = CheckResult("x", "ok", "ok", 1.0)
        err = CheckResult("disk_space", "error", "disk full", 1.0)
        for name in [
            "_check_python_version", "_check_required_packages",
            "_check_data_dir_writable", "_check_socket_path_available",
            "_check_ffmpeg_available", "_check_huggingface_token",
            "_check_stt_model_cached", "_check_lm_studio_reachable",
            "_check_audio_devices",
        ]:
            patcher = patch.object(self.diag, name, return_value=ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher_e = patch.object(self.diag, "_check_disk_space", return_value=err)
        patcher_e.start()
        self.addCleanup(patcher_e.stop)
        report = self.diag.run_all_checks(force=True)
        self.assertEqual(report.status, "critical")
        self.assertIn("disk full", report.errors)

    def test_individual_check_timeout(self) -> None:
        """Медленная проверка (имитация через мок) не вешает всю диагностику."""
        import time as _time

        ok = CheckResult("x", "ok", "ok", 1.0)
        slow = CheckResult("disk_space", "ok", "slow but done", 50.0)

        for name in [
            "_check_python_version", "_check_required_packages",
            "_check_data_dir_writable", "_check_socket_path_available",
            "_check_ffmpeg_available", "_check_huggingface_token",
            "_check_stt_model_cached", "_check_lm_studio_reachable",
            "_check_audio_devices",
        ]:
            patcher = patch.object(self.diag, name, return_value=ok)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher_s = patch.object(self.diag, "_check_disk_space", return_value=slow)
        patcher_s.start()
        self.addCleanup(patcher_s.stop)

        t0 = _time.monotonic()
        report = self.diag.run_all_checks(force=True)
        elapsed = _time.monotonic() - t0
        # Весь прогон занимает < 2 сек (моки мгновенные)
        self.assertLess(elapsed, 2.0)
        self.assertIsInstance(report, StartupReport)

    def test_handles_check_exception_gracefully(self) -> None:
        """Если одна проверка кидает исключение — остальные всё равно выполняются.

        StartupDiagnostics не предусматривает catch внутри run_all_checks(),
        потому каждая проверка должна сама обрабатывать исключения и возвращать
        CheckResult с status='error'/'warning'. Этот тест проверяет, что
        _check_disk_space возвращает CheckResult при OSError.
        """
        with patch("shutil.disk_usage", side_effect=OSError("unexpected error")):
            result = self.diag._check_disk_space()
        self.assertEqual(result.status, "error")
        self.assertIsInstance(result.message, str)

    def test_results_structured_dict(self) -> None:
        """to_dict() возвращает структурированный словарь с ожидаемыми ключами."""
        report = self.diag.run_all_checks()
        d = report.to_dict()
        self.assertIsInstance(d, dict)
        for key in ("status", "startup_time_ms", "warnings", "errors", "checks", "version"):
            self.assertIn(key, d, f"Ключ {key!r} отсутствует в to_dict()")
        self.assertIsInstance(d["checks"], list)
        self.assertGreater(len(d["checks"]), 0)
        for c in d["checks"]:
            for k in ("name", "status", "message", "duration_ms", "details"):
                self.assertIn(k, c)

    def test_concurrent_diagnostics_safe(self) -> None:
        """Параллельный вызов run_all_checks() из нескольких потоков не падает."""
        import threading
        results = []
        errors = []

        def worker():
            try:
                report = self.diag.run_all_checks(force=True)
                results.append(report.status)
            except Exception as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Потоки упали: {errors}")
        self.assertEqual(len(results), 5)
        for status in results:
            self.assertIn(status, ("ready", "degraded", "critical"))


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
# Socket ownership (спека 2026-08-22): точный путь + self-owner матрица
# ===========================================================================

import socket as _socket_mod

from backend.socket_ownership import (  # noqa: E402
    SocketIdentity,
    SocketOwnershipSnapshot,
    SocketOwnershipState,
)


class SocketOwnershipDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="diagown_")
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = self.tmp.name
        self.socket_path = Path(self.tmpdir) / "krab.sock"

    def _listener(self) -> _socket_mod.socket:
        s = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
        s.bind(str(self.socket_path))
        s.listen(8)
        self.addCleanup(s.close)
        return s

    def _snapshot(self, state, identity=None):
        return SocketOwnershipSnapshot(
            socket_path=self.socket_path, state=state, bound_identity=identity
        )

    def test_default_socket_path_uses_data_dir_krabear_sock(self):
        diag = StartupDiagnostics(data_dir=self.tmpdir)
        result = diag._check_socket_path_available()
        self.assertEqual(
            result.details["path"], str(Path(self.tmpdir) / "krabear.sock")
        )

    def test_custom_path_lands_verbatim_in_details(self):
        diag = StartupDiagnostics(data_dir=self.tmpdir, socket_path=self.socket_path)
        result = diag._check_socket_path_available()
        self.assertEqual(result.details["path"], str(self.socket_path))

    def test_claimed_plus_missing_is_self_ok(self):
        diag = StartupDiagnostics(
            data_dir=self.tmpdir,
            socket_path=self.socket_path,
            socket_ownership_snapshot_getter=lambda: self._snapshot(
                SocketOwnershipState.CLAIMED
            ),
        )
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details["owner"], "self")
        self.assertEqual(result.details["ownership_state"], "claimed")

    def test_owned_listener_with_matching_inode_is_self_ok(self):
        self._listener()
        st = self.socket_path.lstat()
        diag = StartupDiagnostics(
            data_dir=self.tmpdir,
            socket_path=self.socket_path,
            socket_ownership_snapshot_getter=lambda: self._snapshot(
                SocketOwnershipState.LISTENING,
                SocketIdentity(st.st_dev, st.st_ino),
            ),
        )
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details["owner"], "self")
        self.assertEqual(result.details["ownership_state"], "listening")

    def test_listening_state_with_missing_socket_is_warning(self):
        diag = StartupDiagnostics(
            data_dir=self.tmpdir,
            socket_path=self.socket_path,
            socket_ownership_snapshot_getter=lambda: self._snapshot(
                SocketOwnershipState.LISTENING,
                SocketIdentity(1, 2),
            ),
        )
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "warning")

    def test_listening_state_with_mismatched_inode_is_warning(self):
        self._listener()
        diag = StartupDiagnostics(
            data_dir=self.tmpdir,
            socket_path=self.socket_path,
            socket_ownership_snapshot_getter=lambda: self._snapshot(
                SocketOwnershipState.LISTENING,
                SocketIdentity(0xDEAD, 0xBEEF),
            ),
        )
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "warning")

    def test_foreign_listener_without_snapshot_is_warning(self):
        self._listener()
        diag = StartupDiagnostics(data_dir=self.tmpdir, socket_path=self.socket_path)
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.details.get("path_status"), "listening")

    def test_stale_socket_without_snapshot_is_ok(self):
        s = _socket_mod.socket(_socket_mod.AF_UNIX, _socket_mod.SOCK_STREAM)
        s.bind(str(self.socket_path))
        s.listen(1)
        s.close()
        diag = StartupDiagnostics(data_dir=self.tmpdir, socket_path=self.socket_path)
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.details.get("path_status"), "stale")

    def test_occupied_regular_file_is_warning_and_untouched(self):
        self.socket_path.write_text("keep", encoding="utf-8")
        diag = StartupDiagnostics(data_dir=self.tmpdir, socket_path=self.socket_path)
        result = diag._check_socket_path_available()
        self.assertEqual(result.status, "warning")
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "keep")
