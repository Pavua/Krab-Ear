"""test_apply_recommended_setup_probes.py — реальная проводка probe-функций
(_handle_apply_recommended_setup в service.py, Задача 1 Шаг 4) на HealthCheckService/
ModelDownloader. Проверяет graceful degradation: недоступный LM Studio / отсутствующий
HF-кэш → skip с понятной причиной, НЕ exception.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_apply_recommended_setup_probes.py -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _FakeStore:
    def __init__(self):
        self._settings: dict = {}

    def load_settings(self, lock_timeout_sec: float | None = None, nowait: bool = False):
        return dict(self._settings)

    def save_settings(self, s):
        self._settings = dict(s)
        return dict(s)


def _make_svc():
    from backend.settings_backup import SettingsBackup
    from backend.settings_service import SettingsService
    tmp = tempfile.mkdtemp()
    return SettingsService(store=_FakeStore(), backup=SettingsBackup(backup_dir=Path(tmp) / "b"))


class ProbeLlmHttpUnreachableGracefulSkipTestCase(unittest.TestCase):
    """HealthCheckService.handle_probe_llm_http без rewriter -> {"reachable": False} ->
    llm_rewrite_enabled/action_items_auto_extract должны быть skipped, не exception."""

    def test_probe_returns_reachable_false_without_rewriter(self):
        from backend.health_check_service import HealthCheckService
        svc = HealthCheckService.__new__(HealthCheckService)
        svc._llm_rewriter = None
        result = svc.handle_probe_llm_http({})
        self.assertFalse(result["reachable"])
        self.assertEqual(result["latency_ms"], 0)
        self.assertIsNone(result["model"])

    def test_apply_recommended_setup_skips_llm_keys_when_probe_unreachable(self):
        svc = _make_svc()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True},
            probe_llm_fn=lambda: {"reachable": False, "latency_ms": 0, "model": None},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertIn("llm_rewrite_enabled", skipped_keys)
        self.assertIn("action_items_auto_extract", skipped_keys)

    def test_probe_fn_raising_exception_is_caught_not_propagated(self):
        svc = _make_svc()

        def _broken_probe():
            raise ConnectionError("LM Studio недоступен")

        # Не должно бросать исключение наружу — должно свестись к skip.
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=_broken_probe, sensevoice_cached_fn=lambda: False,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertNotIn("llm_rewrite_enabled", applied_keys)


class SenseVoiceCacheProbeTestCase(unittest.TestCase):
    """ModelDownloader.get_status(...)["cached"] управляет stt_sensevoice_enabled."""

    def test_sensevoice_not_cached_skipped_with_reason(self):
        svc = _make_svc()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: False,
        )
        skipped_keys = {s["key"]: s["reason"] for s in result["skipped"]}
        self.assertIn("stt_sensevoice_enabled", skipped_keys)
        self.assertIn("SenseVoice", skipped_keys["stt_sensevoice_enabled"])

    def test_sensevoice_cached_applied(self):
        svc = _make_svc()
        result = svc.handle_apply_recommended_setup(
            {"dry_run": True}, probe_llm_fn=lambda: {"reachable": False},
            sensevoice_cached_fn=lambda: True,
        )
        applied_keys = {a["key"] for a in result["applied"]}
        self.assertIn("stt_sensevoice_enabled", applied_keys)


class ServiceWiringUsesModelDownloaderGetStatusTestCase(unittest.TestCase):
    """_handle_apply_recommended_setup (service.py) вызывает ModelDownloader.get_status(...),
    НЕ приватный _is_cached() напрямую — проверка через mock на инстансе BackendService."""

    def test_service_wrapper_calls_get_status_with_sensevoice_model_id(self):
        # Патчим метод на классе ModelDownloader перед конструированием BackendService,
        # чтобы не тянуть тяжёлые зависимости реального __init__.
        with patch("backend.model_downloader.ModelDownloader.get_status") as mock_get_status:
            mock_get_status.return_value = {"cached": True}
            # Минимальный stub объекта с нужным методом — воспроизводит форму
            # self._model_downloader.get_status(...) без полного BackendService.__init__.
            stub = MagicMock()
            stub._model_downloader.get_status.return_value = {"cached": True}
            stub._health_check_svc.handle_probe_llm_http.return_value = {"reachable": False}
            stub._settings_svc.handle_apply_recommended_setup = MagicMock(return_value={"ok": True})

            from backend.service import BackendService
            BackendService._handle_apply_recommended_setup(stub, {"dry_run": True})

            stub._settings_svc.handle_apply_recommended_setup.assert_called_once()
            _, kwargs = stub._settings_svc.handle_apply_recommended_setup.call_args
            self.assertIn("probe_llm_fn", kwargs)
            self.assertIn("sensevoice_cached_fn", kwargs)
            # Вызываем sensevoice_cached_fn, чтобы убедиться что она реально бьёт в get_status
            kwargs["sensevoice_cached_fn"]()
            stub._model_downloader.get_status.assert_called_with("FunAudioLLM/SenseVoiceSmall")

    def test_service_wrapper_probe_llm_fn_calls_health_check_probe_with_empty_params(self):
        """probe_llm_fn должен быть 0-arg callable, вызывающий
        handle_probe_llm_http({}) — HealthCheckService.handle_probe_llm_http требует
        позиционный params, поэтому bound method НЕ может быть передана напрямую."""
        stub = MagicMock()
        stub._health_check_svc.handle_probe_llm_http.return_value = {"reachable": True}
        stub._settings_svc.handle_apply_recommended_setup = MagicMock(return_value={"ok": True})

        from backend.service import BackendService
        BackendService._handle_apply_recommended_setup(stub, {"dry_run": True})

        _, kwargs = stub._settings_svc.handle_apply_recommended_setup.call_args
        # 0-arg call must not raise TypeError (missing 'params').
        result = kwargs["probe_llm_fn"]()
        self.assertEqual(result, {"reachable": True})
        stub._health_check_svc.handle_probe_llm_http.assert_called_once_with({})


if __name__ == "__main__":
    unittest.main(verbosity=2)
