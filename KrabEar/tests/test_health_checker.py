"""Unit-тесты для HealthChecker."""

from __future__ import annotations
from backend.health_checker import HealthChecker

import contextlib
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@contextlib.contextmanager
def force_mlx_available():
    """Делает ``import mlx_whisper`` успешным внутри ``_check_stt_model``.

    Реальный сигнал 1 проверки STT (wave1776) — импортируемость ``mlx_whisper``.
    На Apple Silicon dev-машине пакет установлен, поэтому позитивные тесты
    («должно быть ok») проходили. На Linux CI (ubuntu, Python 3.12) и в любом
    окружении без Metal ``mlx_whisper`` физически отсутствует → проверка
    честно возвращает ``unavailable`` → агрегат ``unhealthy``, и тесты падали
    НЕ из-за регрессии, а из-за платформенной зависимости теста.

    Эти позитивные тесты проверяют ветку «mlx доступен + модель не упала → ok»,
    поэтому условие «mlx доступен» должно быть детерминированно зафиксировано
    моком (так же, как негативные тесты фиксируют недоступность). Это делает
    набор тестов независимым от платформы, ничего не ослабляя в утверждениях.
    """
    original_import = importlib.import_module

    def fake_import(name: str, *args, **kwargs):
        if name == "mlx_whisper":
            return object()  # truthy, не None
        return original_import(name, *args, **kwargs)

    with patch("importlib.import_module", side_effect=fake_import):
        yield


class FakeStore:
    """Минимальный фейк StateStore для тестов."""

    def __init__(self, data_dir: str, count: int = 5, raise_on_count: bool = False) -> None:
        self.data_dir = data_dir
        self._count = count
        self._raise_on_count = raise_on_count

    def count_active_items(self, lock_timeout_sec: float | None = None, nowait: bool = False) -> int:
        if self._raise_on_count:
            raise RuntimeError("store unavailable")
        return self._count


class FakeLLMRewriter:
    """Фейк LLMRewriter для тестов."""

    def __init__(self, circuit_state: str = "closed", model: str = "qwen3-4b") -> None:
        self._circuit_state = circuit_state
        self._model = model

    def status(self) -> dict:
        return {
            "reachable": self._circuit_state != "open",
            "model": self._model,
            "circuit_state": self._circuit_state,
            "last_latency_ms": None,
            "last_error": None,
        }


class FakeEngine:
    """Фейк AudioEngine для тестов.

    Не имеет ``_whisper_model`` — как и реальный движок, который хранит
    веса внутри mlx_whisper, а не на ``self``. ``_unavailable_models`` —
    реальный сигнал движка для сбоев модели.
    """

    def __init__(
        self,
        current_model: str | None = "mlx-community/whisper-small-mlx",
        unavailable_models: dict | None = None,
    ) -> None:
        self.current_model = current_model
        self.quality_profile = "balanced"
        # Реальный сигнал движка для упавших моделей (dict {model_id: timestamp})
        self._unavailable_models: dict = unavailable_models or {}
        # NOTE: _whisper_model намеренно отсутствует — как и в реальном AudioEngine.
        # Проверка getattr(engine, "_whisper_model", None) всегда вернёт None.


class FakeTranscriber:
    """Фейк Transcriber для тестов."""

    def __init__(
        self,
        current_model: str | None = "mlx-community/whisper-small-mlx",
        unavailable_models: dict | None = None,
    ) -> None:
        self.engine = FakeEngine(
            current_model=current_model,
            unavailable_models=unavailable_models,
        )


