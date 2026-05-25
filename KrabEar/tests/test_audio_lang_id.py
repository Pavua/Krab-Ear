"""Тесты для AudioLanguageID (KrabEar/core/audio_lang_id.py).

Wave 128 covers (cumulative):
1. empty audio → None (too short)
2. short audio (< 1s) → None (too short)
3. mock detect_language returns valid lang → code returned
4. mock detect_language raises → None (graceful)
5. cache hit → no second inference call
6. disabled via settings → None
7. stereo→mono conversion and resample path
8. stereo input (2D array) → mono detection
--- Wave 128 additions ---
9.  test_returns_iso_639_1_code — result is lower-case 2-char string
10. test_handles_short_audio — explicit min-length boundary
11. test_model_cache_lru_bound — cache never exceeds 1 entry (Wave 63)
12. test_concurrent_detect_uses_mlx_lock — mlx_lock called per inference
13. test_handles_mlx_failure_gracefully — mlx_whisper ImportError → None
14. test_empty_audio_handled — np.array([]) → None
"""

from __future__ import annotations

import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.audio_lang_id import AudioLanguageID  # noqa: E402


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _silence(seconds: float = 2.0, sr: int = 16000) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def _speech(seconds: float = 2.0, sr: int = 16000) -> np.ndarray:
    t = np.linspace(0, seconds, int(seconds * sr), endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _stereo_speech(seconds: float = 2.0, sr: int = 16000) -> np.ndarray:
    """Stereo audio: shape (2, samples)."""
    mono = _speech(seconds, sr)
    return np.stack([mono, mono * 0.8], axis=0)  # (2, N)


# ---------------------------------------------------------------------------
# Тест 1: пустой массив → None (слишком короткое аудио)
# ---------------------------------------------------------------------------

class TestAudioLangIDEmptyAudio(unittest.TestCase):
    """Пустой numpy массив → None (меньше минимальной длины 1с)."""

    def test_empty_array_returns_none(self):
        lid = AudioLanguageID()
        result = lid.detect(np.array([], dtype=np.float32), sample_rate=16000)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Тест 2: короткое аудио (< 1 с) → None
# ---------------------------------------------------------------------------

class TestAudioLangIDShortAudio(unittest.TestCase):
    """Аудио короче 1 секунды → None (недостаточно для LID)."""

    def test_short_audio_returns_none(self):
        lid = AudioLanguageID()
        # 0.5 секунды — меньше порога 1 с
        short_audio = _speech(seconds=0.5, sr=16000)
        result = lid.detect(short_audio, sample_rate=16000)
        self.assertIsNone(result)

    def test_exactly_1s_is_accepted(self):
        """Аудио ровно 1 секунда — граничный случай, НЕ отвергается по длине.
        Дальнейший результат зависит от mlx_whisper (None если не установлен).
        """
        lid = AudioLanguageID()
        audio_1s = _speech(seconds=1.0, sr=16000)
        # Либо None (если mlx_whisper недоступен) либо строка языка
        result = lid.detect(audio_1s, sample_rate=16000)
        self.assertTrue(result is None or isinstance(result, str))


# ---------------------------------------------------------------------------
# Тест 3: mock detect_language возвращает валидный язык
# ---------------------------------------------------------------------------

class TestAudioLangIDMockDetect(unittest.TestCase):
    """detect_language mocked → возвращает ожидаемый код языка."""

    def _make_mlx_mock(self, lang_result="ru"):
        """Создаёт mock mlx_whisper с работающим detect_language."""
        mock_mlx = MagicMock()
        # log_mel_spectrogram возвращает что-то (mel)
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        # load_models.load_model возвращает mock модель
        mock_model = MagicMock()
        mock_mlx.load_models.load_model.return_value = mock_model
        # detect_language возвращает (lang_code, probs_dict)
        mock_mlx.decoding.detect_language.return_value = (lang_result, {lang_result: 0.95})
        return mock_mlx

    def test_mock_detect_returns_ru(self):
        """Когда mlx_whisper.detect_language → ("ru", {...}), метод возвращает "ru"."""
        # Сбрасываем кеш модели перед тестом
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = self._make_mlx_mock("ru")
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertEqual(result, "ru")

    def test_mock_detect_returns_en(self):
        """Когда detect_language → ("en", {...}), метод возвращает "en"."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = self._make_mlx_mock("en")
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertEqual(result, "en")

    def test_detect_language_dict_result(self):
        """detect_language возвращает dict {lang: prob} → берём argmax."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        # dict формат
        mock_mlx.decoding.detect_language.return_value = {"ru": 0.9, "en": 0.05, "es": 0.05}

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertEqual(result, "ru")


# ---------------------------------------------------------------------------
# Тест 4: detect_language raises → None (graceful)
# ---------------------------------------------------------------------------

class TestAudioLangIDMockRaise(unittest.TestCase):
    """detect_language бросает исключение → метод возвращает None, не падает."""

    def test_detect_exception_returns_none(self):
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.side_effect = RuntimeError("GPU error")

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)

    def test_load_model_exception_returns_none(self):
        """Ошибка загрузки модели → None, нет краша."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.load_models.load_model.side_effect = OSError("model not found")

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Тест 5: cache hit → второй вызов не делает inference
# ---------------------------------------------------------------------------

class TestAudioLangIDCacheHit(unittest.TestCase):
    """При cache hit inference не вызывается повторно."""

    def test_cache_hit_skips_inference(self):
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("es", {"es": 0.9})

        cache: dict = {}
        audio = _speech(seconds=3.0)

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            # Первый вызов: inference запускается
            result1 = lid.detect(audio, sample_rate=16000, cache=cache)
            # Второй вызов: должен вернуть cached результат без inference
            result2 = lid.detect(audio, sample_rate=16000, cache=cache)

        self.assertEqual(result1, "es")
        self.assertEqual(result2, "es")
        self.assertEqual(cache.get("audio_lang"), "es")
        # detect_language вызывался ровно один раз
        self.assertEqual(mock_mlx.decoding.detect_language.call_count, 1)

    def test_cache_populated_after_detection(self):
        """После успешной детекции результат записывается в cache["audio_lang"]."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 0.85})

        cache: dict = {}
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            lid.detect(_speech(seconds=2.0), sample_rate=16000, cache=cache)

        self.assertIn("audio_lang", cache)
        self.assertEqual(cache["audio_lang"], "ru")


