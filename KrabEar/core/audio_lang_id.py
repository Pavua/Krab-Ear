"""Audio-level Language Identification для Krab Ear.

Использует mlx-whisper encoder-only forward pass для определения языка аудио.
Очень быстро (~50ms): берётся первые N секунд аудио → log-mel spectrogram →
detect_language() (encoder + language head, без decoder).

Оборачивается в mlx_lock() согласно MLX thread-safety policy (CLAUDE.md).

Пример использования:
    from core.audio_lang_id import AudioLanguageID
    import numpy as np

    lid = AudioLanguageID()
    audio = np.zeros(48000, dtype=np.float32)  # 3 секунды тишины
    lang = lid.detect(audio, sample_rate=16000)  # None или ISO 639-1
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

import numpy as np

try:
    import mlx.core as mx  # type: ignore[import]
    _HAS_MLX = True
except ImportError:
    _HAS_MLX = False
    mx = None  # type: ignore[assignment]

from core.mlx_lock import mlx_lock

logger = logging.getLogger("KrabEar.AudioLanguageID")


class AudioLanguageID:
    """Определяет язык аудио через mlx-whisper encoder (без decoder).

    Быстрый ~50ms forward pass: log-mel spectrogram → language classification head.
    Все ошибки (импорт, OOM, плохое аудио) → None (graceful fallback).

    Параметры:
        model_path: путь/id mlx-whisper модели (по умолчанию взят из settings).
        preview_sec: сколько секунд аудио использовать для детекции.
                     Берётся из settings.STT_AUDIO_LANG_ID_PREVIEW_SEC если не задан.
    """

    # Singleton-кеш модели (загружается лениво, расшаривается между вызовами)
    _model_cache: Dict[str, Any] = {}
    # Lock защищает _model_cache от race между clear_model_cache() и inference path
    _cache_lock: threading.Lock = threading.Lock()

    @classmethod
    def clear_model_cache(cls) -> None:
        """Сбрасывает Python-ссылку на модель и освобождает Metal GPU буферы.

        W1405 F2 MED: drop Python reference + flush Metal cache через mx.clear_cache().
        Без явного mx.clear_cache() Metal буферы (~300-500 MB) от вытесненной модели
        остаются до следующего inference finally-блока — нелетерминированная утечка.

        Также вызывается из _on_settings_saved_lang_id hook когда MODEL_BALANCED меняется,
        чтобы следующий detect() перезагрузил модель с новым путём.
        Безопасно вызывать конкурентно с detect() — защищено _cache_lock.
        """
        with cls._cache_lock:
            cls._model_cache.clear()
        logger.debug("AudioLanguageID._model_cache очищен по запросу hook'а")
        if _HAS_MLX:
            try:
                with mlx_lock():
                    mx.clear_cache()
            except Exception:
                pass  # MLX не установлен или старая версия без clear_cache

    def __init__(
        self,
        model_path: Optional[str] = None,
        preview_sec: Optional[float] = None,
    ) -> None:
        self._model_path = model_path
        self._preview_sec = preview_sec

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def detect(
        self,
        audio: np.ndarray,
        sample_rate: int = 16000,
        cache: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Определяет язык аудио.

        Параметры:
            audio: PCM float32 массив (mono или stereo). Stereo→mono автоматически.
            sample_rate: частота дискретизации (по умолчанию 16000 Гц).
            cache: внешний dict для кеширования результата детекции.
                   Если cache не None и содержит ключ "audio_lang" — возвращает
                   кешированное значение без повторного inference.
                   После успешной детекции записывает результат в cache["audio_lang"].

        Возвращает:
            ISO 639-1 код языка (например "ru", "en", "es") или None при ошибке
            или если язык не удалось определить надёжно.
        """
        # 1. Проверяем кеш
        if cache is not None and "audio_lang" in cache:
            cached = cache["audio_lang"]
            logger.debug("AudioLanguageID: cache hit → %s", cached)
            return cached

        # 2. Проверяем что флаг включён
        if not self._is_enabled():
            logger.debug("AudioLanguageID: STT_AUDIO_LANG_ID_ENABLED=False → skip")
            return None

        # 3. Проверяем минимальную длину
        preview_sec = self._get_preview_sec()
        min_frames = int(sample_rate * 1.0)  # хотя бы 1 секунда
        audio_mono = self._to_mono(audio)
        if audio_mono is None or len(audio_mono) < min_frames:
            logger.debug(
                "AudioLanguageID: audio too short (%d frames, need %d) → skip",
                len(audio_mono) if audio_mono is not None else 0,
                min_frames,
            )
            return None

        # 4. Обрезаем до preview_sec
        preview_frames = int(sample_rate * preview_sec)
        audio_preview = audio_mono[:preview_frames]

        # 5. Ресемплируем до 16000 Hz если нужно (mlx-whisper требует 16kHz)
        if sample_rate != 16000:
            audio_preview = self._resample(audio_preview, sample_rate, 16000)

        # 6. Запускаем inference под mlx_lock
        result = self._run_detect(audio_preview)

        # 7. Сохраняем в кеш
        if result is not None and cache is not None:
            cache["audio_lang"] = result
            logger.debug("AudioLanguageID: cached result → %s", result)

        return result

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        """Проверяет флаг STT_AUDIO_LANG_ID_ENABLED в settings."""
        try:
            from core.config import settings
            return bool(getattr(settings, "STT_AUDIO_LANG_ID_ENABLED", True))
        except Exception:
            return True  # graceful: если config недоступен — работаем

    def _get_preview_sec(self) -> float:
        """Возвращает длину preview из settings или default 5.0s.

        W1438 F4 MED: preview_sec=0 or None produces an empty audio slice
        which is zero-padded to 30s silence and fed to LID — returns garbage.
        Guard ensures minimum 1.0 second regardless of caller or settings value.
        """
        _MIN_PREVIEW_SEC = 1.0
        if self._preview_sec is not None:
            raw = float(self._preview_sec)
            return max(_MIN_PREVIEW_SEC, raw)
        try:
            from core.config import settings
            raw = float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
            return max(_MIN_PREVIEW_SEC, raw)
        except Exception:
            return 5.0

    def _get_model_path(self) -> str:
        """Возвращает путь к MLX модели для LID."""
        if self._model_path is not None:
            return self._model_path
        try:
            from core.config import settings
            # Используем balanced модель для LID (достаточно маленькая для быстрого LID)
            return getattr(settings, "MODEL_BALANCED", "mlx-community/whisper-large-v3-turbo")
        except Exception:
            return "mlx-community/whisper-large-v3-turbo"

    @staticmethod
    def _to_mono(audio: np.ndarray) -> Optional[np.ndarray]:
        """Конвертирует stereo/multi-channel → mono. Возвращает None если нельзя."""
        try:
            if audio is None:
                return None
            arr = np.asarray(audio, dtype=np.float32)
            if arr.ndim == 1:
                return arr
            if arr.ndim == 2:
                # (channels, samples) или (samples, channels)
                if arr.shape[0] <= arr.shape[1]:
                    return arr.mean(axis=0)
                else:
                    return arr.mean(axis=1)
            return None
        except Exception as exc:
            logger.debug("AudioLanguageID._to_mono error: %s", exc)
            return None

    @staticmethod
    def _resample(audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Линейная интерполяция для ресемплирования (lightweight, не требует librosa).

        Для точного ресемплинга рекомендуется scipy/resampy, но в context LID
        простая линейная интерполяция достаточна — нам нужен только грубый
        спектральный отпечаток для detect_language(), не точная STT транскрипция.
        """
        try:
            ratio = dst_sr / src_sr
            new_len = int(len(audio) * ratio)
            if new_len <= 0:
                return audio
            indices = np.linspace(0, len(audio) - 1, new_len)
            return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
        except Exception as exc:
            logger.debug("AudioLanguageID._resample error: %s", exc)
            return audio

    def _run_detect(self, audio_16k: np.ndarray) -> Optional[str]:
        """Выполняет encoder-only LID inference под mlx_lock().

        Использует mlx_whisper.audio.log_mel_spectrogram + detect_language.
        При любой ошибке возвращает None.

        W1462 fix: mx.clear_cache() вызывается ТОЛЬКО внутри mlx_lock() в
        _detect_with_mlx.finally — НЕ здесь. Вызов clear_cache() вне mlx_lock()
        нарушает MLX thread-safety policy (CLAUDE.md), так как MLX Metal buffers
        нельзя трогать конкурентно с другими MLX операциями.
        """
        try:
            import mlx_whisper  # type: ignore[import]
        except ImportError:
            logger.debug("AudioLanguageID: mlx_whisper не установлен → skip")
            return None

        result = None
        try:
            with mlx_lock():
                result = self._detect_with_mlx(mlx_whisper, audio_16k)
        except Exception as exc:
            logger.warning("AudioLanguageID: inference failed: %s", exc)
        return result

        # F3: Release MLX Metal buffers after inference to prevent memory growth
        # (outside mlx_lock so it doesn't block other threads)
        # Guard: only clear if mlx.core is already in sys.modules to avoid
        # double-registration crash (nanobind) in test environments.
        import sys as _sys
        _mx = _sys.modules.get("mlx.core")
        if _mx is not None:
            try:
                _mx.clear_cache()
            except Exception:
                pass

        return result

    def _detect_with_mlx(self, mlx_whisper: Any, audio_16k: np.ndarray) -> Optional[str]:
        """Внутренний метод LID внутри mlx_lock() контекста.

        Загружает модель (кешируется), строит log-mel, вызывает detect_language().
        W63 rule: mx.clear_cache() вызывается в finally после любого пути inference
        (успех, ошибка mel, ошибка detect_language) — предотвращает утечку MLX Metal
        буферов на длинных сессиях (W1358 F2).
        """
        try:
            return self._run_lid_inference(mlx_whisper, audio_16k)
        finally:
            # W63 rule: free MLX Metal buffers after every LID inference to
            # prevent memory accumulation over long sessions (W1358 F2).
            if _HAS_MLX:
                mx.clear_cache()

    def _run_lid_inference(self, mlx_whisper: Any, audio_16k: np.ndarray) -> Optional[str]:
        """Выполняет загрузку модели, построение mel и detect_language.

        Вызывается из _detect_with_mlx(), которая оборачивает вызов в try/finally
        для гарантированного вызова mx.clear_cache().
        """
        model_path = self._get_model_path()

        # Ленивая загрузка модели с кешированием (max 1 запись).
        # H4: старые записи — чистый leak: объект модели удерживает MLX Metal
        # буферы, даже после mx.clear_cache() в engine.py.  Держим только
        # текущую модель; при смене профиля (balanced→max) старая вытесняется.
        # _cache_lock защищает от race с clear_model_cache() (W1340 fix).
        with AudioLanguageID._cache_lock:
            if model_path not in AudioLanguageID._model_cache:
                logger.debug("AudioLanguageID: загружаем модель %s для LID", model_path)
                if len(AudioLanguageID._model_cache) >= 1:
                    logger.debug("AudioLanguageID: вытесняем старую модель из кеша")
                    AudioLanguageID._model_cache.clear()
                try:
                    model = mlx_whisper.load_models.load_model(model_path)
                    AudioLanguageID._model_cache[model_path] = model
                except Exception as exc:
                    logger.warning(
                        "AudioLanguageID: не удалось загрузить модель %s: %s",
                        model_path,
                        exc,
                    )
                    return None

            model = AudioLanguageID._model_cache[model_path]

        # F1: Early-exit on all-silence (zero-peak) input — no point building mel on zeros
        _peak = float(np.abs(audio_16k).max()) if audio_16k.size > 0 else 0.0
        if _peak < 1e-6:
            logger.debug(
                "AudioLanguageID: all-silence detected (peak=%.2e) → returning 'und'",
                _peak,
            )
            return "und"  # undetermined — silence has no language

        # Строим log-mel spectrogram
        try:
            # mlx_whisper ожидает float32 numpy array нормализованный в [-1, 1]
            audio_norm = audio_16k.astype(np.float32)
            # Нормализуем пик если нужно
            peak = float(np.max(np.abs(audio_norm)))
            if peak > 1.0:
                audio_norm = audio_norm / peak

            # Pad до минимальной длины (30 секунд = 480000 samples @ 16kHz)
            n_samples = 16000 * 30
            if len(audio_norm) < n_samples:
                audio_norm = np.pad(audio_norm, (0, n_samples - len(audio_norm)))

            mel = mlx_whisper.audio.log_mel_spectrogram(audio_norm)

        except Exception as exc:
            logger.warning("AudioLanguageID: log_mel_spectrogram failed: %s", exc)
            return None

        # detect_language
        try:
            # mlx_whisper.decoding.detect_language(model, mel) → dict[str, float]
            # или может возвращать (str, dict) в разных версиях — обрабатываем оба
            result = mlx_whisper.decoding.detect_language(model, mel)

            probs: dict = {}
            if isinstance(result, tuple):
                # (language_str, probs_dict)
                lang_code = result[0]
                if len(result) > 1 and isinstance(result[1], dict):
                    probs = result[1]
            elif isinstance(result, dict):
                # {lang: prob, ...} — берём argmax
                probs = result
                lang_code = max(result, key=lambda k: result[k])
            elif isinstance(result, str):
                lang_code = result
            else:
                logger.warning(
                    "AudioLanguageID: неожиданный тип результата detect_language: %s",
                    type(result),
                )
                return None

            # F2: Confidence threshold — reject low-confidence results (music/noise/silence)
            _MIN_LANG_CONFIDENCE = 0.40
            if probs:
                best_prob = probs.get(str(lang_code).strip().lower(), 0.0)
                if best_prob < _MIN_LANG_CONFIDENCE:
                    logger.debug(
                        "AudioLanguageID: low confidence (%.2f < %.2f) for lang=%s → 'und'",
                        best_prob,
                        _MIN_LANG_CONFIDENCE,
                        lang_code,
                    )
                    return "und"  # undetermined — caller should fall through to alternate detection

            lang_code = str(lang_code).strip().lower()
            logger.info("AudioLanguageID: detected language = %s", lang_code)
            return lang_code if lang_code else None

        except Exception as exc:
            logger.warning("AudioLanguageID: detect_language failed: %s", exc)
            return None