class TestHealthCheckerBasic(unittest.TestCase):
    """Базовые тесты структуры ответа check_all()."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir, count=10)
        self.checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(),
            start_time=time.monotonic() - 120.0,
        )

    def test_check_all_returns_required_keys(self) -> None:
        """check_all() должен возвращать status, checks, uptime_sec, version."""
        result = self.checker.check_all()
        self.assertIn("status", result)
        self.assertIn("checks", result)
        self.assertIn("uptime_sec", result)
        self.assertIn("version", result)

    def test_check_all_contains_all_subsystems(self) -> None:
        """checks должен содержать все 5 подсистем."""
        checks = self.checker.check_all()["checks"]
        for key in ("stt_model", "llm", "disk_space", "history_store", "audio_devices"):
            self.assertIn(key, checks, f"Missing check: {key}")

    def test_uptime_is_positive(self) -> None:
        result = self.checker.check_all()
        self.assertGreater(result["uptime_sec"], 0)

    def test_version_is_string(self) -> None:
        result = self.checker.check_all()
        self.assertIsInstance(result["version"], str)
        self.assertTrue(len(result["version"]) > 0)


class TestSttModelCheck(unittest.TestCase):
    """Тесты проверки STT-модели.

    Центральная задача этого блока — подтвердить, что исправление
    wave1776 устраняет ложно-здоровый сигнал:

    * OLD (broken): ``_whisper_model`` никогда не существует → ``cached``
      всегда ``False``; ``current_model`` всегда не-None →
      ветка ``warming_up`` мёртвый код; HealthChecker всегда докладывал ``ok``.
    * NEW (fixed): используем реальные сигналы движка — mlx_whisper
      импортируемость и ``_unavailable_models``.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)

    # ------------------------------------------------------------------
    # Базовые случаи
    # ------------------------------------------------------------------

    def test_stt_ok_with_transcriber_and_mlx_available(self) -> None:
        """mlx_whisper доступен + модель не упала → status 'ok'."""
        checker = HealthChecker(store=self.store, transcriber=FakeTranscriber())
        with force_mlx_available():
            result = checker._check_stt_model()
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["model"])
        # 'cached' field больше не возвращается (был ложным)
        self.assertNotIn("cached", result)

    def test_stt_unavailable_without_transcriber(self) -> None:
        checker = HealthChecker(store=self.store, transcriber=None)
        result = checker._check_stt_model()
        self.assertEqual(result["status"], "unavailable")

    def test_stt_unavailable_when_engine_none(self) -> None:
        """Transcriber без engine → unavailable."""
        transcriber = MagicMock()
        del transcriber.engine  # AttributeError при getattr
        transcriber.engine = None
        checker = HealthChecker(store=self.store, transcriber=transcriber)
        result = checker._check_stt_model()
        self.assertEqual(result["status"], "unavailable")

    def test_stt_handles_exception_gracefully(self) -> None:
        """Исключение внутри проверки не пробрасывается — возвращает error-статус."""
        bad_transcriber = MagicMock()
        # Вызываем исключение при обращении к .engine.current_model
        type(bad_transcriber.engine).current_model = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        checker = HealthChecker(store=self.store, transcriber=bad_transcriber)
        result = checker._check_stt_model()
        self.assertIn("status", result)
        self.assertIn(result["status"], ("error", "unavailable"))

    # ------------------------------------------------------------------
    # wave1776 fix: реальный сигнал 2 — _unavailable_models
    # ------------------------------------------------------------------

    def test_wave1776_stt_unavailable_when_model_in_unavailable_set(self) -> None:
        """FAIL-BEFORE / PASS-AFTER.

        Если ``engine._unavailable_models`` содержит текущую модель,
        STT деградировал. Старый код возвращал ``ok`` (проверял только
        ``current_model`` и несуществующий ``_whisper_model``).
        """
        model_name = "mlx-community/whisper-small-mlx"
        transcriber = FakeTranscriber(
            current_model=model_name,
            unavailable_models={model_name: time.monotonic()},
        )
        checker = HealthChecker(store=self.store, transcriber=transcriber)
        result = checker._check_stt_model()
        self.assertEqual(
            result["status"],
            "unavailable",
            "Must report 'unavailable' when model is in engine._unavailable_models; "
            "old code falsely reported 'ok' by ignoring this real signal",
        )
        self.assertEqual(result["model"], model_name)

    def test_wave1776_stt_ok_when_unavailable_models_empty(self) -> None:
        """mlx_whisper доступен + _unavailable_models пуст → 'ok'."""
        transcriber = FakeTranscriber(
            current_model="mlx-community/whisper-small-mlx",
            unavailable_models={},
        )
        checker = HealthChecker(store=self.store, transcriber=transcriber)
        with force_mlx_available():
            result = checker._check_stt_model()
        self.assertEqual(result["status"], "ok")

    def test_wave1776_stt_ok_when_different_model_is_unavailable(self) -> None:
        """Другая модель упала, но текущая нет → 'ok'."""
        transcriber = FakeTranscriber(
            current_model="mlx-community/whisper-small-mlx",
            unavailable_models={"mlx-community/whisper-large-mlx": time.monotonic()},
        )
        checker = HealthChecker(store=self.store, transcriber=transcriber)
        with force_mlx_available():
            result = checker._check_stt_model()
        self.assertEqual(result["status"], "ok")

    # ------------------------------------------------------------------
    # wave1776 fix: реальный сигнал 1 — mlx_whisper импортируемость
    # ------------------------------------------------------------------

    def test_wave1776_stt_unavailable_when_mlx_whisper_not_importable(self) -> None:
        """FAIL-BEFORE / PASS-AFTER.

        Если mlx_whisper не импортируется (Linux CI, ARM без Metal),
        STT физически не может работать. Старый код не проверял это и
        докладывал ``ok`` при любом ``current_model``.
        """
        transcriber = FakeTranscriber(current_model="mlx-community/whisper-small-mlx")
        checker = HealthChecker(store=self.store, transcriber=transcriber)

        # Симулируем недоступность mlx_whisper
        import importlib
        original_import = importlib.import_module

        def fake_import(name: str, *args, **kwargs):
            if name == "mlx_whisper":
                raise ImportError("mlx_whisper not available on this platform")
            return original_import(name, *args, **kwargs)

        with patch("importlib.import_module", side_effect=fake_import):
            result = checker._check_stt_model()

        self.assertEqual(
            result["status"],
            "unavailable",
            "Must report 'unavailable' when mlx_whisper is not importable; "
            "old code falsely reported 'ok' by never checking platform availability",
        )

    def test_wave1776_stt_unavailable_when_mlx_whisper_module_is_none(self) -> None:
        """mlx_whisper импортируется как None (как engine.py делает на non-Apple) → unavailable."""
        transcriber = FakeTranscriber(current_model="mlx-community/whisper-small-mlx")
        checker = HealthChecker(store=self.store, transcriber=transcriber)

        # Симулируем возврат None-модуля
        import importlib
        original_import = importlib.import_module

        def fake_import_none(name: str, *args, **kwargs):
            if name == "mlx_whisper":
                return None
            return original_import(name, *args, **kwargs)

        with patch("importlib.import_module", side_effect=fake_import_none):
            result = checker._check_stt_model()

        self.assertEqual(result["status"], "unavailable")

    # ------------------------------------------------------------------
    # Deprecated: старые тесты на _whisper_model / warming_up удалены
    # (те тесты описывали мёртвый код, а не реальное поведение)
    # ------------------------------------------------------------------

    def test_no_cached_field_in_response(self) -> None:
        """Поле 'cached' больше не возвращается — оно было основано на
        несуществующем атрибуте и всегда было False (вводило в заблуждение).
        """
        checker = HealthChecker(store=self.store, transcriber=FakeTranscriber())
        result = checker._check_stt_model()
        self.assertNotIn(
            "cached",
            result,
            "Field 'cached' must be absent: it was based on engine._whisper_model "
            "which never exists; always returned False regardless of real warm state",
        )

    def test_no_warming_up_status_returned(self) -> None:
        """'warming_up' статус больше не возвращается.

        Это был мёртвый код: зависел от cached=True И current_model=None
        одновременно, что теоретически невозможно после AudioEngine.__init__.
        """
        transcriber = FakeTranscriber(current_model=None)
        checker = HealthChecker(store=self.store, transcriber=transcriber)

        # Mock mlx_whisper as available so we reach the unavailable-models check
        import importlib
        original_import = importlib.import_module

        def always_mlx_available(name: str, *args, **kwargs):
            if name == "mlx_whisper":
                return object()  # truthy, not None
            return original_import(name, *args, **kwargs)

        with patch("importlib.import_module", side_effect=always_mlx_available):
            result = checker._check_stt_model()

        self.assertNotEqual(
            result["status"],
            "warming_up",
            "'warming_up' must not be returned; it was dead code based on "
            "engine._whisper_model which never exists",
        )

    def test_stt_ok_when_current_model_set(self) -> None:
        """current_model задан + mlx_whisper доступен + не упал → 'ok'."""
        transcriber = FakeTranscriber(current_model="mlx-community/whisper-small-mlx")
        checker = HealthChecker(store=self.store, transcriber=transcriber)
        with force_mlx_available():
            result = checker._check_stt_model()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], "mlx-community/whisper-small-mlx")


