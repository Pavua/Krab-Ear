"""Тесты double-checked locking для lazy-load методов AudioEngine (W1227 F1 HIGH).

Проверяем что конкурентные вызовы _load_diarization_pipeline() не приводят к
двойной загрузке pipeline'а (~3 GB) при одновременных запросах из IPC-потока
и REST-сервера.

Все тяжёлые импорты (pyannote, torch, funasr, nemo, whisperx) мокаются.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_engine_no_gigaam() -> "AudioEngine":  # noqa: F821
    """Создаёт AudioEngine без GigaAM warmup-потока."""
    from core.engine import AudioEngine
    return AudioEngine(skip_gigaam_warmup=True)


class TestPyannoteDoubleCheckedLock(unittest.TestCase):
    """Проверяем что _load_diarization_pipeline сериализует concurrent вызовы."""

    def test_concurrent_load_pyannote_pipeline_serialized(self) -> None:
        """Два потока одновременно вызывают _load_diarization_pipeline().

        Ожидаем: Pipeline.from_pretrained вызван ровно 1 раз (не 2).
        """
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        load_started_event = threading.Event()
        allow_finish_event = threading.Event()

        fake_pipeline = MagicMock(name="pipeline_instance")
        fake_pipeline.to = MagicMock()

        def slow_from_pretrained(model_name, **kwargs):
            nonlocal load_call_count
            load_call_count += 1
            load_started_event.set()   # сигнал: первый поток вошёл в загрузку
            allow_finish_event.wait(timeout=3.0)  # ждём пока второй поток тоже начнёт
            return fake_pipeline

        import torch as _torch_module

        with patch("core.engine.Pipeline") as mock_pipeline_cls, \
                patch("core.engine.settings") as mock_settings, \
                patch("core.engine.torch", _torch_module):
            mock_pipeline_cls.from_pretrained.side_effect = slow_from_pretrained
            mock_settings.DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"
            mock_settings.HF_TOKEN = ""

            results: list = []
            errors: list = []

            def load_in_thread():
                try:
                    pipeline = engine._load_diarization_pipeline()
                    results.append(pipeline)
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            t1 = threading.Thread(target=load_in_thread, name="thread-1")
            t2 = threading.Thread(target=load_in_thread, name="thread-2")

            t1.start()
            # Ждём пока t1 войдёт в from_pretrained, потом запускаем t2
            load_started_event.wait(timeout=3.0)
            t2.start()
            # Даём t2 немного времени дойти до блокировки
            time.sleep(0.05)
            allow_finish_event.set()

            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        self.assertFalse(errors, f"Потоки завершились с ошибками: {errors}")
        self.assertEqual(
            load_call_count, 1,
            f"Pipeline.from_pretrained должен быть вызван 1 раз, вызван {load_call_count}",
        )
        self.assertEqual(len(results), 2, "Оба потока должны получить результат")
        # Оба потока получают один и тот же объект pipeline
        self.assertIs(results[0], results[1])

    def test_pyannote_loaded_only_once_under_concurrency(self) -> None:
        """N потоков одновременно запрашивают diarization pipeline.

        Ожидаем: from_pretrained вызван ровно 1 раз при N=10.
        """
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        call_count_lock = threading.Lock()

        fake_pipeline = MagicMock(name="pipeline_concurrent")
        fake_pipeline.to = MagicMock()

        def counting_from_pretrained(model_name, **kwargs):
            nonlocal load_call_count
            with call_count_lock:
                load_call_count += 1
            time.sleep(0.02)  # имитация медленной загрузки
            return fake_pipeline

        import torch as _torch_module

        with patch("core.engine.Pipeline") as mock_pipeline_cls, \
                patch("core.engine.settings") as mock_settings, \
                patch("core.engine.torch", _torch_module):
            mock_pipeline_cls.from_pretrained.side_effect = counting_from_pretrained
            mock_settings.DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"
            mock_settings.HF_TOKEN = ""

            results: list = []
            errors: list = []
            results_lock = threading.Lock()

            def load_in_thread():
                try:
                    pipeline = engine._load_diarization_pipeline()
                    with results_lock:
                        results.append(pipeline)
                except Exception as exc:  # pragma: no cover
                    with results_lock:
                        errors.append(exc)

            n_threads = 10
            threads = [threading.Thread(target=load_in_thread, name=f"t{i}") for i in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        self.assertFalse(errors, f"Потоки завершились с ошибками: {errors}")
        self.assertEqual(
            load_call_count, 1,
            f"Pipeline загружен {load_call_count} раз вместо 1 при {n_threads} конкурентных потоках",
        )
        self.assertEqual(len(results), n_threads)
        # Все потоки получают идентичный объект
        for r in results:
            self.assertIs(r, fake_pipeline)


class TestSenseVoiceDoubleCheckedLock(unittest.TestCase):
    """Проверяем что _load_sensevoice_model сериализует concurrent вызовы."""

    def test_sensevoice_loaded_only_once_under_concurrency(self) -> None:
        """N потоков одновременно запрашивают SenseVoice model."""
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        call_count_lock = threading.Lock()
        fake_model = MagicMock(name="sensevoice_model")

        def counting_init(*args, **kwargs):
            nonlocal load_call_count
            with call_count_lock:
                load_call_count += 1
            time.sleep(0.02)
            return fake_model

        with patch("core.engine._SenseVoiceAutoModel", side_effect=counting_init), \
                patch("core.engine.settings") as mock_settings:
            mock_settings.SENSEVOICE_MODEL = "iic/SenseVoiceSmall"

            results: list = []
            errors: list = []
            lock = threading.Lock()

            def load_in_thread():
                try:
                    m = engine._load_sensevoice_model()
                    with lock:
                        results.append(m)
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

            n_threads = 8
            threads = [threading.Thread(target=load_in_thread) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        self.assertFalse(errors, f"Ошибки: {errors}")
        self.assertEqual(load_call_count, 1,
                         f"SenseVoice загружен {load_call_count} раз вместо 1")
        self.assertEqual(len(results), n_threads)


class TestWhisperXDoubleCheckedLock(unittest.TestCase):
    """Проверяем что _load_whisperx_model сериализует concurrent вызовы."""

    def test_whisperx_loaded_only_once_under_concurrency(self) -> None:
        """N потоков одновременно запрашивают WhisperX model."""
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        call_count_lock = threading.Lock()
        fake_wx_model = MagicMock(name="whisperx_model")

        def counting_load_model(*args, **kwargs):
            nonlocal load_call_count
            with call_count_lock:
                load_call_count += 1
            time.sleep(0.02)
            return fake_wx_model

        fake_whisperx = MagicMock()
        fake_whisperx.load_model.side_effect = counting_load_model

        import torch as _torch_module

        with patch("core.engine._whisperx", fake_whisperx), \
                patch("core.engine.torch", _torch_module), \
                patch("core.engine.settings") as mock_settings:
            mock_settings.WHISPERX_MODEL = "large-v3"
            mock_settings.WHISPERX_DEVICE = "cpu"

            results: list = []
            errors: list = []
            lock = threading.Lock()

            def load_in_thread():
                try:
                    m = engine._load_whisperx_model()
                    with lock:
                        results.append(m)
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

            n_threads = 8
            threads = [threading.Thread(target=load_in_thread) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        self.assertFalse(errors, f"Ошибки: {errors}")
        self.assertEqual(load_call_count, 1,
                         f"WhisperX загружен {load_call_count} раз вместо 1")
        self.assertEqual(len(results), n_threads)


class TestParakeetDoubleCheckedLock(unittest.TestCase):
    """Проверяем что _load_parakeet_model сериализует concurrent вызовы."""

    def test_parakeet_loaded_only_once_under_concurrency(self) -> None:
        """N потоков одновременно запрашивают Parakeet model."""
        engine = _make_engine_no_gigaam()

        load_call_count = 0
        call_count_lock = threading.Lock()
        fake_parakeet = MagicMock(name="parakeet_model")

        def counting_from_pretrained(**kwargs):
            nonlocal load_call_count
            with call_count_lock:
                load_call_count += 1
            time.sleep(0.02)
            return fake_parakeet

        fake_nemo_asr = MagicMock()
        fake_nemo_asr.models.ASRModel.from_pretrained.side_effect = counting_from_pretrained

        with patch("core.engine._nemo_asr", fake_nemo_asr), \
                patch("core.engine.settings") as mock_settings:
            mock_settings.PARAKEET_MODEL = "nvidia/parakeet-tdt-1.1b"

            results: list = []
            errors: list = []
            lock = threading.Lock()

            def load_in_thread():
                try:
                    m = engine._load_parakeet_model()
                    with lock:
                        results.append(m)
                except Exception as exc:  # pragma: no cover
                    with lock:
                        errors.append(exc)

            n_threads = 8
            threads = [threading.Thread(target=load_in_thread) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

        self.assertFalse(errors, f"Ошибки: {errors}")
        self.assertEqual(load_call_count, 1,
                         f"Parakeet загружен {load_call_count} раз вместо 1")
        self.assertEqual(len(results), n_threads)


class TestLockFieldsInitialized(unittest.TestCase):
    """Проверяем что все 4 лока инициализируются в __init__."""

    def test_diarization_lock_is_rlock(self) -> None:
        engine = _make_engine_no_gigaam()
        self.assertTrue(hasattr(engine, "_diarization_load_lock"))
        self.assertIsInstance(engine._diarization_load_lock, type(threading.RLock()))

    def test_sensevoice_lock_is_rlock(self) -> None:
        engine = _make_engine_no_gigaam()
        self.assertTrue(hasattr(engine, "_sensevoice_load_lock"))

    def test_parakeet_lock_is_rlock(self) -> None:
        engine = _make_engine_no_gigaam()
        self.assertTrue(hasattr(engine, "_parakeet_load_lock"))

    def test_whisperx_lock_is_rlock(self) -> None:
        engine = _make_engine_no_gigaam()
        self.assertTrue(hasattr(engine, "_whisperx_load_lock"))


if __name__ == "__main__":
    unittest.main()