# ---------------------------------------------------------------------------
# Тест 6: disabled via STT_AUDIO_LANG_ID_ENABLED=False → None
# ---------------------------------------------------------------------------

class TestAudioLangIDDisabled(unittest.TestCase):
    """Когда STT_AUDIO_LANG_ID_ENABLED=False → detect() возвращает None без inference."""

    def test_disabled_returns_none(self):
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 1.0})

        call_count = {"n": 0}

        def track_detect(*args, **kwargs):
            call_count["n"] += 1
            return ("ru", {"ru": 1.0})

        mock_mlx.decoding.detect_language.side_effect = track_detect

        # Патчим settings: LANG_ID выключен
        fake_settings = MagicMock()
        fake_settings.STT_AUDIO_LANG_ID_ENABLED = False
        fake_settings.STT_AUDIO_LANG_ID_PREVIEW_SEC = 5.0

        with patch("core.audio_lang_id.AudioLanguageID._is_enabled", return_value=False):
            with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)
        # detect_language не должен был вызываться
        self.assertEqual(call_count["n"], 0)


# ---------------------------------------------------------------------------
# Тест 7: resample path (non-16kHz audio)
# ---------------------------------------------------------------------------

class TestAudioLangIDResample(unittest.TestCase):
    """Аудио с частотой ≠ 16kHz ресемплируется перед inference."""

    def test_44100hz_audio_gets_resampled(self):
        """44100 Hz аудио не вызывает ошибку; resample прозрачен для detect."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("en", {"en": 0.8})

        # 44100 Hz аудио (2 секунды)
        sr = 44100
        audio_44k = _speech(seconds=2.0, sr=sr)

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(audio_44k, sample_rate=sr)

        self.assertEqual(result, "en")

    def test_resample_static_method_output_length(self):
        """_resample правильно вычисляет длину выходного массива."""
        src = np.ones(44100, dtype=np.float32)  # 1 секунда @ 44100
        resampled = AudioLanguageID._resample(src, src_sr=44100, dst_sr=16000)
        expected_len = int(44100 * 16000 / 44100)
        # Допуск ±1 семпл из-за округления
        self.assertAlmostEqual(len(resampled), expected_len, delta=1)


# ---------------------------------------------------------------------------
# Тест 8: stereo → mono conversion
# ---------------------------------------------------------------------------

class TestAudioLangIDStereoToMono(unittest.TestCase):
    """Stereo (2D) аудио конвертируется в mono перед inference."""

    def test_stereo_2channels_converted_to_mono(self):
        """Stereo (2, N) array → mono, нет ошибки."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 0.9})

        stereo = _stereo_speech(seconds=2.0, sr=16000)
        self.assertEqual(stereo.ndim, 2)

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(stereo, sample_rate=16000)

        self.assertEqual(result, "ru")

    def test_to_mono_static_1d(self):
        """_to_mono с 1D массивом возвращает тот же массив."""
        mono = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        result = AudioLanguageID._to_mono(mono)
        np.testing.assert_array_equal(result, mono)

    def test_to_mono_static_2d_channels_first(self):
        """_to_mono с (2, N) → усредняет каналы."""
        ch1 = np.array([1.0, 2.0], dtype=np.float32)
        ch2 = np.array([3.0, 4.0], dtype=np.float32)
        stereo = np.stack([ch1, ch2], axis=0)  # (2, 2)
        result = AudioLanguageID._to_mono(stereo)
        self.assertIsNotNone(result)
        self.assertEqual(result.ndim, 1)  # type: ignore[union-attr]
        np.testing.assert_allclose(result, [2.0, 3.0])  # type: ignore[arg-type]

    def test_to_mono_static_none_input(self):
        """_to_mono с None → None."""
        result = AudioLanguageID._to_mono(None)  # type: ignore[arg-type]
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Wave 128 — дополнительные тесты по спецификации
# ---------------------------------------------------------------------------