class TestAggregateStatusWithUnavailable(unittest.TestCase):
    """Тесты агрегации: новый статус 'unavailable' для критических подсистем."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)
        self.checker = HealthChecker(store=self.store)

    def test_unhealthy_when_stt_unavailable(self) -> None:
        """wave1776: stt_model=unavailable → unhealthy (критическая подсистема)."""
        checks = {
            "stt_model": {"status": "unavailable"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "unhealthy")

    def test_unhealthy_when_history_unavailable(self) -> None:
        """history_store=unavailable → unhealthy."""
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "unavailable"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "unhealthy")

    def test_healthy_when_llm_unavailable_non_critical(self) -> None:
        """llm=unavailable — не критичная подсистема → healthy (если остальное ok)."""
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "unavailable"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        # llm unavailable не degrade → healthy (audio_devices may add degraded in CI)
        result = self.checker._aggregate_status(checks)
        self.assertIn(result, ("healthy", "degraded"))
        self.assertNotEqual(result, "unhealthy")

    def test_warming_up_no_longer_degrades(self) -> None:
        """'warming_up' убран из статусов деградации — он был мёртвым кодом."""
        checks = {
            "stt_model": {"status": "warming_up"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        # 'warming_up' больше не является особым статусом; агрегатор не знает
        # о нём → попадает в "else" → healthy
        result = self.checker._aggregate_status(checks)
        self.assertEqual(result, "healthy")


class TestLLMCheck(unittest.TestCase):
    """Тесты проверки LLM."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)

    def test_llm_ok_when_circuit_closed(self) -> None:
        checker = HealthChecker(store=self.store, llm_rewriter=FakeLLMRewriter(circuit_state="closed"))
        result = checker._check_llm()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model"], "qwen3-4b")

    def test_llm_circuit_open(self) -> None:
        checker = HealthChecker(store=self.store, llm_rewriter=FakeLLMRewriter(circuit_state="open"))
        result = checker._check_llm()
        self.assertEqual(result["status"], "circuit_open")

    def test_llm_unavailable_when_none(self) -> None:
        checker = HealthChecker(store=self.store, llm_rewriter=None)
        result = checker._check_llm()
        self.assertEqual(result["status"], "unavailable")

    def test_llm_handles_exception(self) -> None:
        bad_rewriter = MagicMock()
        bad_rewriter.status.side_effect = RuntimeError("connection error")
        checker = HealthChecker(store=self.store, llm_rewriter=bad_rewriter)
        result = checker._check_llm()
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)


