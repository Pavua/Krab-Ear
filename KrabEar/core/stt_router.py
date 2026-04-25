"""Language-aware STT router для Krab Ear.

Включает маршрутизацию на GigaAM-RNNT v2 для русскоязычного аудио (PR feat/stt-gigaam-ru-adapter).
Реальная интеграция в AudioEngine запланирована на follow-up PR.
Другие RU-специализированные модели (Parakeet-RU, fine-tuned Whisper и др.) — в follow-up research.

Архитектура:
    STTRouter.select_model(audio_data, sample_rate, hint_language) -> model_id

    1. Если STT_LANGUAGE_ROUTING_ENABLED=False → возвращает STT_OTHER_PRIMARY_MODEL
       (текущий whisper-large-v3 generalist — обратная совместимость).
    2. Если hint_language задан явно → использует его напрямую.
    3. Иначе → определяет язык через AudioLanguageID (core/audio_lang_id.py):
       encoder-only mlx-whisper forward pass, ~50ms. При STT_AUDIO_LANG_ID_ENABLED=False,
       слишком коротком аудио или ошибке mlx-whisper — graceful fallback placeholder "ru"
       (primary user language, 80%+ RU).
    4. Маппинг language → model_id через конфиг:
       ru → STT_RU_PRIMARY_MODEL, en → STT_EN_PRIMARY_MODEL,
       es → STT_ES_PRIMARY_MODEL, * → STT_OTHER_PRIMARY_MODEL.

Добавление новой модели (когда research завершится):
    1. Создай адаптер в core/pipeline/stt_<name>.py (по образцу stt_whisper.py).
    2. Зарегистрируй адаптер в adapter_factory.
    3. Измени STT_RU_PRIMARY_MODEL default в core/config.py на ID новой модели.
    4. Включи STT_LANGUAGE_ROUTING_ENABLED=True.
    5. Интегрируй self._router в AudioEngine.transcribe() (см. заглушку там).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import numpy as np

logger = logging.getLogger("KrabEar.STTRouter")

# Идентификатор GigaAM в fallback chain
_GIGAAM_MODEL_ID = "gigaam"

# Маппинг ISO 639-1 → атрибут конфига для primary model
_LANG_TO_CONFIG_ATTR: dict[str, str] = {
    "ru": "STT_RU_PRIMARY_MODEL",
    "uk": "STT_RU_PRIMARY_MODEL",   # украинский → RU модель (ближайшая)
    "en": "STT_EN_PRIMARY_MODEL",
    "es": "STT_ES_PRIMARY_MODEL",
}

# Первые N секунд аудио для эвристики определения языка (fallback placeholder)
_AUDIO_SAMPLE_SECONDS = 5

# Минимальная длина аудио (секунды) для попытки audio-level LID
_AUDIO_LID_MIN_SEC = 1.0


class STTRouter:
    """Маршрутизатор STT: выбирает модель под язык входящего аудио.

    Параметры:
        settings: объект конфига (core.config.Settings или duck-typed stub).
        language_detector: экземпляр LanguageDetector (передаётся извне для DI).
                           Устаревший параметр — audio-level LID реализован через
                           AudioLanguageID (core/audio_lang_id.py).
        adapter_factory: callable(model_id: str) -> adapter.
                         Вызывается router'ом когда нужно верифицировать/загрузить
                         адаптер под выбранную модель. Может быть None — тогда
                         select_model возвращает только строку модели без загрузки.
    """

    def __init__(
        self,
        settings: Any,
        language_detector: Any = None,
        adapter_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._settings = settings
        self._language_detector = language_detector
        self._adapter_factory = adapter_factory
        # Lazy-init AudioLanguageID (создаётся при первом использовании)
        self._lang_id: Optional[Any] = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def select_model(
        self,
        audio_data: Optional[np.ndarray] = None,
        sample_rate: int = 16000,
        hint_language: Optional[str] = None,
    ) -> str:
        """Выбирает идентификатор STT-модели для данного аудио.

        Параметры:
            audio_data: PCM float32 numpy-массив (mono, sample_rate Hz).
                        Используется для автодетекции языка если hint_language=None.
                        Может быть None — тогда detection пропускается, возвращается
                        fallback модель.
            sample_rate: частота дискретизации audio_data (по умолчанию 16 000 Гц).
            hint_language: явный language hint (ISO 639-1: "ru", "en", "es", ...).
                           Если задан — detection пропускается.

        Возвращает:
            Строка — идентификатор модели (например,
            "mlx-community/whisper-large-v3-mlx").
        """
        # 1. Routing отключён → generalist model (обратная совместимость)
        if not getattr(self._settings, "STT_LANGUAGE_ROUTING_ENABLED", False):
            model_id = getattr(
                self._settings,
                "STT_OTHER_PRIMARY_MODEL",
                "mlx-community/whisper-large-v3-mlx",
            )
            logger.debug(
                "STTRouter: routing disabled, using OTHER_PRIMARY=%s", model_id
            )
            return model_id

        # 2. Определяем язык
        lang = self._resolve_language(audio_data, sample_rate, hint_language)

        # 3. Маппинг язык → модель
        model_id = self._lang_to_model(lang)
        logger.info(
            "STTRouter: lang=%s → model=%s (hint=%s)", lang, model_id, hint_language
        )

        # 4. Опционально вызываем adapter_factory для lazy-load / проверки
        if self._adapter_factory is not None:
            try:
                self._adapter_factory(model_id)
            except Exception as exc:
                logger.warning(
                    "STTRouter: adapter_factory(%s) failed: %s — "
                    "falling back to OTHER_PRIMARY",
                    model_id,
                    exc,
                )
                model_id = getattr(
                    self._settings,
                    "STT_OTHER_PRIMARY_MODEL",
                    "mlx-community/whisper-large-v3-mlx",
                )

        return model_id

    def get_gigaam_adapter(self) -> Optional[Any]:
        """Возвращает инициализированный GigaAMAdapter если STT_GIGAAM_ENABLED=True.

        Условия возврата не-None:
        1. settings.STT_GIGAAM_ENABLED == True
        2. `gigaam` пакет доступен для импорта (pip install gigaam)

        Возвращает GigaAMAdapter или None (если отключён или gigaam не установлен).

        Интеграция в AudioEngine:
            Когда detected_lang == "ru" AND get_gigaam_adapter() is not None →
            пробуем GigaAM первым, при ошибке fallback на whisper-large-v3.
            TODO(follow-up): интегрировать в AudioEngine.transcribe() chain.
        """
        if not getattr(self._settings, "STT_GIGAAM_ENABLED", False):
            logger.debug("STTRouter.get_gigaam_adapter: STT_GIGAAM_ENABLED=False → None")
            return None

        try:
            from core.pipeline.stt_gigaam import GigaAMAdapter  # type: ignore[import]
        except ImportError:
            logger.warning(
                "STTRouter.get_gigaam_adapter: core.pipeline.stt_gigaam не найден"
            )
            return None

        mode = getattr(self._settings, "STT_GIGAAM_MODE", "rnnt")
        device = getattr(self._settings, "STT_GIGAAM_DEVICE", "mps")

        try:
            adapter = GigaAMAdapter(device=device, mode=mode)
            logger.debug(
                "STTRouter.get_gigaam_adapter: адаптер создан (mode=%s, device=%s)",
                mode,
                device,
            )
            return adapter
        except Exception as exc:
            logger.warning("STTRouter.get_gigaam_adapter: ошибка создания адаптера: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _get_lang_id(self) -> Any:
        """Lazy-init AudioLanguageID singleton (один на router instance)."""
        if self._lang_id is None:
            try:
                from core.audio_lang_id import AudioLanguageID
                self._lang_id = AudioLanguageID()
            except Exception as exc:
                logger.warning("STTRouter: не удалось создать AudioLanguageID: %s", exc)
                self._lang_id = None
        return self._lang_id

    def _resolve_language(
        self,
        audio_data: Optional[np.ndarray],
        sample_rate: int,
        hint_language: Optional[str],
    ) -> str:
        """Определяет язык из hint или через audio-level LID.

        Приоритет:
        1. hint_language != None → возвращаем его (нормализованный в lowercase).
        2. audio_data == None → возвращаем "und" (undetermined → OTHER_PRIMARY).
        3. Аудио слишком короткое (< 1с) → placeholder "ru".
        4. STT_AUDIO_LANG_ID_ENABLED=True → AudioLanguageID.detect():
           - Возвращает ISO 639-1 код → используем его.
           - Возвращает None (ошибка/тишина/mlx_whisper недоступен) → placeholder.
        5. STT_AUDIO_LANG_ID_ENABLED=False → placeholder "ru".
        """
        if hint_language is not None:
            return hint_language.strip().lower()

        if audio_data is None:
            logger.debug("STTRouter: no audio_data and no hint → fallback 'und'")
            return "und"

        # Минимальная длина аудио для LID
        min_frames = int(sample_rate * _AUDIO_LID_MIN_SEC)
        if len(audio_data) < min_frames:
            logger.debug(
                "STTRouter: audio too short (%d frames < %d) → placeholder 'ru'",
                len(audio_data),
                min_frames,
            )
            return "ru"

        # Пробуем audio-level LID если включён в настройках
        lang_id_enabled = getattr(self._settings, "STT_AUDIO_LANG_ID_ENABLED", True)
        if lang_id_enabled:
            detected = self._try_audio_lid(audio_data, sample_rate)
            if detected is not None:
                logger.debug("STTRouter: audio LID detected → %s", detected)
                return detected
            # LID вернул None → fallback на placeholder
            logger.debug(
                "STTRouter: audio LID returned None → placeholder 'ru'"
            )
            return "ru"

        # LID отключён → placeholder
        try:
            sample_frames = min(
                len(audio_data), _AUDIO_SAMPLE_SECONDS * sample_rate
            )
            audio_snippet = audio_data[:sample_frames]
            rms = float(np.sqrt(np.mean(audio_snippet ** 2))) if len(audio_snippet) > 0 else 0.0
            if rms < 1e-6:
                logger.debug(
                    "STTRouter: near-silence audio (rms=%.2e), using 'und'", rms
                )
                return "und"
            logger.debug(
                "STTRouter: LID disabled, placeholder → 'ru' (rms=%.4f)", rms
            )
            return "ru"
        except Exception as exc:
            logger.warning("STTRouter: language detection failed: %s", exc)
            return "und"

    def _try_audio_lid(
        self,
        audio_data: np.ndarray,
        sample_rate: int,
    ) -> Optional[str]:
        """Запускает AudioLanguageID.detect(). Возвращает язык или None при ошибке."""
        try:
            lang_id = self._get_lang_id()
            if lang_id is None:
                return None
            return lang_id.detect(audio_data, sample_rate=sample_rate)
        except Exception as exc:
            logger.warning("STTRouter._try_audio_lid: %s", exc)
            return None

    def _lang_to_model(self, lang: str) -> str:
        """Маппинг ISO 639-1 кода языка → идентификатор модели из конфига.

        Неизвестные языки → STT_OTHER_PRIMARY_MODEL.
        """
        attr = _LANG_TO_CONFIG_ATTR.get(lang)
        if attr is not None:
            return getattr(
                self._settings,
                attr,
                "mlx-community/whisper-large-v3-mlx",
            )
        # Неизвестный язык → generalist fallback
        return getattr(
            self._settings,
            "STT_OTHER_PRIMARY_MODEL",
            "mlx-community/whisper-large-v3-mlx",
        )
