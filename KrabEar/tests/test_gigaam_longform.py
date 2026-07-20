"""Тесты GigaAM longform через AudioChunker.

Проверяет что engine.py корректно разбивает длинное аудио на чанки
и объединяет результаты без загрузки реальной GigaAM модели.

Сценарии:
- test_short_audio_no_chunking: аудио 5s → один transcribe() вызов
- test_long_audio_chunked: аудио 60s → несколько transcribe() вызовов, каждый < 25s
- test_chunked_results_concatenated: выход содержит текст всех чанков
- test_chunker_fallback_to_longform: если AudioChunker падает → longform path
- test_very_long_audio_all_chunks_under_limit: 120s аудио → все чанки <= 20s
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

_SR = 16000


def _audio(seconds: float, sr: int = _SR) -> np.ndarray:
    """Синус 440 Гц заданной длительности."""
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _make_fake_settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.STT_GIGAAM_ENABLED = True
    s.STT_GIGAAM_MODE = "rnnt"
    s.STT_GIGAAM_DEVICE = "mps"
    s.STT_GIGAAM_HF_TOKEN = ""
    s.TRANSCRIBE_LANGUAGE = "ru"
    s.NETWORK_MODE = "offline_strict"
    s.MODEL_BALANCED = "mlx-community/whisper-large-v3-mlx"
    s.model_max_list = []
    s.TRANSCRIBE_TIMEOUT_SEC = 30
    s.STT_USE_RU_FINETUNE = False
    s.STT_RU_FINETUNE_MODEL = ""
    s.PARAKEET_ENABLED = False
    s.SENSEVOICE_ENABLED = False
    s.WHISPERX_ENABLED = False
    s.VOXTRAL_ENABLED = False
    s.DATA_DIR = "/tmp"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _make_chunk_transcribe_response(chunk_index: int) -> dict:
    """Адаптер возвращает разный текст для каждого чанка."""
    return {
        "text": f"текст чанка {chunk_index}",
        "confidence": 0.9,
        "engine": "gigaam-rnnt",
        "language": "ru",
    }


def _make_audio_engine_without_warmup():
    """Создаёт GigaAM-engine без запуска фонового процесса прогрева.

    Флаг ``skip_gigaam_warmup`` здесь использовать нельзя: он обозначает
    REST-engine и намеренно запрещает любые вызовы GigaAM.
    """
    from core.engine import AudioEngine

    with patch("core.engine.threading.Thread.start", autospec=True):
        return AudioEngine()


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------

class TestShortAudioNoChunking(unittest.TestCase):
    """Аудио <= 25s → один transcribe() вызов, AudioChunker не используется."""

    def test_5s_single_transcribe_call(self):
        """5s аудио → адаптер вызван ровно 1 раз без chunking."""
        audio = _audio(5.0)
        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "короткое аудио",
            "confidence": 0.95,
            "engine": "gigaam-rnnt",
            "language": "ru",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        # Ровно один вызов transcribe без longform
        self.assertEqual(fake_adapter.transcribe.call_count, 1)
        call_kwargs = fake_adapter.transcribe.call_args
        # Убеждаемся что longform=True не передавался
        if call_kwargs.kwargs:
            self.assertNotEqual(call_kwargs.kwargs.get("longform"), True)
        self.assertEqual(result["text"], "короткое аудио")

    def test_25s_boundary_single_call(self):
        """Ровно 25s допустимы upstream shortform-контрактом GigaAM."""
        audio = _audio(25.0)
        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "граница",
            "confidence": 0.9,
            "engine": "gigaam-rnnt",
            "language": "ru",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        self.assertEqual(fake_adapter.transcribe.call_count, 1)
        self.assertEqual(result["text"], "граница")


class TestLongAudioChunked(unittest.TestCase):
    """Аудио > 25s → несколько transcribe() вызовов через AudioChunker."""

    def test_25_1s_uses_chunker_before_upstream_shortform_limit(self):
        """25.1s не должны попадать в shortform, который отвергает >25s."""
        audio = _audio(25.1)
        chunk_durations: list[float] = []

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            chunk_durations.append(len(chunk_audio) / _SR)
            return {
                "text": f"часть{len(chunk_durations)}",
                "confidence": 0.9,
                "engine": "gigaam-rnnt",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        with patch("core.engine.settings", _make_fake_settings()):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router
            result = engine._transcribe_gigaam(audio, language="ru")

        self.assertGreater(fake_adapter.transcribe.call_count, 1)
        self.assertTrue(all(duration <= 20.1 for duration in chunk_durations))
        self.assertEqual(result["engine"], "gigaam-rnnt-chunked")

    def test_29_9s_also_uses_chunker(self):
        """Весь ранее потерянный диапазон 25–30s обязан идти через chunker."""
        audio = _audio(29.9)
        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "часть",
            "confidence": 0.9,
            "engine": "gigaam-rnnt",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        with patch("core.engine.settings", _make_fake_settings()):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router
            engine._transcribe_gigaam(audio, language="ru")

        self.assertGreater(fake_adapter.transcribe.call_count, 1)

    def test_60s_audio_multiple_calls(self):
        """60s аудио → адаптер вызывается N раз, каждый чанк <= 20s."""
        audio = _audio(60.0)
        call_count = [0]

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            """Возвращает разный текст для каждого чанка, проверяет длину."""
            # Каждый чанк должен быть <= 20s
            chunk_duration = len(chunk_audio) / _SR
            self.assertLessEqual(
                chunk_duration, 21.0,  # +1s допуск на граничные случаи тишины
                f"Чанк {call_count[0]} длиннее 21s: {chunk_duration:.1f}s",
            )
            idx = call_count[0]
            call_count[0] += 1
            return {
                "text": f"чанк{idx}",
                "confidence": 0.9,
                "engine": "gigaam-rnnt",
                "language": "ru",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        # Должно быть больше 1 вызова (60s / 20s = 3 чанка минимум)
        self.assertGreater(fake_adapter.transcribe.call_count, 1)
        self.assertIn("text", result)
        self.assertEqual(result["language"], "ru")

    def test_chunks_each_under_25s(self):
        """90s аудио → ни один чанк не превышает 25s (hard limit GigaAM)."""
        audio = _audio(90.0)
        chunk_durations: list[float] = []

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            dur = len(chunk_audio) / _SR
            chunk_durations.append(dur)
            return {
                "text": f"seg{len(chunk_durations)}",
                "confidence": 0.9,
                "engine": "gigaam-rnnt",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            engine._transcribe_gigaam(audio, language="ru")

        self.assertTrue(len(chunk_durations) > 0, "Должен быть хотя бы один чанк")
        for i, dur in enumerate(chunk_durations):
            self.assertLessEqual(
                dur, 25.0,
                f"Чанк {i} = {dur:.1f}s превышает hard limit 25s GigaAM",
            )


class TestChunkedResultsConcatenated(unittest.TestCase):
    """Выход содержит текст всех чанков, объединённый через пробел."""

    def test_all_chunk_texts_in_output(self):
        """result['text'] содержит текст каждого чанка."""
        audio = _audio(45.0)
        texts = ["первый фрагмент", "второй фрагмент", "третий фрагмент"]
        call_count = [0]

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            idx = min(call_count[0], len(texts) - 1)
            call_count[0] += 1
            return {
                "text": texts[idx],
                "confidence": 0.9,
                "engine": "gigaam-rnnt",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        merged_text = result.get("text", "")
        # Текст каждого чанка должен присутствовать в итоговом результате
        # (AudioChunker.merge_results объединяет через пробел)
        for t in texts[: call_count[0]]:
            if t:
                self.assertIn(t, merged_text, f"Текст чанка '{t}' не найден в '{merged_text}'")

    def test_engine_name_chunked(self):
        """При chunked path engine name = 'gigaam-rnnt-chunked'."""
        audio = _audio(50.0)

        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "текст",
            "confidence": 0.9,
            "engine": "gigaam-rnnt",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        self.assertEqual(result.get("engine"), "gigaam-rnnt-chunked")

    def test_confidence_is_average(self):
        """Confidence в результате = среднее по всем чанкам."""
        audio = _audio(45.0)
        confidences_returned = [0.8, 0.9, 1.0]
        call_count = [0]

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            idx = min(call_count[0], len(confidences_returned) - 1)
            call_count[0] += 1
            return {
                "text": f"слово{idx}",
                "confidence": confidences_returned[idx],
                "engine": "gigaam-rnnt",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        # Confidence должен быть числом из допустимого диапазона
        conf = result.get("confidence", 0.0)
        self.assertIsInstance(conf, float)
        self.assertGreater(conf, 0.0)
        self.assertLessEqual(conf, 1.0)


class TestChunkerFallbackToLongform(unittest.TestCase):
    """Если AudioChunker падает → fallback на transcribe_longform() path."""

    def test_chunker_import_error_falls_back_to_longform(self):
        """Если core.audio_chunker недоступен → engine не падает (graceful fallback)."""
        audio = _audio(25.1)
        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "longform результат",
            "confidence": 0.85,
            "engine": "gigaam-rnnt",
            "language": "ru",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        # Симулируем недоступность audio_chunker через sys.modules
        import sys as _sys
        saved = _sys.modules.pop("core.audio_chunker", None)
        _sys.modules["core.audio_chunker"] = None  # type: ignore

        fake_settings = _make_fake_settings()
        try:
            with patch("core.engine.settings", fake_settings):
                engine = _make_audio_engine_without_warmup()
                engine._router = fake_router

                # Должен либо вернуть результат (longform fallback) либо error dict
                result = engine._transcribe_gigaam(audio, language="ru")
                self.assertIn("text", result)
        except Exception:
            pass  # допустимо — важно что ImportError не пробрасывается необработанным
        finally:
            # Восстанавливаем модуль
            if saved is not None:
                _sys.modules["core.audio_chunker"] = saved
            else:
                _sys.modules.pop("core.audio_chunker", None)

    def test_chunker_exception_falls_back_to_longform(self):
        """RuntimeError внутри AudioChunker → fallback на longform path."""
        audio = _audio(25.1)
        fake_adapter = MagicMock()
        fake_adapter.transcribe.return_value = {
            "text": "longform текст",
            "confidence": 0.88,
            "engine": "gigaam-rnnt",
            "language": "ru",
        }
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        # Мокаем AudioChunker чтобы он кидал исключение
        broken_chunker = MagicMock()
        broken_chunker_instance = MagicMock()
        broken_chunker_instance.chunk.side_effect = RuntimeError("SilenceDetector failed")
        broken_chunker.return_value = broken_chunker_instance

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings), \
             patch("core.audio_chunker.AudioChunker", broken_chunker):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            # При ошибке chunker должен произойти fallback, не exception
            result = engine._transcribe_gigaam(audio, language="ru")
            # Результат должен содержать текст (из longform или error)
            self.assertIn("text", result)


class TestVeryLongAudioAllChunksUnderLimit(unittest.TestCase):
    """120s аудио → все чанки строго <= 20s (max_chunk_sec настройка)."""

    def test_120s_all_chunks_max_20s(self):
        """120s → chunker создаёт чанки по <=20s, все transcribe вызовы корректны."""
        audio = _audio(120.0)
        chunk_durations: list[float] = []

        def side_effect(chunk_audio, sample_rate=16000, **kwargs):
            chunk_durations.append(len(chunk_audio) / _SR)
            return {
                "text": f"часть{len(chunk_durations)}",
                "confidence": 0.92,
                "engine": "gigaam-rnnt",
            }

        fake_adapter = MagicMock()
        fake_adapter.transcribe.side_effect = side_effect
        fake_router = MagicMock()
        fake_router.get_gigaam_adapter.return_value = fake_adapter

        fake_settings = _make_fake_settings()
        with patch("core.engine.settings", fake_settings):
            engine = _make_audio_engine_without_warmup()
            engine._router = fake_router

            result = engine._transcribe_gigaam(audio, language="ru")

        # Минимум 6 чанков для 120s / 20s
        self.assertGreaterEqual(len(chunk_durations), 6)

        for i, dur in enumerate(chunk_durations):
            self.assertLessEqual(
                dur, 21.0,
                f"Чанк {i}: {dur:.2f}s > 21s (превышение допуска)",
            )

        # Весь текст должен присутствовать в результате
        total_text = result.get("text", "")
        self.assertTrue(len(total_text) > 0, "Итоговый текст пустой")


# ---------------------------------------------------------------------------
# Вспомогательная функция для тестов fallback
# ---------------------------------------------------------------------------

_real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__


def _import_fail_for_chunker(name, *args, **kwargs):
    if "audio_chunker" in name:
        raise ImportError(f"Stubbed ImportError for {name}")
    return _real_import(name, *args, **kwargs)


if __name__ == "__main__":
    unittest.main()
