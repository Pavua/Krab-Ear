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

Scored selection (D.2.3):
    select_adapter_scored(language, audio_duration_s, adapters) → best adapter

    Scoring per adapter:
      - Match score:  exact language support = 100, multilingual fallback = 60, no support = 0
      - Speed bonus:  gigaam/parakeet = +20, sensevoice = +10, whisper = +0
      - Quality bonus: whisper-mlx = +15, gigaam = +10, parakeet = +10
      - Duration penalty: gigaam AND duration > 30s → -50
                          (longform path via AudioChunker is slower than whisper)

    Controlled by `stt_routing` setting:
      - "auto_scored"  → use scored selection (default)
      - "legacy"       → preserve previous behaviour (adapter order from engine)

Добавление новой модели (когда research завершится):
    1. Создай адаптер в core/pipeline/stt_<name>.py (по образцу stt_whisper.py).
    2. Зарегистрируй адаптер в adapter_factory.
    3. Измени STT_RU_PRIMARY_MODEL default в core/config.py на ID новой модели.
    4. Включи STT_LANGUAGE_ROUTING_ENABLED=True.
    5. Интегрируй self._router в AudioEngine.transcribe() (см. заглушку там).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

import numpy as np

from core.gigaam_compat import GIGAAM_SHORTFORM_MAX_SEC

logger = logging.getLogger("KrabEar.STTRouter")

# ---------------------------------------------------------------------------
# D.2.3 — Scored adapter selection
# ---------------------------------------------------------------------------


@runtime_checkable
class STTAdapterBase(Protocol):
    """Минимальный протокол STT-адаптера для scored selection.

    Адаптер обязан иметь:
      - name: str  — идентификатор (например «gigaam», «parakeet», «whisper-mlx»)
      - supported_languages: set[str]  — ISO 639-1 коды; пустое множество = multilingual
      - is_available(): bool  — True если адаптер может быть запущен прямо сейчас
    """

    @property
    def name(self) -> str: ...

    @property
    def supported_languages(self) -> "set[str]": ...

    def is_available(self) -> bool: ...


# Языки, гарантированно поддерживаемые Whisper-MLX (multilingual ≥ 99 языков).
# Используется как маркер «multilingual fallback» в score function.
_WHISPER_MLX_MARKER = "whisper"
_GIGAAM_ADAPTER_NAME = "gigaam"
_PARAKEET_ADAPTER_NAME = "parakeet"
_SENSEVOICE_ADAPTER_NAME = "sensevoice"
_SHERPA_ADAPTER_NAME = "sherpa"

# Адаптеры, получающие speed bonus
_SPEED_BONUS_FAST: Dict[str, int] = {
    _GIGAAM_ADAPTER_NAME: 20,
    _PARAKEET_ADAPTER_NAME: 20,
    _SHERPA_ADAPTER_NAME: 25,
    _SENSEVOICE_ADAPTER_NAME: 10,
}

# Адаптеры, получающие quality bonus (per benchmark data)
_QUALITY_BONUS: Dict[str, int] = {
    _WHISPER_MLX_MARKER: 15,    # matches any adapter whose name contains "whisper"
    _GIGAAM_ADAPTER_NAME: 10,
    _PARAKEET_ADAPTER_NAME: 10,
}

# Штраф начинается сразу после точного upstream shortform limit.
_GIGAAM_DURATION_PENALTY_THRESHOLD_S = GIGAAM_SHORTFORM_MAX_SEC
_GIGAAM_DURATION_PENALTY = -50


def _adapter_speed_bonus(name: str) -> int:
    """Возвращает speed bonus для адаптера по его имени."""
    name_lower = name.lower()
    for key, bonus in _SPEED_BONUS_FAST.items():
        if key in name_lower:
            return bonus
    return 0


def _adapter_quality_bonus(name: str) -> int:
    """Возвращает quality bonus для адаптера по его имени."""
    name_lower = name.lower()
    # whisper-mlx bonus: любое имя содержащее "whisper"
    if "whisper" in name_lower:
        return _QUALITY_BONUS[_WHISPER_MLX_MARKER]
    for key, bonus in _QUALITY_BONUS.items():
        if key in name_lower and key != _WHISPER_MLX_MARKER:
            if bonus > 0:
                return bonus
    return 0