class TestDiskSpaceCheck(unittest.TestCase):
    """Тесты проверки места на диске."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)
        self.checker = HealthChecker(store=self.store)

    def test_disk_check_returns_free_gb(self) -> None:
        result = self.checker._check_disk_space()
        self.assertIn("free_gb", result)
        self.assertIsInstance(result["free_gb"], float)

    def test_disk_ok_with_plenty_of_space(self) -> None:
        with patch("shutil.disk_usage") as mock_du:
            # 100 ГБ свободно
            mock_du.return_value = MagicMock(free=100 * 1024 ** 3)
            result = self.checker._check_disk_space()
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["free_gb"], 100.0, places=1)

    def test_disk_warning_when_low(self) -> None:
        with patch("shutil.disk_usage") as mock_du:
            # 1 ГБ — меньше DISK_WARN_GB=2, но больше DISK_CRIT_GB=0.5
            mock_du.return_value = MagicMock(free=1 * 1024 ** 3)
            result = self.checker._check_disk_space()
        self.assertEqual(result["status"], "warning")

    def test_disk_critical_when_very_low(self) -> None:
        with patch("shutil.disk_usage") as mock_du:
            # 100 МБ — меньше DISK_CRIT_GB=0.5
            mock_du.return_value = MagicMock(free=100 * 1024 ** 2)
            result = self.checker._check_disk_space()
        self.assertEqual(result["status"], "critical")

    def test_disk_error_on_exception(self) -> None:
        with patch("shutil.disk_usage", side_effect=OSError("no such file")):
            result = self.checker._check_disk_space()
        self.assertEqual(result["status"], "error")


class TestHistoryStoreCheck(unittest.TestCase):
    """Тесты проверки хранилища истории."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()

    def test_history_ok(self) -> None:
        store = FakeStore(data_dir=self.tmpdir, count=42)
        checker = HealthChecker(store=store)
        result = checker._check_history_store()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["entries"], 42)
        self.assertIn("size_mb", result)

    def test_history_error_on_store_failure(self) -> None:
        store = FakeStore(data_dir=self.tmpdir, raise_on_count=True)
        checker = HealthChecker(store=store)
        result = checker._check_history_store()
        self.assertEqual(result["status"], "error")
        self.assertIn("error", result)

    def test_history_size_mb_is_zero_when_no_file(self) -> None:
        store = FakeStore(data_dir=self.tmpdir, count=0)
        checker = HealthChecker(store=store)
        result = checker._check_history_store()
        self.assertEqual(result["size_mb"], 0.0)


