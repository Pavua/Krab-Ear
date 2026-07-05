"""Unit tests for OpenWakeWordAdapter.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_openwakeword_adapter.py -v
"""

from __future__ import annotations

import sys
import threading
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

# Resolve project root so `backend.*` imports work standalone
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

BACKEND_DIR = _PROJECT_ROOT / "KrabEar"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.openwakeword_adapter import (  # noqa: E402
    OpenWakeWordAdapter,
    _BUILTIN_MODELS,
    _CUSTOM_MODELS_DIR,
)


class TestOpenWakeWordAdapterNoLib(unittest.TestCase):
    """Тесты stub-режима — openwakeword НЕ установлен."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _make_adapter(self) -> OpenWakeWordAdapter:
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        # Принудительно помечаем библиотеку как недоступную
        adapter._oww_available = False
        return adapter

    def test_instantiation_no_lib(self) -> None:
        """Адаптер создаётся без ошибок даже если openwakeword не установлен."""
        adapter = self._make_adapter()
        self.assertFalse(adapter.is_available())

    def test_is_running_false_initially(self) -> None:
        adapter = self._make_adapter()
        self.assertFalse(adapter.is_running())

    def test_active_model_none_initially(self) -> None:
        adapter = self._make_adapter()
        self.assertIsNone(adapter.active_model())

    def test_start_raises_runtime_error_without_lib(self) -> None:
        """start() бросает RuntimeError если openwakeword не установлен."""
        adapter = self._make_adapter()
        with self.assertRaises(RuntimeError) as ctx:
            adapter.start("alexa", lambda n, s: None)
        self.assertIn("openwakeword", str(ctx.exception).lower())

    def test_stop_noop_when_not_running(self) -> None:
        """stop() не падает если поток не запущен."""
        adapter = self._make_adapter()
        adapter.stop()  # должен пройти без исключений

    def test_list_models_builtins_always_present(self) -> None:
        """list_models() всегда возвращает built-in модели."""
        adapter = self._make_adapter()
        models = adapter.list_models()
        names = [m["name"] for m in models]
        for builtin in _BUILTIN_MODELS:
            self.assertIn(builtin, names)

    def test_list_models_source_builtin(self) -> None:
        adapter = self._make_adapter()
        for m in adapter.list_models():
            if m["name"] in _BUILTIN_MODELS:
                self.assertEqual(m["source"], "builtin")
                self.assertIsNone(m["path"])

    def test_handle_wake_word_list_models_no_lib(self) -> None:
        adapter = self._make_adapter()
        result = adapter.handle_wake_word_list_models({})
        self.assertTrue(result["ok"])
        self.assertFalse(result["engine_available"])
        self.assertIsInstance(result["models"], list)
        self.assertGreater(len(result["models"]), 0)

    def test_handle_wake_word_start_returns_error_no_lib(self) -> None:
        adapter = self._make_adapter()
        result = adapter.handle_wake_word_start({"model": "alexa"})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_handle_wake_word_stop_ok(self) -> None:
        adapter = self._make_adapter()
        result = adapter.handle_wake_word_stop({})
        self.assertTrue(result["ok"])

    def test_handle_wake_word_status_not_running(self) -> None:
        adapter = self._make_adapter()
        result = adapter.handle_wake_word_status({})
        self.assertTrue(result["ok"])
        self.assertFalse(result["running"])
        self.assertIsNone(result["active_model"])
        self.assertFalse(result["engine_available"])


class TestOpenWakeWordAdapterCustomModels(unittest.TestCase):
    """Тесты обнаружения пользовательских моделей."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _make_adapter(self) -> OpenWakeWordAdapter:
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = False
        return adapter

    def test_list_models_includes_custom_onnx(self) -> None:
        """Пользовательские .onnx файлы попадают в list_models()."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        (custom_dir / "krab.onnx").write_text("fake")

        adapter = self._make_adapter()
        models = adapter.list_models()
        custom_names = [m["name"] for m in models if m["source"] == "custom"]
        self.assertIn("krab", custom_names)

    def test_list_models_includes_custom_tflite(self) -> None:
        """Пользовательские .tflite файлы попадают в list_models()."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        (custom_dir / "mycustom.tflite").write_text("fake")

        adapter = self._make_adapter()
        models = adapter.list_models()
        custom_names = [m["name"] for m in models if m["source"] == "custom"]
        self.assertIn("mycustom", custom_names)

    def test_list_models_custom_has_path(self) -> None:
        """Пользовательские модели содержат поле path."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        model_file = custom_dir / "krab.onnx"
        model_file.write_text("fake")

        adapter = self._make_adapter()
        for m in adapter.list_models():
            if m["name"] == "krab":
                self.assertEqual(m["source"], "custom")
                self.assertEqual(m["path"], str(model_file))

    def test_list_models_ignores_non_model_files(self) -> None:
        """Файлы с другими расширениями игнорируются."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        (custom_dir / "readme.txt").write_text("ignore me")
        (custom_dir / "krab.json").write_text("{}")

        adapter = self._make_adapter()
        custom_names = [
            m["name"] for m in adapter.list_models() if m["source"] == "custom"
        ]
        self.assertNotIn("readme", custom_names)
        self.assertNotIn("krab", custom_names)

    def test_resolve_model_path_unknown_raises_value_error(self) -> None:
        """Неизвестное имя модели вызывает ValueError."""
        adapter = self._make_adapter()
        with self.assertRaises(ValueError) as ctx:
            adapter._resolve_model_path("nonexistent_model")
        self.assertIn("nonexistent_model", str(ctx.exception))

    def test_resolve_model_path_builtin_returns_none(self) -> None:
        """Встроенные модели возвращают None из _resolve_model_path."""
        adapter = self._make_adapter()
        for name in _BUILTIN_MODELS:
            self.assertIsNone(adapter._resolve_model_path(name))

    def test_resolve_model_path_custom_onnx(self) -> None:
        """_resolve_model_path находит .onnx в custom dir."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        model_file = custom_dir / "krab.onnx"
        model_file.write_text("fake")

        adapter = self._make_adapter()
        path = adapter._resolve_model_path("krab")
        self.assertEqual(path, str(model_file))

    def test_handle_wake_word_list_models_custom_dir_in_response(self) -> None:
        """IPC handler включает путь к custom_models_dir в ответ."""
        adapter = self._make_adapter()
        result = adapter.handle_wake_word_list_models({})
        self.assertEqual(
            result["custom_models_dir"],
            str(Path(self.tmp) / _CUSTOM_MODELS_DIR),
        )


class TestOpenWakeWordAdapterWithMockLib(unittest.TestCase):
    """Тесты с замоканным openwakeword — симуляция детекции."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _make_adapter_with_mock_oww(self) -> tuple[OpenWakeWordAdapter, MagicMock]:
        """Возвращает адаптер с oww_available=True и mock моделью."""
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        mock_oww = MagicMock()
        return adapter, mock_oww

    def test_start_sets_active_model(self) -> None:
        """После start() active_model() возвращает имя модели."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()

        # Патчим _load_model чтобы не трогать реальный openwakeword
        adapter._load_model = MagicMock(return_value=mock_oww)
        # _listen_loop не должен блокировать тест — заменяем на noop
        stop_called = threading.Event()

        def fake_listen_loop(**kwargs: object) -> None:
            stop_called.wait(timeout=0.1)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]

        callback_results: list[tuple[str, float]] = []
        adapter.start("alexa", lambda n, s: callback_results.append((n, s)))

        self.assertEqual(adapter.active_model(), "alexa")
        self.assertTrue(adapter.is_running())

        adapter.stop()

    def test_stop_clears_active_model(self) -> None:
        """После stop() active_model() возвращает None."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("hey_jarvis", lambda n, s: None)
        adapter.stop()

        self.assertIsNone(adapter.active_model())
        self.assertFalse(adapter.is_running())

    def test_callback_fired_on_detection(self) -> None:
        """Callback вызывается при обнаружении wake word."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        mock_oww.predict.return_value = {"alexa": 0.9}
        adapter._load_model = MagicMock(return_value=mock_oww)

        detected: list[tuple[str, float]] = []
        callback_lock = threading.Event()

        def cb(name: str, score: float) -> None:
            detected.append((name, score))
            callback_lock.set()

        # Переопределяем _listen_loop: один "чанк" и выход
        def fake_listen_loop(**kwargs: object) -> None:
            chunk = [0] * 1280
            oww = adapter._oww
            if oww is not None:
                preds = oww.predict(chunk)
                for model_name, score in preds.items():
                    if score >= 0.5 and adapter._on_detected is not None:
                        adapter._on_detected(model_name, float(score))

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("alexa", cb)
        # Ждём callback (он вызывается синхронно в fake_listen_loop до join)
        callback_lock.wait(timeout=1.0)
        adapter.stop()

        self.assertTrue(len(detected) > 0)
        name, score = detected[0]
        self.assertEqual(name, "alexa")
        self.assertAlmostEqual(score, 0.9, places=3)

    def test_start_already_running_no_double_start(self) -> None:
        """Повторный start() без stop() игнорируется."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("alexa", lambda n, s: None)
        thread1 = adapter._thread

        # Второй start() — должен быть проигнорирован
        adapter.start("hey_mycroft", lambda n, s: None)
        # Модель не должна была поменяться
        self.assertEqual(adapter.active_model(), "alexa")
        self.assertIs(adapter._thread, thread1)

        adapter.stop()

    def test_handle_wake_word_start_ok_with_mock(self) -> None:
        """IPC handle_wake_word_start возвращает ok=True с mock lib."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]

        result = adapter.handle_wake_word_start({"model": "alexa", "threshold": 0.7})
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "alexa")
        self.assertAlmostEqual(result["threshold"], 0.7, places=3)

        adapter.stop()

    def test_handle_wake_word_start_unknown_model_error(self) -> None:
        """IPC handle_wake_word_start возвращает ok=False для неизвестной модели."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        # Не патчим _load_model — _resolve_model_path вызовет ValueError первым

        result = adapter.handle_wake_word_start({"model": "unknown_xyz"})
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_handle_wake_word_status_running(self) -> None:
        """IPC handle_wake_word_status показывает running=True."""
        adapter, mock_oww = self._make_adapter_with_mock_oww()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("hey_mycroft", lambda n, s: None)

        result = adapter.handle_wake_word_status({})
        self.assertTrue(result["ok"])
        self.assertTrue(result["running"])
        self.assertEqual(result["active_model"], "hey_mycroft")

        adapter.stop()