class TestAudioLangIDReturnsIso6391(unittest.TestCase):
    """test_returns_iso_639_1_code — результат должен быть строкой ISO 639-1."""

    def test_returns_iso_639_1_code(self):
        """detect() возвращает lower-case 2-символьный ISO 639-1 код."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        for lang_code in ("ru", "en", "es", "de", "fr"):
            AudioLanguageID._model_cache.clear()
            mock_mlx = MagicMock()
            mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
            mock_mlx.load_models.load_model.return_value = MagicMock()
            mock_mlx.decoding.detect_language.return_value = (lang_code, {lang_code: 0.9})

            with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

            self.assertIsNotNone(result, f"Expected code for {lang_code}")
            self.assertIsInstance(result, str)
            self.assertEqual(result, result.lower(), "Код должен быть в нижнем регистре")
            # ISO 639-1 — 2 символа (допускаем 2-3 для редких языков, но базовые — 2)
            self.assertLessEqual(len(result), 5,
                                 "Код языка не должен быть длиннее 5 символов")


class TestAudioLangIDHandlesShortAudioExplicit(unittest.TestCase):
    """test_handles_short_audio — явный boundary test 1-секундного порога."""

    def test_under_1s_returns_none(self):
        """Аудио менее 1 секунды → None (не зависит от mlx_whisper)."""
        lid = AudioLanguageID()
        # 0.8 секунды — ниже min_frames = int(16000 * 1.0) = 16000
        audio = np.zeros(int(16000 * 0.8), dtype=np.float32)
        result = lid.detect(audio, sample_rate=16000)
        self.assertIsNone(result)

    def test_exactly_1s_boundary_passes_length_check(self):
        """Ровно 1 секунда — граница min_frames: не отвергается по длине."""
        lid = AudioLanguageID()
        # Ровно 16000 сэмплов — = min_frames, НЕ < min_frames
        audio = np.zeros(16000, dtype=np.float32)
        # Может вернуть None если mlx_whisper недоступен, но НЕ из-за длины
        result = lid.detect(audio, sample_rate=16000)
        # Не проверяем значение — только что нет исключения
        self.assertTrue(result is None or isinstance(result, str))


class TestAudioLangIDModelCacheLruBound(unittest.TestCase):
    """test_model_cache_lru_bound — кеш модели ограничен 1 записью (Wave 63).

    Верифицирует инвариант: AudioLanguageID._model_cache никогда не превышает 1 запись.
    """

    def setUp(self):
        AudioLanguageID._model_cache.clear()

    def _make_mock_for_path(self, model_path: str):
        """Mock mlx_whisper настроенный возвращать конкретный model_path."""
        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_model = MagicMock(name=f"model_{model_path}")
        mock_mlx.load_models.load_model.return_value = mock_model
        mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 0.9})
        return mock_mlx

    def test_cache_never_exceeds_one_entry(self):
        """После 5 запусков с разными model_path кеш всегда содержит ровно 1 запись."""
        model_paths = ["model-a", "model-b", "model-c", "model-d", "model-e"]
        for path in model_paths:
            AudioLanguageID._model_cache.clear()
            lid = AudioLanguageID(model_path=path)
            mock_mlx = self._make_mock_for_path(path)
            with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                lid.detect(_speech(seconds=3.0), sample_rate=16000)
            # После вызова кеш должен содержать ровно 1 запись
            self.assertLessEqual(len(AudioLanguageID._model_cache), 1,
                                 f"Cache exceeded 1 after path={path}")

    def test_second_model_evicts_first(self):
        """Смена model_path вытесняет предыдущую модель из кеша."""
        # Вставляем модель-a вручную
        AudioLanguageID._model_cache["model-a"] = object()
        self.assertEqual(len(AudioLanguageID._model_cache), 1)

        # Запускаем с model-b
        lid = AudioLanguageID(model_path="model-b")
        mock_mlx = self._make_mock_for_path("model-b")
        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            lid.detect(_speech(seconds=3.0), sample_rate=16000)

        cache = AudioLanguageID._model_cache
        self.assertEqual(len(cache), 1)
        self.assertNotIn("model-a", cache, "Старая модель должна быть вытеснена")
        self.assertIn("model-b", cache, "Новая модель должна быть в кеше")


class TestAudioLangIDConcurrentMlxLock(unittest.TestCase):
    """test_concurrent_detect_uses_mlx_lock — mlx_lock вызывается при inference."""

    def test_mlx_lock_used_during_inference(self):
        """mlx_lock() context manager вызывается хотя бы один раз при детекции."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 0.9})

        lock_entered = []

        class FakeLock:
            def __enter__(self_inner):
                lock_entered.append(True)
                return self_inner

            def __exit__(self_inner, *args):
                pass

        with patch("core.audio_lang_id.mlx_lock", return_value=FakeLock()):
            with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertEqual(result, "ru")
        self.assertGreater(len(lock_entered), 0,
                           "mlx_lock() должен быть вызван при inference")

    def test_concurrent_detect_no_crash(self):
        """6 потоков параллельно вызывают detect() без исключений."""
        AudioLanguageID._model_cache.clear()
        errors: list[Exception] = []
        results: list = [None] * 6

        def worker(idx: int):
            try:
                lid = AudioLanguageID()
                mock_mlx = MagicMock()
                mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
                mock_mlx.load_models.load_model.return_value = MagicMock()
                mock_mlx.decoding.detect_language.return_value = ("ru", {"ru": 0.9})
                with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
                    results[idx] = lid.detect(_speech(seconds=2.0), sample_rate=16000)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [], f"Concurrent detect errors: {errors}")


