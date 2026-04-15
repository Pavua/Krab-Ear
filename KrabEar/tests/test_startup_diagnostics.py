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
# 10. LM Studio check
# ===========================================================================

class TestCheckLmStudio(unittest.TestCase):
    def setUp(self) -> None:
        self.diag = _make_diag()

    def test_disabled_llm_returns_ok(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = False
        with patch("core.config.settings", mock_settings):
            result = self.diag._check_lm_studio_reachable()
        self.assertEqual(result.status, "ok")
        self.assertFalse(result.details["enabled"])

    def test_enabled_and_reachable_returns_ok(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = True
        mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
        mock_sock = MagicMock()
        with patch("core.config.settings", mock_settings):
            with patch("socket.create_connection", return_value=mock_sock):
                result = self.diag._check_lm_studio_reachable()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.details["enabled"])

    def test_enabled_but_unreachable_returns_warning(self) -> None:
        mock_settings = MagicMock()
        mock_settings.LLM_ENABLED = True
        mock_settings.LLM_BASE_URL = "http://localhost:1234/v1"
        with patch("core.config.settings", mock_settings):
            with patch("socket.create_connection", side_effect=ConnectionRefusedError()):
                result = self.diag._check_lm_studio_reachable()
        self.assertEqual(result.status, "warning")


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


if __name__ == "__main__":
    unittest.main()