class TestBuiltinModelsList(unittest.TestCase):
    """Проверка константы встроенных моделей."""

    def test_builtin_models_non_empty(self) -> None:
        self.assertGreater(len(_BUILTIN_MODELS), 0)

    def test_builtin_models_are_strings(self) -> None:
        for m in _BUILTIN_MODELS:
            self.assertIsInstance(m, str)
            self.assertTrue(m.strip())

    def test_expected_builtin_models_present(self) -> None:
        expected = {"alexa", "hey_mycroft", "hey_jarvis"}
        self.assertTrue(expected.issubset(set(_BUILTIN_MODELS)))


class TestOpenWakeWordAdapterWave178(unittest.TestCase):
    """Wave 178 — additional coverage tests."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    # ------------------------------------------------------------------
    # test_init_loads_default_model
    # ------------------------------------------------------------------

    def test_init_loads_default_model(self) -> None:
        """_load_model вызывает openwakeword.model.Model с именем модели."""
        mock_model_instance = MagicMock()
        mock_model_cls = MagicMock(return_value=mock_model_instance)

        import sys
        import types

        # Создаём фиктивный модуль openwakeword.model
        oww_pkg = types.ModuleType("openwakeword")
        oww_model_mod = types.ModuleType("openwakeword.model")
        oww_model_mod.Model = mock_model_cls
        oww_pkg.model = oww_model_mod

        sys.modules.setdefault("openwakeword", oww_pkg)
        sys.modules["openwakeword.model"] = oww_model_mod

        try:
            adapter = OpenWakeWordAdapter(data_dir=self.tmp)
            adapter._oww_available = True
            result = adapter._load_model("alexa", None)
        finally:
            del sys.modules["openwakeword.model"]
            sys.modules.pop("openwakeword", None)

        mock_model_cls.assert_called_once_with(wakeword_models=["alexa"])
        self.assertIs(result, mock_model_instance)

    # ------------------------------------------------------------------
    # test_process_chunk_returns_score
    # ------------------------------------------------------------------

    def test_process_chunk_returns_score(self) -> None:
        """predict() на mock модели возвращает словарь score."""
        mock_oww = MagicMock()
        mock_oww.predict.return_value = {"alexa": 0.75}

        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("alexa", lambda n, s: None)

        # Вызываем predict напрямую через mock oww
        chunk = [0] * 1280
        scores = adapter._oww.predict(chunk)
        self.assertIsInstance(scores, dict)
        self.assertIn("alexa", scores)
        self.assertAlmostEqual(scores["alexa"], 0.75, places=3)

        adapter.stop()

    # ------------------------------------------------------------------
    # test_score_below_threshold_no_detection
    # ------------------------------------------------------------------

    def test_score_below_threshold_no_detection(self) -> None:
        """Callback НЕ вызывается когда score ниже threshold."""
        mock_oww = MagicMock()
        mock_oww.predict.return_value = {"alexa": 0.3}  # below default 0.5

        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        adapter._load_model = MagicMock(return_value=mock_oww)

        detected: list[tuple[str, float]] = []

        def fake_listen_loop(**kwargs: object) -> None:
            threshold = kwargs.get("threshold", 0.5)
            chunk = [0] * 1280
            oww = adapter._oww
            if oww is not None:
                preds = oww.predict(chunk)
                for model_name, score in preds.items():
                    if score >= threshold and adapter._on_detected is not None:
                        adapter._on_detected(model_name, float(score))

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("alexa", lambda n, s: detected.append((n, s)), threshold=0.5)
        adapter.stop()

        self.assertEqual(len(detected), 0, "Не должно быть детекций при score=0.3")

    # ------------------------------------------------------------------
    # test_handles_short_audio_chunk
    # ------------------------------------------------------------------

    def test_handles_short_audio_chunk(self) -> None:
        """predict() вызывается даже с коротким аудио-чанком (< chunk_size)."""
        mock_oww = MagicMock()
        mock_oww.predict.return_value = {"alexa": 0.1}

        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        adapter._oww = mock_oww

        # Вызываем predict с коротким массивом напрямую
        short_chunk = [0] * 64
        result = adapter._oww.predict(short_chunk)
        mock_oww.predict.assert_called_once_with(short_chunk)
        self.assertIsInstance(result, dict)

    # ------------------------------------------------------------------
    # test_handles_silent_audio
    # ------------------------------------------------------------------

    def test_handles_silent_audio(self) -> None:
        """predict() не падает на тишине (все нули)."""
        mock_oww = MagicMock()
        mock_oww.predict.return_value = {"alexa": 0.0}

        silent_chunk = [0] * 1280
        result = mock_oww.predict(silent_chunk)
        self.assertEqual(result["alexa"], 0.0)
        mock_oww.predict.assert_called_once_with(silent_chunk)

    # ------------------------------------------------------------------
    # test_handles_unicode_model_path
    # ------------------------------------------------------------------

    def test_handles_unicode_model_path(self) -> None:
        """Пользовательская модель с Unicode-именем обнаруживается в list_models."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        # Имя файла с кириллицей
        unicode_model = custom_dir / "краб_голос.onnx"
        unicode_model.write_text("fake_onnx_data")

        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = False

        models = adapter.list_models()
        custom_names = [m["name"] for m in models if m["source"] == "custom"]
        self.assertIn("краб_голос", custom_names)

    # ------------------------------------------------------------------
    # test_concurrent_process_thread_safe
    # ------------------------------------------------------------------

    def test_concurrent_process_thread_safe(self) -> None:
        """Параллельные вызовы status/stop не вызывают race condition."""
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        mock_oww = MagicMock()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=2.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("alexa", lambda n, s: None)

        errors: list[Exception] = []

        def check_status() -> None:
            try:
                for _ in range(20):
                    _ = adapter.is_running()
                    _ = adapter.active_model()
                    _ = adapter.handle_wake_word_status({})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=check_status) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        adapter.stop()
        self.assertEqual(len(errors), 0, f"Race condition errors: {errors}")

    # ------------------------------------------------------------------
    # test_handles_openwakeword_unavailable (ImportError path in _load_model)
    # ------------------------------------------------------------------

    def test_handles_openwakeword_unavailable_load_model(self) -> None:
        """_load_model бросает RuntimeError если openwakeword не установлен."""
        import sys

        # Убираем openwakeword из sys.modules если есть, симулируем отсутствие
        saved = sys.modules.pop("openwakeword", None)
        saved_model = sys.modules.pop("openwakeword.model", None)

        # Запрещаем импорт через sys.modules
        import builtins
        original_import = builtins.__import__

        def mock_import(name: str, *args: object, **kwargs: object) -> object:
            if name in ("openwakeword", "openwakeword.model"):
                raise ImportError("openwakeword not installed")
            return original_import(name, *args, **kwargs)  # type: ignore[arg-type]

        builtins.__import__ = mock_import  # type: ignore[assignment]
        try:
            adapter = OpenWakeWordAdapter(data_dir=self.tmp)
            adapter._oww_available = True  # force past availability check
            with self.assertRaises(RuntimeError) as ctx:
                adapter._load_model("alexa", None)
            self.assertIn("openwakeword", str(ctx.exception))
        finally:
            builtins.__import__ = original_import  # type: ignore[assignment]
            if saved is not None:
                sys.modules["openwakeword"] = saved
            if saved_model is not None:
                sys.modules["openwakeword.model"] = saved_model

    # ------------------------------------------------------------------
    # test_reset_state
    # ------------------------------------------------------------------

    def test_reset_state(self) -> None:
        """После stop() внутреннее состояние сброшено: oww=None, active_model=None."""
        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        mock_oww = MagicMock()
        adapter._load_model = MagicMock(return_value=mock_oww)

        def fake_listen_loop(**kwargs: object) -> None:
            adapter._stop_event.wait(timeout=1.0)

        adapter._listen_loop = fake_listen_loop  # type: ignore[method-assign]
        adapter.start("hey_mycroft", lambda n, s: None)

        # Во время работы состояние установлено
        self.assertEqual(adapter.active_model(), "hey_mycroft")
        self.assertTrue(adapter.is_running())

        adapter.stop()

        # После stop() сброс состояния
        self.assertIsNone(adapter._oww)
        self.assertIsNone(adapter.active_model())
        self.assertFalse(adapter.is_running())

    # ------------------------------------------------------------------
    # test_custom_model_path_override
    # ------------------------------------------------------------------

    def test_custom_model_path_override(self) -> None:
        """_load_model вызывается с путём к .onnx файлу для пользовательской модели."""
        custom_dir = Path(self.tmp) / _CUSTOM_MODELS_DIR
        custom_dir.mkdir(parents=True)
        model_file = custom_dir / "краб.onnx"
        model_file.write_text("fake_onnx")

        import sys
        import types

        mock_model_instance = MagicMock()
        mock_model_cls = MagicMock(return_value=mock_model_instance)

        oww_pkg = types.ModuleType("openwakeword")
        oww_model_mod = types.ModuleType("openwakeword.model")
        oww_model_mod.Model = mock_model_cls
        oww_pkg.model = oww_model_mod

        sys.modules.setdefault("openwakeword", oww_pkg)
        sys.modules["openwakeword.model"] = oww_model_mod

        try:
            adapter = OpenWakeWordAdapter(data_dir=self.tmp)
            adapter._oww_available = True
            result = adapter._load_model("краб", str(model_file))
        finally:
            del sys.modules["openwakeword.model"]
            sys.modules.pop("openwakeword", None)

        # _load_model с model_path != None должен передать путь в OWWModel
        mock_model_cls.assert_called_once_with(wakeword_models=[str(model_file)])
        self.assertIs(result, mock_model_instance)


