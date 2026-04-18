"""Тесты Parakeet-TDT-1.1B adapter в fallback chain AudioEngine (Phase 4.2).

Проверяет интеграцию без реальной загрузки модели (FakeEngine/mock паттерн).
Следует шаблону test_sensevoice_adapter.py.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import AudioEngine


class TestParakeetAdapterDisabled(unittest.TestCase):
    """Parakeet не участвует в chain когда PARAKEET_ENABLED=False."""

    @patch("core.engine.settings")
    def test_parakeet_skipped_when_disabled(self, mock_settings: Any) -> None:
        """_transcribe_with_fallback_impl не вставляет PARAKEET_MARKER если флаг выключен."""
        mock_settings.PARAKEET_ENABLED = False
        mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
        mock_settings.SENSEVOICE_ENABLED = False
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        engine._unavailable_models = set()
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_future = MagicMock()
                mock_future.result.return_value = {"text": "test", "segments": [], "language": "en"}
                mock_pool.submit.return_value = mock_future
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")

        # Parakeet маркер не должен попасть в unavailable (он не вставлялся)
        self.assertNotIn(engine._PARAKEET_MARKER, engine._unavailable_models)


class TestParakeetAdapterEnabled(unittest.TestCase):
    """Parakeet участвует в chain когда включён."""

    @patch("core.engine.settings")
    def test_parakeet_reached_when_balanced_unavailable(self, mock_settings: Any) -> None:
        """Когда balanced whisper помечен недоступным — Parakeet успешно транскрибирует."""
        mock_settings.PARAKEET_ENABLED = True
        mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
        mock_settings.SENSEVOICE_ENABLED = False
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.MODEL_MAX_CANDIDATES = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        # Помечаем balanced whisper как недоступный — Parakeet должен сработать
        engine._unavailable_models = {"mlx-community/whisper-large-v3-turbo"}
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        engine._transcribe_parakeet = lambda *a, **kw: {  # type: ignore[method-assign]
            "text": "hello world",
            "engine": "parakeet",
            "language": "en",
            "segments": [],
        }

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            result = engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")

        self.assertEqual(result["text"], "hello world")
        self.assertEqual(result["engine"], "parakeet")
        self.assertEqual(result["language"], "en")

    @patch("core.engine.settings")
    def test_parakeet_marker_inserted_before_sensevoice(self, mock_settings: Any) -> None:
        """PARAKEET_MARKER вставляется на позицию 1, SENSEVOICE_MARKER — на позицию 2."""
        mock_settings.PARAKEET_ENABLED = True
        mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
        mock_settings.SENSEVOICE_ENABLED = True
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        engine._unavailable_models = set()
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        visited_adapters: list[str] = []

        # Оба адаптера падают — записываем порядок вызовов
        def fake_transcribe_parakeet(*a: Any, **kw: Any) -> dict:
            visited_adapters.append("parakeet")
            raise RuntimeError("nemo not installed")

        def fake_transcribe_sensevoice(*a: Any, **kw: Any) -> dict:
            visited_adapters.append("sensevoice")
            raise RuntimeError("funasr not installed")

        engine._transcribe_parakeet = fake_transcribe_parakeet  # type: ignore[method-assign]
        engine._transcribe_sensevoice = fake_transcribe_sensevoice  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_future = MagicMock()
                mock_future.result.side_effect = RuntimeError("whisper unavailable")
                mock_pool.submit.return_value = mock_future
                with self.assertRaises(RuntimeError):
                    engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")

        # Parakeet должен быть вызван ДО SenseVoice
        self.assertIn("parakeet", visited_adapters)
        self.assertIn("sensevoice", visited_adapters)
        parakeet_idx = visited_adapters.index("parakeet")
        sensevoice_idx = visited_adapters.index("sensevoice")
        self.assertLess(parakeet_idx, sensevoice_idx, "Parakeet должен идти до SenseVoice в chain")

    @patch("core.engine.settings")
    def test_parakeet_marker_not_retried_after_failure(self, mock_settings: Any) -> None:
        """Если Parakeet однажды упал — он не вставляется в chain повторно."""
        mock_settings.PARAKEET_ENABLED = True
        mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"
        mock_settings.SENSEVOICE_ENABLED = False
        mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
        mock_settings.SENSEVOICE_EMOTION_TO_HISTORY = True
        mock_settings.MODEL_BALANCED = "mlx-community/whisper-large-v3-turbo"
        mock_settings.TRANSCRIBE_TIMEOUT_SEC = 30
        mock_settings.NETWORK_MODE = "offline_strict"
        mock_settings.model_max_list = ["mlx-community/whisper-large-v3-turbo"]

        engine = AudioEngine.__new__(AudioEngine)
        engine.quality_profile = "balanced"
        engine.current_model = "mlx-community/whisper-large-v3-turbo"
        # Маркер уже помечен недоступным после предыдущего сбоя
        engine._unavailable_models = {engine._PARAKEET_MARKER}
        engine._sensevoice_model = None
        engine._sensevoice_load_error = None
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        pk_call_count: list[bool] = []
        engine._transcribe_parakeet = lambda *a, **kw: pk_call_count.append(True) or {}  # type: ignore[method-assign]

        with patch("core.engine._profiler") as mock_profiler:
            mock_profiler.start_span.return_value.__enter__ = lambda s: s
            mock_profiler.start_span.return_value.__exit__ = MagicMock(return_value=False)
            with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool_cls:
                mock_pool = MagicMock()
                mock_pool_cls.return_value.__enter__ = lambda s: mock_pool
                mock_pool_cls.return_value.__exit__ = MagicMock(return_value=False)
                mock_future = MagicMock()
                mock_future.result.return_value = {"text": "whisper ok", "segments": [], "language": "en"}
                mock_pool.submit.return_value = mock_future
                engine._transcribe_with_fallback_impl(b"audio", "prompt", "en")

        self.assertEqual(len(pk_call_count), 0, "Parakeet не должен вызываться если маркер уже недоступен")


class TestParakeetLoadNoNemo(unittest.TestCase):
    """_load_parakeet_model корректно обрабатывает отсутствие nemo."""

    def test_load_raises_when_nemo_missing(self) -> None:
        """Без nemo _load_parakeet_model поднимает RuntimeError."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        with patch("core.engine._nemo_asr", None):
            with self.assertRaises(RuntimeError) as ctx:
                engine._load_parakeet_model()
        self.assertIn("nemo", str(ctx.exception).lower())
        # Ошибка кэшируется для последующих вызовов
        self.assertIsNotNone(engine._parakeet_load_error)

    def test_load_raises_from_cache_on_second_call(self) -> None:
        """После первого сбоя _load_parakeet_model сразу поднимает RuntimeError без попытки загрузки."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._parakeet_model = None
        engine._parakeet_load_error = "nemo не установлен"

        load_attempts: list[bool] = []

        with patch("core.engine._nemo_asr") as mock_nemo:
            mock_nemo.models.ASRModel.from_pretrained.side_effect = lambda **kw: load_attempts.append(True)
            with self.assertRaises(RuntimeError):
                engine._load_parakeet_model()

        self.assertEqual(len(load_attempts), 0, "Повторная загрузка не должна происходить")

    def test_load_returns_cached_model(self) -> None:
        """Второй вызов возвращает кэшированную модель без повторной загрузки."""
        engine = AudioEngine.__new__(AudioEngine)
        fake_model = MagicMock()
        engine._parakeet_model = fake_model
        engine._parakeet_load_error = None

        result = engine._load_parakeet_model()
        self.assertIs(result, fake_model)


class TestParakeetTranscribe(unittest.TestCase):
    """_transcribe_parakeet корректно оборачивает NeMo transcribe API."""

    def test_transcribe_with_path_input(self) -> None:
        """Паракит принимает путь к wav-файлу без создания temp-файла."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        fake_model = MagicMock()
        fake_model.transcribe.return_value = ["hello world from parakeet"]

        engine._load_parakeet_model = lambda: fake_model  # type: ignore[method-assign]

        result = engine._transcribe_parakeet("/tmp/test.wav", language="en")

        self.assertEqual(result["text"], "hello world from parakeet")
        self.assertEqual(result["engine"], "parakeet")
        self.assertEqual(result["language"], "en")
        self.assertEqual(result["segments"], [])
        # transcribe должен был вызван с путём к файлу
        fake_model.transcribe.assert_called_once_with(["/tmp/test.wav"])

    def test_transcribe_raises_on_empty_output(self) -> None:
        """Пустой вывод от NeMo вызывает RuntimeError."""
        engine = AudioEngine.__new__(AudioEngine)
        engine._parakeet_model = None
        engine._parakeet_load_error = None

        fake_model = MagicMock()
        fake_model.transcribe.return_value = []

        engine._load_parakeet_model = lambda: fake_model  # type: ignore[method-assign]

        with self.assertRaises(RuntimeError):
            engine._transcribe_parakeet("/tmp/test.wav")


class TestParakeetMarkerConstant(unittest.TestCase):
    """Проверка константы маркера и её уникальности."""

    def test_parakeet_marker_is_class_attribute(self) -> None:
        """_PARAKEET_MARKER доступен как атрибут класса."""
        self.assertEqual(AudioEngine._PARAKEET_MARKER, "parakeet:adapter")

    def test_markers_are_distinct(self) -> None:
        """Parakeet и SenseVoice маркеры уникальны (не путаются в unavailable set)."""
        self.assertNotEqual(AudioEngine._PARAKEET_MARKER, AudioEngine._SENSEVOICE_MARKER)


if __name__ == "__main__":
    unittest.main()