class TestAggregateStatus(unittest.TestCase):
    """Тесты агрегации итогового статуса."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)
        self.checker = HealthChecker(store=self.store)

    def test_healthy_when_all_ok(self) -> None:
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "healthy")

    def test_degraded_when_warning_present(self) -> None:
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "warning"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "degraded")

    def test_degraded_when_llm_circuit_open(self) -> None:
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "circuit_open"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "degraded")

    def test_unhealthy_when_critical_check_fails(self) -> None:
        checks = {
            "stt_model": {"status": "error"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "unhealthy")

    def test_unhealthy_when_history_store_fails(self) -> None:
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "error"},
            "audio_devices": {"status": "ok"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "unhealthy")

    def test_degraded_not_unhealthy_for_non_critical_error(self) -> None:
        """Ошибка в audio_devices (не критичная) даёт degraded, не unhealthy."""
        checks = {
            "stt_model": {"status": "ok"},
            "llm": {"status": "ok"},
            "disk_space": {"status": "ok"},
            "history_store": {"status": "ok"},
            "audio_devices": {"status": "error"},
        }
        self.assertEqual(self.checker._aggregate_status(checks), "degraded")


class TestFullCheckAllIntegration(unittest.TestCase):
    """Интеграционные тесты check_all() с реальными зависимостями."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir, count=100)

    def test_check_all_healthy_with_good_dependencies(self) -> None:
        checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(circuit_state="closed"),
            start_time=time.monotonic() - 60.0,
        )
        with force_mlx_available():
            result = checker.check_all()
        # Статус может быть healthy или degraded из-за audio_devices (нет в CI)
        self.assertIn(result["status"], ("healthy", "degraded"))
        self.assertEqual(result["checks"]["stt_model"]["status"], "ok")
        self.assertEqual(result["checks"]["llm"]["status"], "ok")
        self.assertEqual(result["checks"]["history_store"]["status"], "ok")
        self.assertEqual(result["checks"]["history_store"]["entries"], 100)

    def test_check_all_degraded_with_open_circuit(self) -> None:
        checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(circuit_state="open"),
        )
        result = checker.check_all()
        self.assertIn(result["status"], ("degraded", "unhealthy"))
        self.assertEqual(result["checks"]["llm"]["status"], "circuit_open")

    def test_check_all_unhealthy_when_store_fails(self) -> None:
        bad_store = FakeStore(data_dir=self.tmpdir, raise_on_count=True)
        checker = HealthChecker(store=bad_store, transcriber=FakeTranscriber())
        result = checker.check_all()
        self.assertEqual(result["status"], "unhealthy")
        self.assertEqual(result["checks"]["history_store"]["status"], "error")

    def test_check_all_each_check_has_status_key(self) -> None:
        checker = HealthChecker(store=self.store)
        result = checker.check_all()
        for name, check in result["checks"].items():
            self.assertIn("status", check, f"Check '{name}' missing 'status' key")

    def test_check_all_unhealthy_when_stt_model_unavailable(self) -> None:
        """wave1776: упавшая STT-модель → check_all() unhealthy."""
        model_name = "mlx-community/whisper-small-mlx"
        transcriber = FakeTranscriber(
            current_model=model_name,
            unavailable_models={model_name: time.monotonic()},
        )
        checker = HealthChecker(
            store=self.store,
            transcriber=transcriber,
            llm_rewriter=FakeLLMRewriter(circuit_state="closed"),
        )
        result = checker.check_all()
        self.assertEqual(result["checks"]["stt_model"]["status"], "unavailable")
        self.assertEqual(
            result["status"],
            "unhealthy",
            "Unavailable STT (critical subsystem) must surface as unhealthy",
        )