def score_adapter(
    adapter: Any,
    language: str,
    audio_duration_s: Optional[float] = None,
) -> int:
    """Вычисляет score для одного адаптера.

    Scoring rules:
        Match:   exact language support = 100
                 multilingual (no supported_languages restriction) = 60
                 language not supported = 0
        Speed:   sherpa = +25, gigaam/parakeet = +20, sensevoice = +10, other = +0
        Quality: whisper-mlx = +15, gigaam = +10, parakeet = +10
        Penalty: gigaam AND audio_duration_s > 30 → -50

    Args:
        adapter: объект с атрибутами .name (str) и .supported_languages (set[str])
                 и методом .is_available() -> bool.
        language: ISO 639-1 код языка (уже нормализованный, lowercase).
        audio_duration_s: длительность аудио в секундах или None.

    Returns:
        Целочисленный score (может быть отрицательным после штрафа).
        Адаптер с score 0 по «match» не выбирается (no language support).
    """
    name: str = getattr(adapter, "name", "")
    supported: "set[str]" = getattr(adapter, "supported_languages", set())

    # --- Match score ---
    if len(supported) == 0:
        # Multilingual: поддерживает всё
        match_score = 60
    elif language in supported:
        match_score = 100
    else:
        # Язык явно не поддерживается → не выбираем
        return 0

    # --- Speed bonus ---
    speed = _adapter_speed_bonus(name)

    # --- Quality bonus ---
    quality = _adapter_quality_bonus(name)

    # --- Duration penalty (GigaAM hard limit) ---
    name_lower = name.lower()
    duration_penalty = 0
    if _GIGAAM_ADAPTER_NAME in name_lower:
        if audio_duration_s is not None and audio_duration_s > _GIGAAM_DURATION_PENALTY_THRESHOLD_S:
            duration_penalty = _GIGAAM_DURATION_PENALTY

    total = match_score + speed + quality + duration_penalty
    return total


def score_adapters(
    adapters: List[Any],
    language: str,
    audio_duration_s: Optional[float] = None,
) -> Dict[str, int]:
    """Возвращает словарь {adapter.name: score} для всех адаптеров.

    Недоступные адаптеры (is_available() → False) получают score 0.
    """
    scores: Dict[str, int] = {}
    for adapter in adapters:
        name = getattr(adapter, "name", repr(adapter))
        try:
            available = adapter.is_available() if hasattr(adapter, "is_available") else True
        except Exception:
            available = False
        if not available:
            scores[name] = 0
            continue
        scores[name] = score_adapter(adapter, language, audio_duration_s)
    return scores


def select_adapter_scored(
    language: str,
    audio_duration_s: Optional[float],
    adapters: List[Any],
) -> Optional[Any]:
    """Выбирает лучший STT-адаптер по score function.

    Backward-compat wrapper: сохраняем порядок — при равном score первый в списке.

    Args:
        language: ISO 639-1 код языка (lowercase), например «ru», «en», «zh».
        audio_duration_s: длительность аудио в секундах или None.
        adapters: список адаптеров с атрибутами .name, .supported_languages, .is_available().

    Returns:
        Адаптер с наибольшим score или None если список пуст / все score = 0.
    """
    if not adapters:
        return None

    lang = language.strip().lower() if language else "und"
    scores = score_adapters(adapters, lang, audio_duration_s)

    best_adapter = None
    best_score = 0
    for adapter in adapters:
        name = getattr(adapter, "name", repr(adapter))
        s = scores.get(name, 0)
        if s > best_score:
            best_score = s
            best_adapter = adapter

    if best_adapter is None:
        logger.debug(
            "select_adapter_scored: все score=0 для lang=%s dur=%.1f — нет подходящего адаптера",
            lang,
            audio_duration_s or 0.0,
        )
    else:
        logger.info(
            "select_adapter_scored: lang=%s dur=%s → %s (score=%d)",
            lang,
            f"{audio_duration_s:.1f}s" if audio_duration_s is not None else "None",
            getattr(best_adapter, "name", "?"),
            best_score,
        )

    return best_adapter