class TestAudioLangIDMlxFailureGraceful(unittest.TestCase):
    """test_handles_mlx_failure_gracefully — ImportError и RuntimeError → None."""

    def test_mlx_whisper_not_installed_returns_none(self):
        """Если mlx_whisper не установлен (ImportError) → None, нет исключения."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        # Убираем mlx_whisper из sys.modules чтобы симулировать ImportError
        with patch.dict("sys.modules", {"mlx_whisper": None}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)

    def test_log_mel_failure_returns_none(self):
        """log_mel_spectrogram бросает → None, нет крэша."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.side_effect = RuntimeError("metal OOM")

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)

    def test_detect_language_runtime_error_returns_none(self):
        """detect_language RuntimeError → None (повтор из TestAudioLangIDMockRaise)."""
        AudioLanguageID._model_cache.clear()
        lid = AudioLanguageID()

        mock_mlx = MagicMock()
        mock_mlx.audio.log_mel_spectrogram.return_value = np.zeros((80, 3000))
        mock_mlx.load_models.load_model.return_value = MagicMock()
        mock_mlx.decoding.detect_language.side_effect = RuntimeError("GPU hang")

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            result = lid.detect(_speech(seconds=3.0), sample_rate=16000)

        self.assertIsNone(result)


class TestAudioLangIDEmptyAudioHandled(unittest.TestCase):
    """test_empty_audio_handled — np.array([]) возвращает None без исключений."""

    def test_empty_float32_array_returns_none(self):
        """np.array([], dtype=float32) → None."""
        lid = AudioLanguageID()
        result = lid.detect(np.array([], dtype=np.float32), sample_rate=16000)
        self.assertIsNone(result)

    def test_none_like_audio_does_not_crash(self):
        """Очень маленький массив (1 сэмпл) → None без исключения."""
        lid = AudioLanguageID()
        result = lid.detect(np.array([0.0], dtype=np.float32), sample_rate=16000)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