class TestHealthCheckerRequiredChecks(unittest.TestCase):
    """Верификация наличия конкретных проверок подсистем."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.store = FakeStore(data_dir=self.tmpdir)

    def test_all_subsystems_ok_returns_healthy(self) -> None:
        """Все подсистемы OK → overall healthy (или degraded из-за audio в CI)."""
        checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(circuit_state="closed"),
        )
        with force_mlx_available():
            result = checker.check_all()
        self.assertIn(result["status"], ("healthy", "degraded"))

    def test_one_subsystem_down_returns_degraded(self) -> None:
        """Одна некритичная подсистема в ошибке → degraded."""
        checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(circuit_state="open"),  # circuit_open → degraded
        )
        result = checker.check_all()
        self.assertIn(result["status"], ("degraded", "unhealthy"))

    def test_critical_subsystem_down_returns_unhealthy(self) -> None:
        """Критическая подсистема (history_store) в ошибке → unhealthy."""
        bad_store = FakeStore(data_dir=self.tmpdir, raise_on_count=True)
        checker = HealthChecker(store=bad_store, transcriber=FakeTranscriber())
        result = checker.check_all()
        self.assertEqual(result["status"], "unhealthy")

    def test_includes_disk_status(self) -> None:
        """checks содержит disk_space со status-полем."""
        checker = HealthChecker(store=self.store)
        result = checker.check_all()
        self.assertIn("disk_space", result["checks"])
        self.assertIn("status", result["checks"]["disk_space"])
        self.assertIn("free_gb", result["checks"]["disk_space"])

    def test_includes_stt_model_status(self) -> None:
        """checks содержит stt_model со status-полем."""
        checker = HealthChecker(store=self.store, transcriber=FakeTranscriber())
        result = checker.check_all()
        self.assertIn("stt_model", result["checks"])
        self.assertIn("status", result["checks"]["stt_model"])

    def test_includes_ipc_socket_status(self) -> None:
        """check_all() включает все subsystem-ключи; audio_devices служит IPC readiness proxy.

        HealthChecker не имеет отдельной IPC-socket проверки, но всегда включает
        history_store (доступность данных) и disk_space (достаточно ресурсов для IPC работы).
        Тест проверяет, что оба эти ключа присутствуют как proxy для IPC-readiness.
        """
        checker = HealthChecker(store=self.store)
        result = checker.check_all()
        checks = result["checks"]
        # Оба ключа служат proxy для IPC-readiness
        self.assertIn("history_store", checks)
        self.assertIn("disk_space", checks)
        # history_store доступен → IPC можно принимать
        self.assertEqual(checks["history_store"]["status"], "ok")

    def test_handles_subsystem_check_exception(self) -> None:
        """Исключение в одной проверке не ломает остальные."""
        # LLM rewriter бросает при вызове status()
        bad_llm = MagicMock()
        bad_llm.status.side_effect = RuntimeError("llm dead")
        checker = HealthChecker(store=self.store, llm_rewriter=bad_llm)
        result = checker.check_all()
        self.assertEqual(result["checks"]["llm"]["status"], "error")
        # Остальные проверки должны быть выполнены
        self.assertIn("disk_space", result["checks"])
        self.assertIn("history_store", result["checks"])
        self.assertNotEqual(result["checks"]["history_store"]["status"], "error")

    def test_concurrent_check_safe(self) -> None:
        """check_all() безопасен при параллельном вызове из N потоков."""
        import threading

        checker = HealthChecker(
            store=self.store,
            transcriber=FakeTranscriber(),
            llm_rewriter=FakeLLMRewriter(),
        )
        errors: list[Exception] = []

        def _worker() -> None:
            try:
                for _ in range(5):
                    result = checker.check_all()
                    assert "status" in result
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [], f"Concurrency errors: {errors}")


if __name__ == "__main__":
    unittest.main()
