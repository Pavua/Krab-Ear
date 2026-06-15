"""Тесты для SherpaOnnxSTTAdapter.

Проверяет graceful degradation, lazy-load lock (double-checked locking)
и корректность структуры возвращаемого STTResult.
Пакет sherpa_onnx мокается, так как не всегда установлен в CI.
"""

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Path setup for tests
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_krabear = PROJECT_ROOT / "KrabEar"
if str(_krabear) not in sys.path:
    sys.path.insert(0, str(_krabear))

from core.pipeline.stt_adapter import STTResult
from core.pipeline.stt_sherpa import SherpaOnnxSTTAdapter


def _make_fake_sherpa_module(result_text="Привет sherpa"):
    """Создает мок модуля sherpa_onnx с OfflineRecognizer."""
    fake_module = types.ModuleType("sherpa_onnx")

    fake_result = MagicMock()
    fake_result.text = result_text

    fake_stream = MagicMock()
    fake_stream.result = fake_result

    fake_model = MagicMock()
    fake_model.create_stream.return_value = fake_stream

    # Мок метода from_paraformer
    fake_recognizer_class = MagicMock()
    fake_recognizer_class.from_paraformer.return_value = fake_model
    fake_module.OfflineRecognizer = fake_recognizer_class

    return fake_module, fake_recognizer_class, fake_model, fake_stream


class TestSherpaOnnxAvailability(unittest.TestCase):
    def test_is_available_when_installed(self):
        fake_module, _, _, _ = _make_fake_sherpa_module()
        with patch("core.pipeline.stt_sherpa._try_import_sherpa", return_value=fake_module):
            adapter = SherpaOnnxSTTAdapter()
            self.assertTrue(adapter.is_available())

    def test_is_available_when_not_installed(self):
        with patch("core.pipeline.stt_sherpa._try_import_sherpa", return_value=None):
            adapter = SherpaOnnxSTTAdapter()
            self.assertFalse(adapter.is_available())


class TestSherpaOnnxTranscribe(unittest.TestCase):
    def test_transcribe_raises_import_error_when_lib_missing(self):
        import numpy as np
        with patch("core.pipeline.stt_sherpa._try_import_sherpa", return_value=None):
            adapter = SherpaOnnxSTTAdapter()
            audio = np.zeros(16000, dtype=np.float32)
            with self.assertRaises(ImportError):
                adapter.transcribe(audio)

    def test_transcribe_returns_stt_result(self):
        import numpy as np
        fake_module, _, fake_model, fake_stream = _make_fake_sherpa_module("Привет от sherpa")

        with patch("core.pipeline.stt_sherpa._try_import_sherpa", return_value=fake_module):
            adapter = SherpaOnnxSTTAdapter()
            audio = np.zeros(16000, dtype=np.float32)
            result = adapter.transcribe(audio)

        self.assertIsInstance(result, STTResult)
        self.assertEqual(result.text, "Привет от sherpa")
        self.assertEqual(result.engine, "sherpa-onnx/sherpa_onnx_model")

        fake_model.create_stream.assert_called_once()
        fake_stream.accept_waveform.assert_called_once_with(16000, audio)
        fake_model.decode_stream.assert_called_once_with(fake_stream)


class TestSherpaOnnxLoadThreadSafe(unittest.TestCase):
    """Concurrent transcribe() must not double-load the model."""

    def test_concurrent_transcribe_loads_model_once(self):
        import numpy as np

        calls = []
        load_event = threading.Event()

        fake_module, fake_recognizer_class, fake_model, _ = _make_fake_sherpa_module("hi")

        def counting_from_paraformer(**kwargs):
            calls.append(kwargs)
            load_event.wait(timeout=2.0)  # Блокируем для имитации гонки потоков
            return fake_model

        fake_recognizer_class.from_paraformer = counting_from_paraformer
        adapter = SherpaOnnxSTTAdapter()
        errors = []

        def worker():
            try:
                audio = np.zeros(1600, dtype=np.float32)
                with patch("core.pipeline.stt_sherpa._try_import_sherpa", return_value=fake_module):
                    adapter.transcribe(audio)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()

        time.sleep(0.05)
        load_event.set()

        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(errors, [], f"Threads raised exceptions: {errors}")
        self.assertEqual(len(calls), 1, "Модель загрузилась более 1 раза (ошибка double-checked locking)")


if __name__ == "__main__":
    unittest.main()
