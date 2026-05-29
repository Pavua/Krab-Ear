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

from core.mlx_lock import mlx_lock

logger = logging.getLogger("KrabEar.AudioLanguageID")

# W1090 / W1525 guard — DO NOT DROP IN CHERRY-PICKS.
# W1497 cherry-pick train reverted these constants via --theirs strategy.
# Any future cherry-pick that touches this file must preserve both guards.
#
# F1: skip encoder on silent/near-silent audio (avoids spurious detections
#     and wastes GPU time on pure silence).
_MIN_PEAK_AMPLITUDE: float = 1e-4

# F2: discard low-confidence detect_language results (Whisper LID head is
#     unreliable below ~0.35 — language code may flip randomly on short clips).
_MIN_CONFIDENCE: float = 0.35

# W1121 / W1581: canonical allowlist of language codes returned by detect().
# Exact four codes: ru (Russian), uk (Ukrainian), en (English), es (Spanish).
# Ukrainian is critical — do NOT replace with de/fr/it/pt (W1575 F1 HIGH regression).
# Codes outside this set emit a WARNING and are returned as-is (STTRouter decides),
# unless restrict_to_supported=True is set on the instance (returns None instead).
SUPPORTED_LANGUAGES = frozenset({"ru", "uk", "en", "es"})


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
    # Lock guards _model_cache modifications from clear_model_cache() vs _detect_with_mlx().
    # Note: _detect_with_mlx() already runs inside mlx_lock(), so this lock is only
    # needed for clear_model_cache() calls from outside the mlx_lock() context.
    _cache_lock: threading.Lock = threading.Lock()

    # W1090 / W1525 guard constants exposed as class attributes for testability.
    # DO NOT REMOVE — reverted by W1497 cherry-pick train, restored W1530.
    _ZERO_PEAK_THRESHOLD: float = _MIN_PEAK_AMPLITUDE  # 1e-4
    MIN_CONFIDENCE: float = _MIN_CONFIDENCE  # 0.35

    def __init__(
        self,
        model_path: Optional[str] = None,
        preview_sec: Optional[float] = None,
        restrict_to_supported: bool = False,
    ) -> None:
        self._model_path = model_path
        self._preview_sec = preview_sec
        # W1581: when True, codes not in SUPPORTED_LANGUAGES → None (hard filter).
        # Default False: unsupported codes are returned with a WARNING so STTRouter
        # can decide what to do (soft-warn mode is the production default).
        self._restrict_to_supported = restrict_to_supported

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

        # 6. Запускаем inference под mlx_lock (F1 silent-audio guard lives inside _run_detect)
        result = self._run_detect(audio_preview)

        # 7. W1121 / W1581: allowlist gate after _run_detect.
        # Unsupported lang codes emit a WARNING so STTRouter can decide what to do.
        # Hard-filter mode (restrict_to_supported=True) returns None instead.
        # DO NOT REMOVE — W1561 used wrong set {ru,es,en,de,fr,it,pt}; canonical is {ru,uk,en,es}.
        if result is not None and result not in SUPPORTED_LANGUAGES:
            logger.warning(
                "AudioLanguageID: detected unsupported language, STTRouter will use fallback",
                extra={"detected_lang": result, "fallback": "other"},
            )
            if self._restrict_to_supported:
                logger.debug(
                    "AudioLanguageID: restrict_to_supported=True → suppressing lang=%s",
                    result,
                )
                return None

        # 8. Сохраняем в кеш
        if result is not None and cache is not None:
            cache["audio_lang"] = result
            logger.debug("AudioLanguageID: cached result → %s", result)

        return result

    @classmethod
    def clear_model_cache(cls) -> None:
        """Вытесняет загруженную LID-модель из кеша класса.

        Вызывается из _on_settings_saved hook в BackendService при изменении
        MODEL_BALANCED — предотвращает использование стale модели после переключения
        профиля STT, устраняя cold-load stall внутри mlx_lock() на следующей записи.

        Потокобезопасно: захватывает _cache_lock перед очисткой dict.
        """
        with cls._cache_lock:
            if cls._model_cache:
                logger.debug(
                    "AudioLanguageID.clear_model_cache: вытесняем %d запись(ей) из кеша",
                    len(cls._model_cache),
                )
                cls._model_cache.clear()
            else:
                logger.debug("AudioLanguageID.clear_model_cache: кеш уже пуст")

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
        """Возвращает длину preview из settings или default 5.0s."""
        if self._preview_sec is not None:
            return self._preview_sec
        try:
            from core.config import settings
            return float(getattr(settings, "STT_AUDIO_LANG_ID_PREVIEW_SEC", 5.0))
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

        F1 guard (W1090 / W1525) lives here — skips MLX encoder on silent/near-silent
        audio to avoid spurious detections and wasted GPU time.
        DO NOT REMOVE — reverted by W1497 cherry-pick train, restored W1530.
        Keeping it here (not in detect()) lets unit tests monkey-patch _run_detect
        while still exercising allowlist logic in detect().
        """
        # F1 guard (W1090 / W1525): skip encoder on silent/near-silent audio.
        peak = float(np.max(np.abs(audio_16k))) if len(audio_16k) > 0 else 0.0
        if peak < _MIN_PEAK_AMPLITUDE:
            logger.debug(
                "audio_lang_id: peak %.4f below threshold %.4f, skipping encoder",
                peak,
                _MIN_PEAK_AMPLITUDE,
            )
            return None

        try:
            import mlx_whisper  # type: ignore[import]
        except ImportError:
            logger.debug("AudioLanguageID: mlx_whisper не установлен → skip")
            return None

        try:
            with mlx_lock():
                return self._detect_with_mlx(mlx_whisper, audio_16k)
        except Exception as exc:
            logger.warning("AudioLanguageID: inference failed: %s", exc)
            return None

    def _detect_with_mlx(self, mlx_whisper: Any, audio_16k: np.ndarray) -> Optional[str]:
        """Внутренний метод LID внутри mlx_lock() контекста.

        Загружает модель (кешируется), строит log-mel, вызывает detect_language().
        """
        model_path = self._get_model_path()

        # Ленивая загрузка модели с кешированием (max 1 запись).
        # H4: старые записи — чистый leak: объект модели удерживает MLX Metal
        # буферы, даже после mx.clear_cache() в engine.py.  Держим только
        # текущую модель; при смене профиля (balanced→max) старая вытесняется.
        # _cache_lock защищает от гонки с clear_model_cache() из settings hook.
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

            probs: Optional[dict] = None
            if isinstance(result, tuple):
                # (language_str, probs_dict)
                lang_code = result[0]
                if len(result) >= 2 and isinstance(result[1], dict):
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

            lang_code = str(lang_code).strip().lower()

            # F2 guard (W1090 / W1525): drop low-confidence detections.
            # DO NOT REMOVE — reverted by W1497 cherry-pick train, restored W1530.
            if probs is not None and lang_code:
                confidence = float(probs.get(lang_code, 0.0))
                if confidence < _MIN_CONFIDENCE:
                    logger.debug(
                        "audio_lang_id: confidence %.3f for '%s' below threshold %.3f, dropping",
                        confidence,
                        lang_code,
                        _MIN_CONFIDENCE,
                    )
                    return None

            logger.info("AudioLanguageID: detected language = %s", lang_code)
            return lang_code if lang_code else None

        except Exception as exc:
            logger.warning("AudioLanguageID: detect_language failed: %s", exc)
            return None