# ---------------------------------------------------------------------------
# Идентификатор GigaAM в fallback chain
# ---------------------------------------------------------------------------

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
        # GigaAM adapter cache — single instance reused между transcribe calls.
        # Раньше get_gigaam_adapter() создавал new instance каждый раз —
        # subprocess spawn + model load на каждый chunk. Now cache + warm-up
        # support через `warmup_gigaam()`.
        self._gigaam_adapter: Optional[Any] = None
        # Fingerprint не даёт hot reload вернуть адаптер со старой моделью,
        # устройством, транспортом или уже изменившимся Python-окружением.
        self._gigaam_adapter_fingerprint: Optional[
            tuple[Any, Any, str, Optional[str]]
        ] = None
        # UI-настройки и транскрипция могут запросить адаптер одновременно.
        # Один lock исключает двойной subprocess и гонку close/create.
        self._gigaam_adapter_lock = threading.RLock()

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

        # 5. Sentry tag — позволяет фильтровать crash-отчёты по активному STT движку.
        #    Lazy import: sentry_sdk опциональная зависимость; no-op если не инициализирован.
        try:
            import sentry_sdk  # noqa: PLC0415
            sentry_sdk.set_tag("stt_engine", model_id)
        except Exception:  # noqa: BLE001
            pass  # telemetry никогда не должна ломать routing

        return model_id

    def get_gigaam_adapter(self) -> Optional[Any]:
        """Возвращает инициализированный GigaAMAdapter если STT_GIGAAM_ENABLED=True.

        Один экземпляр переиспользуется, пока совпадает fingerprint из mode,
        device, transport и проверенного venv-пути. Изменение любого поля
        закрывает старый subprocess и атомарно создаёт адаптер с новым конфигом.
        """
        with self._gigaam_adapter_lock:
            return self._get_gigaam_adapter_locked()

    def _get_gigaam_adapter_locked(self) -> Optional[Any]:
        """Реализует получение GigaAM-адаптера под удерживаемым lock."""
        if not getattr(self._settings, "STT_GIGAAM_ENABLED", False):
            # Toggle off очищает и объект, и fingerprint, иначе последующее
            # включение может ошибочно принять новый конфиг за старый.
            self._close_cached_gigaam_adapter()
            logger.debug("STTRouter.get_gigaam_adapter: STT_GIGAAM_ENABLED=False → None")
            return None

        mode = getattr(self._settings, "STT_GIGAAM_MODE", "rnnt")
        device = getattr(self._settings, "STT_GIGAAM_DEVICE", "mps")
        transport_raw = getattr(self._settings, "STT_GIGAAM_TRANSPORT", "auto")
        transport = transport_raw if isinstance(transport_raw, str) else "auto"
        venv_python_raw = getattr(self._settings, "STT_GIGAAM_VENV_PYTHON", "")
        if isinstance(venv_python_raw, str) and venv_python_raw.strip():
            venv_python: Optional[str] = self._validate_gigaam_venv_python(
                venv_python_raw.strip()
            )
            if venv_python is None:
                # Нельзя продолжать со старым адаптером после невалидного hot
                # reload: он больше не соответствует выбранным настройкам.
                self._close_cached_gigaam_adapter()
                return None
        else:
            venv_python = None

        fingerprint = (mode, device, transport, venv_python)
        if (
            self._gigaam_adapter is not None
            and self._gigaam_adapter_fingerprint == fingerprint
        ):
            return self._gigaam_adapter

        if self._gigaam_adapter is not None:
            logger.info(
                "STTRouter.get_gigaam_adapter: конфигурация изменилась — "
                "пересоздаём адаптер"
            )
            self._close_cached_gigaam_adapter()

        if transport == "mlx":
            # MLX-транспорт — другой КЛАСС адаптера (инференс в главном процессе
            # под mlx_lock); диверсия обязана происходить до конструктора
            # GigaAMAdapter: его _VALID_TRANSPORTS не знает "mlx" (ValueError).
            try:
                from core.pipeline.stt_gigaam_mlx import (  # type: ignore[import]
                    GigaAMMLXAdapter,
                )
            except ImportError:
                logger.warning(
                    "STTRouter.get_gigaam_adapter: transport=mlx, но "
                    "core.pipeline.stt_gigaam_mlx не найден"
                )
                return None
            try:
                timeout = float(
                    getattr(self._settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 120.0)
                )
                adapter = GigaAMMLXAdapter(mode=mode, watchdog_timeout_sec=timeout)
                self._gigaam_adapter = adapter
                self._gigaam_adapter_fingerprint = fingerprint
                logger.info(
                    "STTRouter.get_gigaam_adapter: MLX-адаптер создан (mode=%s)",
                    mode,
                )
                return adapter
            except Exception as exc:
                # ValueError(mode) и пр.: GigaAM мягко выключается, каскад
                # продолжает по whisper-кандидатам (симметрия PyTorch-ветки).
                logger.warning(
                    "STTRouter.get_gigaam_adapter: ошибка создания MLX-адаптера: %s",
                    exc,
                )
                return None

        try:
            from core.pipeline.stt_gigaam import GigaAMAdapter  # type: ignore[import]
        except ImportError:
            logger.warning(
                "STTRouter.get_gigaam_adapter: core.pipeline.stt_gigaam не найден"
            )
            return None

        try:
            adapter = GigaAMAdapter(
                device=device,
                mode=mode,
                transport=transport,
                venv_python_path=venv_python,
            )
            self._gigaam_adapter = adapter
            self._gigaam_adapter_fingerprint = fingerprint
            logger.info(
                "STTRouter.get_gigaam_adapter: адаптер создан (mode=%s, device=%s, transport=%s)",
                mode,
                device,
                transport,
            )
            return adapter
        except Exception as exc:
            logger.warning("STTRouter.get_gigaam_adapter: ошибка создания адаптера: %s", exc)
            return None

    def _close_cached_gigaam_adapter(self) -> None:
        """Закрывает кэшированный адаптер и безусловно сбрасывает fingerprint."""
        adapter = self._gigaam_adapter
        # Сначала очищаем ссылки: даже ошибка close не должна оставлять в кэше
        # уже недействительный адаптер или его конфигурацию.
        self._gigaam_adapter = None
        self._gigaam_adapter_fingerprint = None
        if adapter is None:
            return
        try:
            adapter.close()
        except Exception as exc:
            logger.warning(
                "STTRouter.get_gigaam_adapter: ошибка закрытия старого адаптера: %s",
                exc,
            )

    def warmup_gigaam(self) -> bool:
        """Force-load GigaAM model в background чтобы избежать cold-start latency.

        Called at backend startup (если settings.STT_GIGAAM_ENABLED=True) —
        spawns subprocess + loads model в фоне, к моменту первой диктовки всё
        готово.

        Returns: True если warmup triggered (или already done), False если
        GigaAM disabled / unavailable.
        """
        adapter = self.get_gigaam_adapter()
        if adapter is None:
            return False
        # Pre-load: small dummy audio (1s silence) → forces subprocess spawn +
        # model load. Это same path как transcribe но на dev silence.
        import numpy as np
        try:
            dummy = np.zeros(16000, dtype=np.float32)  # 1s silence at 16kHz
            adapter.transcribe(dummy, sample_rate=16000)
            logger.info("STTRouter.warmup_gigaam: модель готова (subprocess loaded)")
            return True
        except Exception as exc:
            logger.warning("STTRouter.warmup_gigaam: ошибка warmup: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_gigaam_venv_python(path: str) -> Optional[str]:
        """Validate STT_GIGAAM_VENV_PYTHON before passing to subprocess.Popen.

        Prevents arbitrary binary execution by an attacker who can write
        to settings (e.g. via a rogue IPC client or a compromised settings.json).

        Rules:
          1. The *lexical* (absolute, non-symlink-followed) path must be inside
             the user's home directory.  We use Path.absolute() rather than
             Path.resolve() because venv Python binaries are often symlinks into
             Homebrew Cellar (/opt/homebrew/…) — resolving them would make every
             venv python path appear to be "outside home".  The lexical path is
             what the user typed (or what was stored in settings.json) and is the
             appropriate scope to restrict.
          2. The final filename (basename) must be a known Python interpreter name:
             python, python3, python3.10, python3.11, python3.12.

        Returns the lexical absolute path as a string, or None if validation
        fails (caller logs a warning and returns None from get_gigaam_adapter()).
        """
        from pathlib import Path  # noqa: PLC0415 (local import for narrow scope)

        _VALID_BASENAMES = frozenset({
            "python",
            "python3",
            "python3.10",
            "python3.11",
            "python3.12",
        })

        try:
            # Use absolute() (no symlink resolution) to keep venv paths inside home.
            venv_python = Path(path).absolute()
            home = Path.home().absolute()

            if not venv_python.is_relative_to(home):
                logger.warning(
                    "STTRouter: STT_GIGAAM_VENV_PYTHON %r is outside home directory"
                    " — ignoring to prevent arbitrary binary execution",
                    path,
                )
                return None

            if venv_python.name not in _VALID_BASENAMES:
                logger.warning(
                    "STTRouter: STT_GIGAAM_VENV_PYTHON basename %r is not a"
                    " recognised Python interpreter — ignoring"
                    " (allowed: %s)",
                    venv_python.name,
                    ", ".join(sorted(_VALID_BASENAMES)),
                )
                return None

        except Exception as exc:  # noqa: BLE001 (path resolution can raise on weird input)
            logger.warning(
                "STTRouter: STT_GIGAAM_VENV_PYTHON validation error for %r: %s"
                " — ignoring",
                path,
                exc,
            )
            return None

        return str(venv_python)

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