class TestOpenWakeWordAdapterListenLoopPredictType(unittest.TestCase):
    """Regression: Sentry KRAB-EAR-BACKEND-1C / 1D.

    ``openwakeword.Model.predict()`` requires a numpy array — passing a
    Python ``list`` raises ``ValueError`` on every audio chunk while wake
    word listening is active, so detection silently never fires (the
    exception is swallowed by the ``except Exception`` in ``_listen_loop``).
    Root cause was ``audio_chunk.flatten().tolist()`` converting the numpy
    array to a list right before calling ``oww.predict()``.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def _install_fake_sounddevice(self, pcm_chunk: np.ndarray) -> None:
        """Installs a fake `sounddevice` module exercising real _listen_loop."""

        class _FakeInputStream:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._read_count = 0

            def __enter__(self) -> "_FakeInputStream":
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def read(self, frames: int) -> tuple[np.ndarray, bool]:
                self._read_count += 1
                return pcm_chunk, False

        fake_sd = types.ModuleType("sounddevice")
        fake_sd.InputStream = _FakeInputStream  # type: ignore[attr-defined]
        sys.modules["sounddevice"] = fake_sd

    def tearDown(self) -> None:
        sys.modules.pop("sounddevice", None)

    def test_listen_loop_calls_predict_with_numpy_array_not_list(self) -> None:
        """_listen_loop must pass a numpy.ndarray to oww.predict(), not a list.

        FAILS before the fix (predict() called with a plain Python list —
        the exact shape openwakeword's real Model.predict() rejects with
        `ValueError: ... must by a Numpy array, instead received ... list`).
        PASSES after removing the stray `.tolist()` call.
        """
        # Real int16 PCM chunk shaped like sd.InputStream(channels=1) output.
        pcm_chunk = np.zeros((1280, 1), dtype="int16")
        self._install_fake_sounddevice(pcm_chunk)

        adapter = OpenWakeWordAdapter(data_dir=self.tmp)
        adapter._oww_available = True
        mock_oww = MagicMock()
        mock_oww.predict.return_value = {"alexa": 0.0}
        adapter._oww = mock_oww

        # Run exactly one iteration of the REAL _listen_loop body, then stop.
        def _stop_after_first_predict(*_a: object, **_k: object) -> dict:
            adapter._stop_event.set()
            return {"alexa": 0.0}

        mock_oww.predict.side_effect = _stop_after_first_predict

        adapter._listen_loop(threshold=0.5, chunk_size=1280, sample_rate=16000)

        mock_oww.predict.assert_called_once()
        called_arg = mock_oww.predict.call_args[0][0]
        self.assertIsInstance(
            called_arg,
            np.ndarray,
            f"oww.predict() called with {type(called_arg)!r} instead of "
            "numpy.ndarray — openwakeword.Model.predict() raises ValueError "
            "on a plain list (Sentry KRAB-EAR-BACKEND-1C/1D).",
        )
        self.assertNotIsInstance(called_arg, list)


if __name__ == "__main__":
    unittest.main()
