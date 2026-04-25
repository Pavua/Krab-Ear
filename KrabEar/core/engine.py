"""Ядро Krab Ear: локальная транскрибация и интеграция с внешними сервисами.

AudioEngine управляет жизненным циклом STT-моделей (через mlx-whisper), нормализацией аудио
и взаимодействием со шлюзами (LLM/Remote STT). Для файловых импортов движок
может дополнять результат diarization спикеров через pyannote.audio.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re as _re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable, Optional, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter

import numpy as np
import requests

# Heavy optional dependencies — недоступны на Linux CI (mlx only Apple Silicon)
# и/или требуют system libs (soundfile→libsndfile, pyannote→ffmpeg via torchcodec).
# Оборачиваем в try/except Exception чтобы test discovery проходил на Ubuntu:
# - ImportError — module not installed
# - OSError — native library missing (libavutil.so.60 от torchcodec на Ubuntu)
# - Вообще любое исключение при module-level init
try:
    import mlx_whisper  # type: ignore
except Exception:
    mlx_whisper = None  # type: ignore[assignment]

from core.mlx_lock import mlx_lock  # noqa: E402 — после try/except блока MLX импорта

try:
    import soundfile as sf  # type: ignore
except Exception:
    sf = None  # type: ignore[assignment]

try:
    import torch  # type: ignore
except Exception:
    torch = None  # type: ignore[assignment]

try:
    from pyannote.audio import Pipeline  # type: ignore
except Exception:
    Pipeline = None  # type: ignore[assignment,misc]

# SenseVoice (FunASR) — опциональный альтернативный STT движок (Alibaba).
# Поддерживает 50+ языков (вкл. RU) и эмоцию. Lazy: если funasr не установлен,
# адаптер возвращает ошибку, и fallback chain продолжит работу на whisper'е.
try:
    from funasr import AutoModel as _SenseVoiceAutoModel  # type: ignore
except Exception:
    _SenseVoiceAutoModel = None  # type: ignore[assignment]

# Parakeet-TDT-1.1B (NVIDIA NeMo) — опциональный EN-оптимизированный STT движок.
# Топ OpenASR leaderboard. Требует `pip install nemo-toolkit[asr]`.
# На Apple Silicon работает через PyTorch MPS или CPU (CUDA не обязателен).
# Если nemo не установлен — адаптер мягко возвращает ошибку и chain продолжается.
try:
    import nemo.collections.asr as _nemo_asr  # type: ignore
except Exception:
    _nemo_asr = None  # type: ignore[assignment]

# WhisperX (m-bain/whisperX) — community wrapper над Whisper с word-level timestamps
# и native pyannote diarization. Opt-in через WHISPERX_ENABLED.
# Lazy: если whisperx не установлен — fallback chain продолжается на whisper'е.
try:
    import whisperx as _whisperx  # type: ignore
except Exception:
    _whisperx = None  # type: ignore[assignment]

# Voxtral Mini 4B Realtime (Mistral) — STT + семантический reasoning (Phase 4.4).
# Поддерживает 13 языков (вкл. RU/ES/EN). MLX 4-bit quant ~2–3 GB.
# Библиотека: mistral-inference (pip install mistral-inference).
# Lazy: если библиотека не установлена — fallback chain продолжается на whisper'е.
try:
    from mistral_inference.transformer import Transformer as _VoxtralTransformer  # type: ignore
    from mistral_inference.generate import generate as _voxtral_generate  # type: ignore
    from mistral_common.tokens.tokenizers.mistral import MistralTokenizer as _VoxtralTokenizer  # type: ignore
    from mistral_common.audio import AudioChunk as _VoxtralAudioChunk  # type: ignore
    from mistral_common.protocol.instruct.messages import UserMessage as _VoxtralUserMessage  # type: ignore
    from mistral_common.protocol.instruct.request import ChatCompletionRequest as _VoxtralChatRequest  # type: ignore
    _voxtral_available = True
except Exception:
    _VoxtralTransformer = None  # type: ignore[assignment,misc]
    _voxtral_generate = None  # type: ignore[assignment]
    _VoxtralTokenizer = None  # type: ignore[assignment,misc]
    _VoxtralAudioChunk = None  # type: ignore[assignment,misc]
    _VoxtralUserMessage = None  # type: ignore[assignment,misc]
    _VoxtralChatRequest = None  # type: ignore[assignment,misc]
    _voxtral_available = False

from .config import settings
from .confidence_calibrator import ConfidenceCalibrator
from .text_diff import TextDiffAnalyzer
from .utils import TextUtils

# Profiler — module-level singleton (thread-safe, sliding window).
# Импорт лениво-совместим: при отсутствии numpy на ранней стадии init'а
# или любой другой ошибке ImportError — fallback на no-op, чтобы не ломать STT path.
try:
    from backend.performance_profiler import profiler as _profiler
except Exception:  # pragma: no cover — defensive
    class _NoOpSpan:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _NoOpProfiler:
        def start_span(self, name: str):
            return _NoOpSpan()

    _profiler = _NoOpProfiler()  # type: ignore[assignment]


def _short_model_name(model: str) -> str:
    """Возвращает короткое имя модели для span-name (безопасное для ключей)."""
    if not model:
        return "unknown"
    return str(model).rsplit("/", 1)[-1]


logger = logging.getLogger("KrabEar.Engine")


# ---------------------------------------------------------------------------
# Утилита: поиск ffmpeg в PATH (portable на Intel/Apple Silicon/нестандартные установки)
# ---------------------------------------------------------------------------


def _find_ffmpeg_path() -> str:
    """Находит путь к ffmpeg через PATH или fallback на системные пути.

    Порядок поиска:
    1. shutil.which("ffmpeg") — поиск в PATH (портируемо)
    2. /opt/homebrew/bin/ffmpeg — Homebrew на Apple Silicon
    3. /usr/local/bin/ffmpeg — Homebrew на Intel или other installs

    Если ffmpeg не найден, вернёт fallback path (может быть недоступен в runtime).
    """
    # Сначала проверяем PATH — самый портируемый способ
    which_result = shutil.which("ffmpeg")
    if which_result:
        return which_result

    # Fallback на известные Homebrew пути
    for candidate in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # Если ничего не нашли, возвращаем логичный default
    # (мог быть установлен в PATH или система его найдёт позже)
    logger.warning("ffmpeg не найден в PATH или стандартных путях Homebrew; используем fallback")
    return "ffmpeg"


_FFMPEG_PATH = _find_ffmpeg_path()

# ---------------------------------------------------------------------------
# Константы для магических чисел
# ---------------------------------------------------------------------------

# Конвертация размеров файлов: байты в МБ (повторяется в разных местах).
_BYTES_PER_MB = 1024 * 1024

# Voxtral Mini 4B (Mistral) — максимум токенов генерации для STT + reasoning.
# Достаточно для ~30 секунд аудио (~300 токенов STT) + краткого резюме.
_VOXTRAL_MAX_TOKENS = 2048

# ---------------------------------------------------------------------------
# Утилита: проверка доступной памяти macOS через vm_stat
# ---------------------------------------------------------------------------

# Минимум свободной (free + inactive) памяти для загрузки тяжёлых моделей.
# whisper-large-v3-mlx занимает ~3GB, pyannote ~1.5GB. Оставляем запас.
_HEAVY_MODEL_MIN_FREE_GB = 4.0


def _get_available_memory_gb() -> float:
    """Возвращает примерный объём доступной памяти (free + inactive) в GB.

    Использует macOS `vm_stat` — это дешёвый вызов без зависимостей.
    При ошибке возвращает -1 (не блокируем работу если не macOS).
    """
    try:
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return -1.0
        page_size = os.sysconf("SC_PAGE_SIZE")
        free_pages = 0
        inactive_pages = 0
        for line in result.stdout.splitlines():
            if "Pages free" in line:
                m = _re.search(r"(\d+)", line.split(":")[1])
                if m:
                    free_pages = int(m.group(1))
            elif "Pages inactive" in line:
                m = _re.search(r"(\d+)", line.split(":")[1])
                if m:
                    inactive_pages = int(m.group(1))
        return (free_pages + inactive_pages) * page_size / (1024 ** 3)
    except Exception:
        return -1.0


# ---------------------------------------------------------------------------
# Утилиты: iCloud workaround — копирование в /tmp перед ffmpeg
# ---------------------------------------------------------------------------

# Паттерны путей, которые macOS iCloud Drive может «заморозить» без
# NSFileCoordinator. При попытке read() или ffmpeg-pipe возникает
# errno 11 (EDEADLK «Resource deadlock avoided») на macOS.
_ICLOUD_PATH_MARKERS = (
    "Mobile Documents",
    "com~apple~CloudDocs",
    "iCloud~",
    "CloudDocs",
)

# errno 11 = EDEADLK ("Resource deadlock avoided") на macOS — типично для
# iCloud-заглушек (placeholder), к которым обращаются без NSFileCoordinator.
_ICLOUD_ERRNO = 11


def _is_icloud_path(path: str) -> bool:
    """Возвращает True если путь выглядит как iCloud Drive расположение."""
    return any(marker in path for marker in _ICLOUD_PATH_MARKERS)


def _needs_icloud_copy(path: str) -> bool:
    """Пробует открыть файл; возвращает True если получаем errno 11 (EDEADLK).

    Это ловит iCloud-placeholder'ы вне стандартных путей Mobile Documents —
    например, в ~/Downloads или ~/Desktop после частичной синхронизации.
    """
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return False
    except OSError as exc:
        return exc.errno == _ICLOUD_ERRNO


def _copy_to_tmp_with_icloud_download(path: str) -> Optional[str]:
    """Пытается скопировать iCloud-файл во временный путь в /tmp.

    Шаги:
    1. Если файл — незагруженный placeholder (размер 0 или errno 11 при чтении),
       вызывает ``brctl download`` чтобы macOS инициировал загрузку, затем
       ждёт до 30 секунд пока файл станет доступен.
    2. Копирует в /tmp и возвращает путь к копии.
    При ошибке возвращает None (вызывающий продолжит с оригинальным путём
    и получит осмысленную ошибку позже).
    """
    _log = logging.getLogger("KrabEar.Engine")
    try:
        # Шаг 1: проверяем, нужна ли загрузка (placeholder = 0 байт)
        file_size = os.path.getsize(path)
        if file_size == 0:
            _log.info("iCloud placeholder обнаружен, запрашиваем загрузку: %s", path)
            try:
                subprocess.run(
                    ["brctl", "download", path],
                    timeout=5,
                    capture_output=True,
                )
            except Exception:
                pass  # brctl может отсутствовать; продолжаем

            # Ждём пока файл станет ненулевым (macOS скачивает в фоне)
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                try:
                    if os.path.getsize(path) > 0:
                        break
                except OSError:
                    pass
                time.sleep(0.5)

        # Шаг 2: копируем в /tmp
        suffix = Path(path).suffix
        with tempfile.NamedTemporaryFile(
            prefix="krab_ear_import_", suffix=suffix, delete=False
        ) as tmp:
            tmp_path = tmp.name
        shutil.copy2(path, tmp_path)
        return tmp_path
    except Exception as exc:
        _log.warning("Не удалось скопировать iCloud файл %s: %s", path, exc)
        return None


class AudioEngine:
    """Сервисный слой для STT ( Speech-to-Text) и TTS (Text-to-Speech)."""

    DOMAIN_PROMPTS = {
        "casual": "Разговорная речь, сленг, обычный стиль.",
        "finance": "Финансовая терминология, числа, валюты, банки, котировки.",
        "code": "Программирование, названия функций, переменные, технический английский. Python, Javascript, API.",
        "meeting": "Протокольная запись встречи, четкая структура, официальный тон.",
        "legal": "Юридические термины, законы, кодексы, официальные документы."
    }

    def __init__(
        self,
        llm_rewriter: Optional["LLMRewriter"] = None,
        settings_get: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        """Инициализирует двигатель, загружая настройки из централизованного конфига.

        Args:
            llm_rewriter: опциональный D.10a LLM клиент для post-cleanup rewrite'а.
                          Если None — LLM hook отключён, работает как до D.10a.
            settings_get: callback (key, default) -> value для runtime toggle'ов.
                          Инжектируется из BackendService чтобы engine не знал про StateStore.
        """
        self.current_model = settings.MODEL_BALANCED
        self.quality_profile = "balanced"
        self._unavailable_models: set[str] = set()
        self._diarization_pipeline: Pipeline | None = None
        self._diarization_load_error: str | None = None

        # SenseVoice adapter state (lazy-loaded FunASR pipeline).
        # Если funasr не установлен или модель не грузится — адаптер навсегда
        # отключается через _sensevoice_load_error, whisper chain продолжает жить.
        self._sensevoice_model = None  # type: ignore[var-annotated]
        self._sensevoice_load_error: str | None = None

        # Parakeet-TDT-1.1B adapter state (lazy-loaded NeMo ASR model).
        # Если nemo не установлен или модель не грузится — адаптер навсегда
        # отключается через _parakeet_load_error, whisper chain продолжает жить.
        self._parakeet_model = None  # type: ignore[var-annotated]
        self._parakeet_load_error: str | None = None

        # WhisperX adapter state (Phase 4.3, lazy-loaded).
        # Если whisperx не установлен или модель не грузится — адаптер навсегда
        # отключается через _whisperx_load_error, chain продолжает жить.
        self._whisperx_model = None  # type: ignore[var-annotated]
        self._whisperx_load_error: str | None = None

        # D.10a: LLM rewriter integration
        self._llm_rewriter = llm_rewriter
        self._last_llm_diff = None
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._confidence_calibrator = ConfidenceCalibrator()

        logger.info(
            "AudioEngine инициализирован. Профиль=%s, Модель=%s, Max Candidates=%d, LLM=%s",
            self.quality_profile,
            self.current_model,
            len(settings.model_max_list),
            "enabled" if llm_rewriter is not None else "disabled",
        )

    def _llm_rewrite_allowed(self) -> bool:
        """Runtime check: включён ли LLM rewriter И user runtime toggle."""
        if self._llm_rewriter is None:
            return False
        return bool(self._settings_get("llm_rewrite_enabled", False))

    def set_quality_profile(self, profile: str) -> bool:
        """Переключает профиль качества (balanced или max)."""
        clean_profile = profile.strip().lower()
        if clean_profile not in {"balanced", "max"}:
            clean_profile = "balanced"

        new_model = settings.MODEL_BALANCED if clean_profile == "balanced" else settings.model_max_list[0]

        if clean_profile == self.quality_profile and new_model == self.current_model:
            return False

        logger.info("Смена профиля STT: %s -> %s (модель: %s)", self.quality_profile, clean_profile, new_model)
        self.quality_profile = clean_profile
        self.current_model = new_model
        return True

    def normalize_audio(self, audio_path: str) -> bool | str:
        """Нормализует громкость аудиофайла до целевого уровня (примерно -20 dBFS).

        Совместимость:
        - для отсутствующего файла возвращает исходный путь (legacy-контракт тестов),
        - для успешной нормализации возвращает True.
        """
        if not os.path.exists(audio_path):
            logger.error("Файл не найден для нормализации: %s", audio_path)
            return audio_path
        try:
            data, samplerate = sf.read(audio_path)
            if len(data.shape) > 1:
                data = data.mean(axis=1)  # Стерео в моно

            rms = np.sqrt(np.mean(data**2))
            if rms < 1e-6:
                return True  # Тишина

            gain = 0.1 / rms
            normalized_data = np.clip(data * gain, -1.0, 1.0)
            sf.write(audio_path, normalized_data, samplerate)
            return True
        except Exception as e:
            logger.error("Ошибка при нормализации аудио %s: %s", audio_path, e)
            return False

    # --- Legacy-совместимость для тестов и старых вызовов ---
    @staticmethod
    def _normalize_phrase(text: str) -> str:
        """Совместимый алиас нормализации фраз."""
        return TextUtils.normalize_phrase(text)

    @staticmethod
    def _same_short_phrase(a: str, b: str, max_words: int = 8) -> bool:
        """Совместимый алиас сравнения коротких фраз."""
        return TextUtils.same_short_phrase(a, b, max_words=max_words)

    @staticmethod
    def _cleanup_soft(text: str) -> str:
        """Совместимый алиас мягкой очистки хвостов.

        В legacy-тестах ожидается, что при снятии дублированного хвоста
        у фразы сохранится финальная точка.
        """
        cleaned = TextUtils._cleanup_soft(text)
        if cleaned and not cleaned.endswith((".", "!", "?", "…")) and text.strip().endswith((".", "!", "?", "…")):
            return f"{cleaned}."
        return cleaned

    @staticmethod
    def _cleanup_transcript(text: str, cleanup_profile: str = "soft") -> str:
        """Совместимый алиас общей очистки транскрипта."""
        return TextUtils.cleanup_transcript(text, profile=cleanup_profile)

    # Допустимые языковые коды для lang_hint (ISO 639-1).
    # None означает автоопределение whisper'ом по первым 30с аудио.
    _VALID_LANG_HINTS: frozenset[str] = frozenset({"ru", "es", "en", "auto"})

    @staticmethod
    def _resolve_language(lang_hint: str | None) -> str | None:
        """Преобразует lang_hint в параметр whisper language.

        - None / "auto" → None (whisper сам определяет язык)
        - "ru" / "es" / "en" → передаётся напрямую
        - неизвестное значение → None с предупреждением
        """
        if lang_hint is None or lang_hint.strip().lower() in ("auto", ""):
            return None
        clean = lang_hint.strip().lower()
        if clean in AudioEngine._VALID_LANG_HINTS - {"auto"}:
            return clean
        logger.warning("Неизвестный lang_hint=%r, используем авто-определение", lang_hint)
        return None

    def _maybe_denoise(self, audio: np.ndarray) -> np.ndarray:
        """Проверяет SNR и применяет шумоподавление при необходимости.

        Использует NoiseProfiler для оценки SNR. Если SNR < порога из настроек →
        запускает AudioDenoiser с заданной силой. Возвращает (возможно обработанный)
        аудиомассив той же dtype и формы.

        Исключения внутри не должны ломать транскрибацию — ловим и логируем.
        """
        try:
            from core.noise_profiler import NoiseProfiler
            from core.audio_denoiser import AudioDenoiser

            sample_rate = 16000  # mlx-whisper ожидает 16 кГц
            profile = NoiseProfiler().profile(audio, sample_rate)
            snr = profile.snr_db
            threshold = settings.STT_DENOISE_SNR_THRESHOLD_DB
            strength = settings.STT_DENOISE_STRENGTH

            if snr < threshold:
                logger.info(
                    "[STT] noise SNR=%.1f dB < %.1f dB → denoising applied (strength=%s)",
                    snr, threshold, strength,
                )
                return AudioDenoiser().denoise(audio, sample_rate, strength=strength)  # type: ignore[arg-type]
            else:
                logger.debug(
                    "[STT] noise SNR=%.1f dB ≥ %.1f dB → denoising skipped",
                    snr, threshold,
                )
        except Exception as exc:
            logger.warning("[STT] denoising error, skipping: %s", exc)
        return audio

    def transcribe(
        self,
        audio_data: Any,
        cleanup_profile: str = "soft",
        is_preview: bool = False,
        domain: str = "casual",
        extra_vocabulary: list[str] | None = None,
        lang_hint: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Основной метод распознавания речи. Поддерживает динамические промпты и доменные подсказки.

        Args:
            lang_hint: ISO 639-1 код языка ("ru", "es", "en") или None/"auto" для
                       автоопределения whisper'ом. По умолчанию берётся из конфига (settings.TRANSCRIBE_LANGUAGE).
            progress_callback: Опциональный колбэк для отчёта о прогрессе. Вызывается с именем
                       этапа ("audio_load", "normalize", "stt", "cleanup", "diarize", "llm_rewrite").
                       Исключения внутри колбэка подавляются — отчёт о прогрессе не должен ломать
                       транскрибацию.
        """
        def _report(stage: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(stage)
                except Exception:
                    pass

        start_time = time.time()
        resolved_lang = self._resolve_language(lang_hint) if lang_hint is not None else settings.TRANSCRIBE_LANGUAGE

        # 1. Формирование динамического промпта.
        # Preview path идёт с пустым prompt'ом: короткие аудиобуферы (<3s)
        # провоцируют whisper на "leakage" initial_prompt'а в output как
        # артефакта. Финальный stop_recording по-прежнему использует полный
        # TRANSCRIBE_PROMPT для пунктуации/брендов/имён. Defense-in-depth:
        # _postprocess_preview_text в service.py срезает известные фрагменты
        # промпта как safety net.
        if is_preview:
            dynamic_prompt = ""
        else:
            domain_desc = self.DOMAIN_PROMPTS.get(domain, self.DOMAIN_PROMPTS["casual"])
            dynamic_prompt = f"{settings.TRANSCRIBE_PROMPT} Тематика: {domain_desc}"
            if extra_vocabulary:
                dynamic_prompt += f" Ключевые слова: {', '.join(extra_vocabulary)}"

        # 2. Проверка лимитов и iCloud workaround для файлов
        _report("audio_load")
        _temp_copy_path: str | None = None
        if isinstance(audio_data, (str, Path)) and os.path.exists(audio_data):
            size_mb = os.path.getsize(audio_data) / _BYTES_PER_MB
            if size_mb > settings.MAX_AUDIO_MB:
                raise ValueError(f"Файл слишком большой: {size_mb:.1f}MB > {settings.MAX_AUDIO_MB}MB")
            # iCloud Drive files may trigger "Resource deadlock avoided" (errno 11 EDEADLK)
            # when ffmpeg tries to read them without NSFileCoordinator.
            # Workaround: try to trigger iCloud download via brctl, then copy to /tmp.
            audio_str = str(audio_data)
            if _is_icloud_path(audio_str) or _needs_icloud_copy(audio_str):
                tmp_path = _copy_to_tmp_with_icloud_download(audio_str)
                if tmp_path:
                    _temp_copy_path = tmp_path
                    audio_data = _temp_copy_path
                    logger.info("iCloud файл скопирован во временный: %s", _temp_copy_path)

        # Auto-select model for file imports based on duration
        _report("normalize")
        if isinstance(audio_data, (str, Path)) and os.path.exists(str(audio_data)) and not is_preview:
            try:
                import soundfile as sf
                info = sf.info(str(audio_data))
                if info.duration < 30:
                    self.set_quality_profile("balanced")
                    logger.info("Auto-select: balanced (short audio %.1fs)", info.duration)
                elif info.duration > 300:
                    self.set_quality_profile("max")
                    logger.info("Auto-select: max (long audio %.1fs)", info.duration)
            except Exception:
                pass  # Fall through to configured profile

        try:
            # 2.5 Адаптивное шумоподавление (применяется только к numpy-массивам,
            #     т.е. к живым записям; файловые импорты пропускаются для скорости).
            if (
                settings.STT_DENOISE_ENABLED
                and not is_preview
                and isinstance(audio_data, np.ndarray)
            ):
                audio_data = self._maybe_denoise(audio_data)

            # 3. Вызов распознавания с механизмом деградации (fallback)
            _report("stt")

            # VAD pre-filter: убираем длинные паузы ДО Whisper → меньше галлюцинаций.
            # Работает только с numpy-массивами (не с file path).
            if settings.STT_VAD_PREFILTER_ENABLED and isinstance(audio_data, np.ndarray):
                vad_result = self._apply_vad_prefilter(audio_data)
                if vad_result is None:
                    # Тишина или слишком мало речи — возвращаем пустой результат
                    return {"text": "", "raw_text": "", "cleaned_text": "",
                            "llm_applied": False, "llm_latency_ms": None,
                            "llm_fallback_reason": None, "llm_diff": None,
                            "confidence": 0.0, "raw_confidence": 0.0,
                            "confidence_adjustments": [], "duration_ms": 0,
                            "engine": "vad_skip", "model": None,
                            "language": resolved_lang, "segments": [],
                            "diarization": None, "emotion": None}
                audio_data = vad_result

            result = self._transcribe_with_fallback(audio_data, prompt=dynamic_prompt, language=resolved_lang)
            raw_text = str(result.get("text", "")).strip()
            segments = result.get("segments", [])
            if not is_preview and settings.DIARIZATION_ENABLED:
                _report("diarize")
            diarization = self._maybe_run_diarization(audio_data, segments, is_preview=is_preview)

            # 4. Очистка результата через утилиты (D.7 normalization)
            _report("cleanup")
            cleaned_text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)
            text = cleaned_text

            # 4.5 D.10a: LLM rewrite hook (только если admin+runtime toggle=true)
            llm_result = None
            llm_diff = None
            if self._llm_rewrite_allowed():
                _report("llm_rewrite")
                llm_result = self._llm_rewriter.rewrite(cleaned_text)
                if llm_result.ok:
                    logger.info(
                        "LLM rewrite: %d chars -> %d chars, %d ms",
                        len(cleaned_text), len(llm_result.text), llm_result.latency_ms,
                    )
                    llm_diff = TextDiffAnalyzer().compute_diff(cleaned_text, llm_result.text)
                    self._last_llm_diff = llm_diff
                    text = llm_result.text
                else:
                    logger.debug(
                        "LLM rewrite fallback: %s (latency=%s ms)",
                        llm_result.fallback_reason,
                        llm_result.latency_ms,
                    )

            # 5. Расчет метрик уверенности
            confidence = 0.0
            if segments:
                confidence = float(np.mean([np.exp(s.get("avg_logprob", -1.0)) for s in segments]))

            duration = time.time() - start_time

            # 5a. Калибровка уверенности
            audio_duration_sec = result.get("audio_duration_sec", duration)
            calibrated_score = self._confidence_calibrator.calibrate_detailed(
                raw_confidence=confidence,
                duration_sec=audio_duration_sec,
                language=result.get("language", resolved_lang) or "",
                model=result.get("model_used", self.current_model) or "",
            )

            logger.info(
                "STT готово: %.2fs, уверенность: raw=%.2f calibrated=%.2f, язык: %s",
                duration,
                confidence,
                calibrated_score.calibrated,
                resolved_lang or "auto",
            )

            return {
                "text": text,
                "raw_text": raw_text,
                "cleaned_text": cleaned_text,
                "llm_applied": bool(llm_result is not None and llm_result.ok),
                "llm_latency_ms": llm_result.latency_ms if llm_result else None,
                "llm_fallback_reason": (
                    llm_result.fallback_reason
                    if (llm_result is not None and not llm_result.ok)
                    else None
                ),
                "llm_diff": (
                    {
                        "similarity_ratio": llm_diff.similarity_ratio,
                        "words_added": llm_diff.words_added,
                        "words_removed": llm_diff.words_removed,
                        "words_unchanged": llm_diff.words_unchanged,
                        "summary": llm_diff.summary,
                    }
                    if llm_diff is not None
                    else None
                ),
                "confidence": round(calibrated_score.calibrated, 3),
                "raw_confidence": round(confidence, 3),
                "confidence_adjustments": calibrated_score.adjustments,
                "duration_ms": int(duration * 1000),
                "engine": result.get("engine", "mlx-whisper"),
                "model": result.get("model_used", self.current_model),
                "language": result.get("language", resolved_lang),
                "segments": segments if not is_preview else [],
                "diarization": diarization,
                # Phase 4: SenseVoice эмоция (happy/neutral/angry/...) — None для whisper.
                # Surfaced только если SENSEVOICE_EMOTION_TO_HISTORY=True и adapter сработал.
                "emotion": (
                    result.get("emotion")
                    if settings.SENSEVOICE_EMOTION_TO_HISTORY
                    else None
                ),
            }
        except Exception as exc:
            logger.exception("Критическая ошибка распознавания")
            return {"text": "", "error": str(exc), "status": "error"}
        finally:
            # Cleanup iCloud temp copy
            if _temp_copy_path:
                try:
                    os.unlink(_temp_copy_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # VAD pre-filter
    # ------------------------------------------------------------------

    _VAD_SAMPLE_RATE: int = 16000   # Whisper всегда работает с 16 kHz
    _VAD_MIN_VOICE_SEC: float = 0.3  # минимум речи для запуска STT
    _VAD_PADDING_SEC: float = 0.5    # padding вокруг каждого речевого сегмента

    def _apply_vad_prefilter(
        self,
        audio: np.ndarray,
        sample_rate: int = _VAD_SAMPLE_RATE,
    ) -> Optional[np.ndarray]:
        """Применяет VAD к аудио-массиву ДО STT.

        Алгоритм:
        1. Обнаруживает речевые сегменты через VoiceActivityDetector.
        2. Если суммарная речь < _VAD_MIN_VOICE_SEC → возвращает None
           (caller должен вернуть пустой результат без вызова STT).
        3. Извлекает речевые регионы с padding _VAD_PADDING_SEC и
           конкатенирует их. Паузы > STT_VAD_SILENCE_TRIM_THRESHOLD_SEC
           заменяются тишиной длиной _VAD_PADDING_SEC (обрезаются).
        4. Логирует VAD ratio и сколько тишины обрезано.

        Returns:
            Обработанный numpy-массив float32 или None если речи нет.
        """
        from core.vad import VoiceActivityDetector

        vad = VoiceActivityDetector()
        result = vad.detect(audio, sample_rate=sample_rate)

        total_sec = len(audio) / sample_rate if sample_rate > 0 else 0.0
        trimmed_silence_sec = 0.0

        logger.info(
            "VAD pre-filter: speech_ratio=%.2f, speech=%.2fs, silence=%.2fs, "
            "segments=%d, total=%.2fs",
            result.speech_ratio,
            result.total_speech_sec,
            result.total_silence_sec,
            len(result.speech_segments),
            total_sec,
        )

        if result.total_speech_sec < self._VAD_MIN_VOICE_SEC:
            logger.info(
                "VAD pre-filter: слишком мало речи (%.3fs < %.3fs) — STT пропускается",
                result.total_speech_sec,
                self._VAD_MIN_VOICE_SEC,
            )
            return None

        silence_threshold = settings.STT_VAD_SILENCE_TRIM_THRESHOLD_SEC
        pad_samples = int(self._VAD_PADDING_SEC * sample_rate)

        # Собираем фрагменты: каждый сегмент речи + padding
        chunks: list[np.ndarray] = []
        n_samples = len(audio)
        for seg in result.speech_segments:
            start_s = max(0, int(seg.start_sec * sample_rate) - pad_samples)
            end_s = min(n_samples, int(seg.end_sec * sample_rate) + pad_samples)
            chunks.append(audio[start_s:end_s])

        if not chunks:
            return None

        # Вычисляем реальную обрезанную тишину (паузы > threshold)
        # путём сравнения общей исходной длины с длиной конкатенированных фрагментов
        raw_extracted_sec = sum(len(c) for c in chunks) / sample_rate
        trimmed_silence_sec = max(0.0, total_sec - raw_extracted_sec)

        if trimmed_silence_sec > 0.01:
            logger.info(
                "VAD pre-filter: обрезано %.2fs тишины (порог=%.1fs, padding=%.1fs)",
                trimmed_silence_sec,
                silence_threshold,
                self._VAD_PADDING_SEC,
            )

        # Добавляем silence_padding между фрагментами вместо длинных пауз.
        # Это сохраняет относительный ритм речи для Whisper.
        silence_pad = np.zeros(pad_samples, dtype=np.float32)
        merged_parts: list[np.ndarray] = []
        for i, chunk in enumerate(chunks):
            merged_parts.append(chunk.astype(np.float32))
            if i < len(chunks) - 1:
                merged_parts.append(silence_pad)

        filtered = np.concatenate(merged_parts)
        return filtered

    def _transcribe_with_fallback(self, audio_data: Any, prompt: str, language: str | None = None) -> dict[str, Any]:
        """Пробует несколько моделей при возникновении ошибок (например, нехватка VRAM).

        Перед загрузкой тяжёлых моделей (не balanced) проверяет свободную память
        через vm_stat, чтобы macOS Jetsam не убил процесс (SIGKILL).
        """
        with _profiler.start_span("stt_with_fallback"):
            return self._transcribe_with_fallback_impl(audio_data, prompt, language)

    _SENSEVOICE_MARKER: str = "sensevoice:adapter"
    _PARAKEET_MARKER: str = "parakeet:adapter"
    _WHISPERX_MARKER: str = "whisperx:adapter"
    _VOXTRAL_MARKER: str = "voxtral:adapter"

    def _transcribe_with_fallback_impl(self, audio_data: Any, prompt: str, language: str | None = None) -> dict[str, Any]:
        """Внутренняя реализация fallback chain. Отделена от публичной _transcribe_with_fallback
        чтобы обернуть весь chain одним span'ом без изменения retry/timeout логики."""
        candidates = [self.current_model]
        if self.quality_profile == "max":
            candidates = list(dict.fromkeys(settings.model_max_list))

        balanced_model = settings.MODEL_BALANCED

        # --- Parakeet adapter: позиция 2 (после balanced, до SenseVoice) ---
        # Вставляем маркер ПОСЛЕ первого кандидата (balanced/turbo). Parakeet
        # EN-оптимизирован и пробуется перед SenseVoice (которая RU+эмоция).
        # Гейт по settings.PARAKEET_ENABLED. При сбое маркер помечается недоступным.
        if settings.PARAKEET_ENABLED and self._PARAKEET_MARKER not in self._unavailable_models:
            if len(candidates) >= 1:
                candidates = [candidates[0], self._PARAKEET_MARKER] + candidates[1:]
            else:
                candidates = [self._PARAKEET_MARKER]

        # --- SenseVoice adapter: additive попытка между balanced и max candidates ---
        # Вставляем маркер в цепочку кандидатов сразу после первой попытки (balanced
        # или первого из max_list). Гейт по settings.SENSEVOICE_ENABLED. При сбое
        # маркер помечается как недоступный, и chain продолжается на whisper'ах.
        # Примечание: если оба Parakeet и SenseVoice включены, порядок будет:
        # [balanced, PARAKEET_MARKER, SENSEVOICE_MARKER, ...остальные].
        if settings.SENSEVOICE_ENABLED and self._SENSEVOICE_MARKER not in self._unavailable_models:
            if len(candidates) >= 1:
                # После первой (balanced/turbo) попытки и после Parakeet (если включён)
                # Находим позицию сразу за всеми non-whisper маркерами в начале chain
                insert_at = 1
                while insert_at < len(candidates) and candidates[insert_at] == self._PARAKEET_MARKER:
                    insert_at += 1
                candidates = candidates[:insert_at] + [self._SENSEVOICE_MARKER] + candidates[insert_at:]
            else:
                candidates = [self._SENSEVOICE_MARKER]

        # --- WhisperX adapter: additive попытка ПОСЛЕ SenseVoice, перед max candidates ---
        # Chain order (когда всё включено):
        #   balanced → SenseVoice → WhisperX → max-candidates whisper-large-v3
        # Маркер вставляется на позицию 2 (после balanced и SenseVoice marker если они есть).
        # При сбое маркер помечается как недоступный, chain продолжается на whisper'ах.
        if settings.WHISPERX_ENABLED and self._WHISPERX_MARKER not in self._unavailable_models:
            # Находим позицию вставки: сразу за последним adapter-маркером или после balanced.
            insert_pos = 1
            for i, c in enumerate(candidates):
                if c == self._SENSEVOICE_MARKER:
                    insert_pos = i + 1
                    break
            candidates = candidates[:insert_pos] + [self._WHISPERX_MARKER] + candidates[insert_pos:]

        # --- Voxtral adapter: позиция 5 (после WhisperX, перед max-candidates) ---
        # Mistral Voxtral Mini 4B Realtime — STT + встроенный reasoning (Phase 4.4).
        # Chain order (когда все адаптеры включены):
        #   balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates
        # Маркер вставляется сразу за WHISPERX_MARKER (или за последним adapter-маркером).
        # При сбое маркер помечается как недоступный, chain продолжается на whisper'ах.
        if settings.VOXTRAL_ENABLED and self._VOXTRAL_MARKER not in self._unavailable_models:
            # Ищем позицию вставки: после WHISPERX_MARKER если есть, иначе после всех
            # adapter-маркеров в начале списка (PARAKEET / SENSEVOICE / WHISPERX).
            _adapter_markers = {self._PARAKEET_MARKER, self._SENSEVOICE_MARKER, self._WHISPERX_MARKER}
            vx_insert_pos = 1
            for i, c in enumerate(candidates):
                if c in _adapter_markers:
                    vx_insert_pos = i + 1
            candidates = candidates[:vx_insert_pos] + [self._VOXTRAL_MARKER] + candidates[vx_insert_pos:]

        # Таблица маркеров адаптеров: marker → (span_prefix, model_setting, transcribe_fn)
        _adapter_dispatch = [
            (
                self._PARAKEET_MARKER,
                "stt_parakeet",
                settings.PARAKEET_MODEL,
                lambda: self._transcribe_parakeet(audio_data, language=language),
            ),
            (
                self._SENSEVOICE_MARKER,
                "stt_sensevoice",
                settings.SENSEVOICE_MODEL,
                lambda: self._transcribe_sensevoice(audio_data, language=language),
            ),
            (
                self._WHISPERX_MARKER,
                "stt_whisperx",
                settings.WHISPERX_MODEL,
                lambda: self._transcribe_whisperx(audio_data, language=language),
            ),
            (
                self._VOXTRAL_MARKER,
                "stt_voxtral",
                settings.VOXTRAL_MODEL,
                lambda: self._transcribe_voxtral(audio_data, language=language),
            ),
        ]
        _adapter_map = {marker: (span_pfx, model, fn) for marker, span_pfx, model, fn in _adapter_dispatch}

        for model_name in candidates:
            # Adapter ветки (не whisper).
            if model_name in _adapter_map:
                span_pfx, adapter_model, adapter_fn = _adapter_map[model_name]
                try:
                    span_name = f"{span_pfx}_{_short_model_name(adapter_model)}"
                    with _profiler.start_span(span_name):
                        adapter_result = adapter_fn()
                    adapter_result["model_used"] = adapter_model
                    return adapter_result
                except Exception as exc:
                    logger.warning("%s adapter не сработал: %s — продолжаю chain", span_pfx, exc)
                    self._unavailable_models.add(model_name)
                    continue

            if model_name in self._unavailable_models:
                continue

            # Проверка памяти перед тяжёлыми моделями (не balanced)
            if model_name != balanced_model:
                avail_gb = _get_available_memory_gb()
                if 0 < avail_gb < _HEAVY_MODEL_MIN_FREE_GB:
                    logger.warning(
                        "Пропускаю тяжёлую модель %s: доступно %.1f GB, нужно >= %.1f GB",
                        model_name, avail_gb, _HEAVY_MODEL_MIN_FREE_GB,
                    )
                    continue

            try:
                timeout = settings.TRANSCRIBE_TIMEOUT_SEC
                span_name = f"stt_model_{_short_model_name(model_name)}"
                with _profiler.start_span(span_name):
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(self._transcribe_model, audio_data, model_name, prompt, language)
                        result = future.result(timeout=timeout)
                result["model_used"] = model_name
                return result
            except concurrent.futures.TimeoutError:
                logger.error(
                    "Таймаут %ds при транскрибации моделью %s — пропускаю",
                    settings.TRANSCRIBE_TIMEOUT_SEC, model_name,
                )
                self._unavailable_models.add(model_name)
            except MemoryError:
                logger.error("MemoryError при загрузке модели %s — помечаю как недоступную", model_name)
                self._unavailable_models.add(model_name)
            except OSError as e:
                # errno 12 = Cannot allocate memory — ядро отказало в mmap
                if e.errno == 12 or "Cannot allocate memory" in str(e):
                    logger.error("OOM (OSError) при модели %s: %s — помечаю как недоступную", model_name, e)
                    self._unavailable_models.add(model_name)
                else:
                    logger.warning("Модель %s не сработала (OSError): %s", model_name, e)
                    self._unavailable_models.add(model_name)
            except Exception as e:
                logger.warning("Модель %s не сработала: %s", model_name, e)
                self._unavailable_models.add(model_name)

        # Если локально ничего не вышло — пробуем облако (если разрешено)
        if settings.NETWORK_MODE != "offline_strict":
            logger.info("Локальные модели недоступны, переключаюсь на Remote STT...")
            with _profiler.start_span("stt_remote"):
                return self._transcribe_remote(audio_data, prompt)

        raise RuntimeError("Все доступные STT-движки вышли из строя.")

    def _transcribe_model(self, audio_data: Any, model_name: str, prompt: str, language: str | None = None) -> dict[str, Any]:
        """Низкоуровневый вызов MLX Whisper с обработкой несовместимых аргументов.

        Все MLX вызовы сериализуются через глобальный RLock (mlx_lock) во избежание
        race condition в __hash_table<MTL::Resource*> внутри libmlx.dylib (SIGSEGV).
        RLock позволяет повторный захват из того же потока (fallback chain).
        """
        effective_language = language if language is not None else settings.TRANSCRIBE_LANGUAGE
        base_params = {
            "path_or_hf_repo": model_name,
            "initial_prompt": prompt,
            "language": effective_language,
            "temperature": 0.0,
            "verbose": False,
        }

        # Варианты аргументов для разных версий библиотеки
        variants = [
            {**base_params, "condition_on_previous_text": False, "no_speech_threshold": 0.6},
            {**base_params, "condition_on_previous_text": False},
            base_params,
        ]

        last_err = None
        # Сериализуем доступ к GPU через глобальный MLX lock.
        # Минимальный critical section: только сам mlx_whisper.transcribe вызов.
        with mlx_lock():
            for params in variants:
                try:
                    return mlx_whisper.transcribe(audio_data, **params)
                except TypeError as e:
                    last_err = e
        raise last_err or RuntimeError("Ошибка вызова mlx_whisper.transcribe")

    # --- SenseVoice adapter (Alibaba FunASR, Phase 4 quick win) ---
    # SenseVoice-Small — мультиязычная STT модель (50+ языков, вкл. RU) c встроенной
    # эмоцией (happy/neutral/angry/sad/fearful/disgusted/surprised) и language ID.
    # Размер модели: ~900 MB на диске, ~1.5 GB RAM при загрузке (float32 весы).
    # Приоритет перед whisper-large: быстрее (NFA inference) и даёт эмоцию бесплатно.

    # Маппинг эмоциональных спецтокенов SenseVoice → короткие метки для истории.
    # SenseVoice вставляет токены прямо в текст: "<|HAPPY|><|ru|>привет мир".
    _SENSEVOICE_EMOTION_MAP: dict[str, str] = {
        "HAPPY": "happy",
        "SAD": "sad",
        "ANGRY": "angry",
        "NEUTRAL": "neutral",
        "FEARFUL": "fearful",
        "DISGUSTED": "disgusted",
        "SURPRISED": "surprised",
        "EMO_UNKNOWN": "unknown",
    }

    _SENSEVOICE_LANG_MAP: dict[str, str] = {
        "zh": "zh",
        "en": "en",
        "ja": "ja",
        "ko": "ko",
        "ru": "ru",
        "es": "es",
        "yue": "yue",
        "nospeech": "",
        "auto": "",
    }

    def _load_sensevoice_model(self) -> Any:
        """Ленивая загрузка SenseVoice pipeline. Raises если funasr недоступен."""
        if self._sensevoice_model is not None:
            return self._sensevoice_model
        if self._sensevoice_load_error:
            raise RuntimeError(self._sensevoice_load_error)
        if _SenseVoiceAutoModel is None:
            self._sensevoice_load_error = (
                "funasr не установлен — SenseVoice adapter недоступен "
                "(установите: pip install funasr)"
            )
            raise RuntimeError(self._sensevoice_load_error)
        with _profiler.start_span(f"model_load_{_short_model_name(settings.SENSEVOICE_MODEL)}"):
            try:
                # device='mps' не поддерживается funasr'ом стабильно; cpu — безопасный
                # default. Pytorch сам выберет MPS если модель будет это поддерживать.
                self._sensevoice_model = _SenseVoiceAutoModel(
                    model=settings.SENSEVOICE_MODEL,
                    trust_remote_code=True,
                    disable_update=True,
                )
            except Exception as exc:
                self._sensevoice_load_error = f"Не удалось загрузить SenseVoice: {exc}"
                raise RuntimeError(self._sensevoice_load_error)
            logger.info("SenseVoice модель загружена: %s", settings.SENSEVOICE_MODEL)
            return self._sensevoice_model

    @staticmethod
    def _parse_sensevoice_output(raw_text: str) -> tuple[str, str | None, str | None]:
        """Парсит SenseVoice output: выделяет эмоцию, язык, чистый текст.

        SenseVoice формат: "<|ru|><|HAPPY|><|Speech|><|woitn|>привет мир"
        - lang tag: <|ru|>, <|en|>, etc.
        - emotion tag: <|HAPPY|>, <|NEUTRAL|>, etc.
        - event/itn теги игнорируем.
        Returns (clean_text, emotion_label|None, language|None).
        """
        if not raw_text:
            return "", None, None
        import re as _re
        tokens = _re.findall(r"<\|([^|]+)\|>", raw_text)
        emotion: str | None = None
        language: str | None = None
        for tok in tokens:
            if tok in AudioEngine._SENSEVOICE_EMOTION_MAP:
                emotion = AudioEngine._SENSEVOICE_EMOTION_MAP[tok]
            elif tok.lower() in AudioEngine._SENSEVOICE_LANG_MAP:
                mapped = AudioEngine._SENSEVOICE_LANG_MAP[tok.lower()]
                if mapped:
                    language = mapped
        # Убираем все <|...|> токены и лишние пробелы
        clean = _re.sub(r"<\|[^|]+\|>", "", raw_text).strip()
        return clean, emotion, language

    def _transcribe_sensevoice(self, audio_data: Any, language: str | None = None) -> dict[str, Any]:
        """Транскрибация через SenseVoice (FunASR) с эмоцией.

        Args:
            audio_data: путь к wav-файлу (str/Path) или numpy.ndarray (16kHz mono).
            language: ISO 639-1 код языка ("ru", "es", "en") или None для авто.

        Returns:
            dict с ключами:
              - text: транскрипт без спецтокенов
              - engine: "sensevoice"
              - emotion: "happy"|"neutral"|... или None
              - language: детектированный язык (ISO 639-1) или None
              - segments: [] (SenseVoice не даёт segment-level avg_logprob)
        """
        model = self._load_sensevoice_model()
        # FunASR language param: короткий код "ru"/"en"/... или "auto"
        lang_param = language if language else "auto"
        # FunASR принимает путь к файлу или numpy float32 @ 16kHz
        input_audio: Any = audio_data
        if isinstance(audio_data, Path):
            input_audio = str(audio_data)
        # FunASR generate API возвращает list[dict] с ключом "text"
        outputs = model.generate(
            input=input_audio,
            language=lang_param,
            use_itn=True,
            batch_size_s=60,
        )
        if not outputs:
            raise RuntimeError("SenseVoice вернул пустой результат")
        raw_text = ""
        if isinstance(outputs, list) and outputs:
            first = outputs[0]
            if isinstance(first, dict):
                raw_text = str(first.get("text", ""))
            else:
                raw_text = str(first)
        clean_text, emotion, detected_lang = self._parse_sensevoice_output(raw_text)
        logger.info(
            "SenseVoice готово: %d chars, emotion=%s, lang=%s",
            len(clean_text), emotion or "—", detected_lang or "—",
        )
        return {
            "text": clean_text,
            "engine": "sensevoice",
            "emotion": emotion,
            "language": detected_lang,
            "segments": [],
        }

    # --- Parakeet-TDT-1.1B adapter (NVIDIA NeMo, Phase 4.2) ---
    # NVIDIA Parakeet-TDT-1.1B — топ OpenASR leaderboard на English.
    # WER лучше, чем whisper-large-v3 на EN бенчмарках (LibriSpeech, MLS, etc.).
    # Размер: ~2.3 GB на диске, ~3-4 GB RAM при загрузке (float32 + attention heads).
    # Работает через NeMo (PyTorch backend). На Apple Silicon: MPS или CPU.
    # CUDA не обязателен — MPS работает нативно на M-серии.
    # Ограничения: только English (EN-only модель), нет emotion/lang tags.
    # NeMo управляет режимом инференса внутри — мы не вмешиваемся в это.

    def _load_parakeet_model(self) -> Any:
        """Ленивая загрузка Parakeet-TDT модели через NeMo. Raises если nemo недоступен."""
        if self._parakeet_model is not None:
            return self._parakeet_model
        if self._parakeet_load_error:
            raise RuntimeError(self._parakeet_load_error)
        if _nemo_asr is None:
            self._parakeet_load_error = (
                "nemo не установлен — Parakeet adapter недоступен "
                "(установите: pip install nemo-toolkit[asr])"
            )
            raise RuntimeError(self._parakeet_load_error)
        with _profiler.start_span(f"model_load_{_short_model_name(settings.PARAKEET_MODEL)}"):
            try:
                # NeMo from_pretrained скачивает веса с HuggingFace/NVIDIA NGC.
                # Устройство определяется автоматически: MPS на Apple Silicon,
                # CUDA на NVIDIA GPU, CPU fallback. NeMo сам управляет инференсом.
                model = _nemo_asr.models.ASRModel.from_pretrained(
                    model_name=settings.PARAKEET_MODEL,
                )
                self._parakeet_model = model
            except Exception as exc:
                self._parakeet_load_error = f"Не удалось загрузить Parakeet: {exc}"
                raise RuntimeError(self._parakeet_load_error)
            logger.info("Parakeet модель загружена: %s", settings.PARAKEET_MODEL)
            return self._parakeet_model

    def _transcribe_parakeet(self, audio_data: Any, language: str | None = None) -> dict[str, Any]:
        """Транскрибация через Parakeet-TDT-1.1B (NVIDIA NeMo).

        Args:
            audio_data: путь к wav-файлу (str/Path) или numpy.ndarray (16kHz mono float32).
            language: ISO 639-1 код (только "en" поддерживается Parakeet; иные языки
                      пробрасываются без фильтрации, но качество будет ниже).

        Returns:
            dict с ключами:
              - text: транскрипт
              - engine: "parakeet"
              - language: "en" (Parakeet EN-only)
              - segments: [] (NeMo transcribe не возвращает segment-level данных в базовом API)
        """
        import tempfile as _tempfile
        import os as _os

        model = self._load_parakeet_model()

        # NeMo transcribe принимает list[str] с путями к wav-файлам.
        # Если пришёл numpy array — сохраняем во временный файл.
        tmp_path: str | None = None
        try:
            if isinstance(audio_data, (str, Path)):
                audio_paths = [str(audio_data)]
            else:
                # numpy array: пишем в tmp wav (16kHz, mono, float32)
                import soundfile as _sf
                with _tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    tmp_path = f.name
                _sf.write(tmp_path, audio_data, samplerate=16000)
                audio_paths = [tmp_path]

            # NeMo transcribe возвращает list[str] (тексты).
            outputs = model.transcribe(audio_paths)
            if not outputs:
                raise RuntimeError("Parakeet вернул пустой результат")
            text = outputs[0] if isinstance(outputs[0], str) else str(outputs[0])
        finally:
            if tmp_path and _os.path.exists(tmp_path):
                _os.unlink(tmp_path)

        logger.info("Parakeet готово: %d chars", len(text))
        return {
            "text": text,
            "engine": "parakeet",
            "language": "en",
            "segments": [],
        }

    # --- WhisperX adapter (Phase 4.3) ---
    # whisperx от m-bain: whisper-large-v3 + forced phoneme alignment (word timestamps)
    # + pyannote diarization в одном pipeline'е. Использует torch (не MLX), поэтому
    # работает на mps/cpu; MPS ускоряет inference на Apple Silicon но не так сильно как MLX.
    # Размер: whisper-large-v3 ~3 GB + alignment model ~200 MB + pyannote ~1.5 GB.
    # Итого: ~4.5-5 GB RAM при полном pipeline (WHISPERX_DIARIZATION=True).
    # При WHISPERX_DIARIZATION=False: ~3.2 GB (только whisper + aligner).

    def _load_whisperx_model(self) -> Any:
        """Ленивая загрузка WhisperX pipeline. Raises если whisperx недоступен."""
        # Используем getattr для совместимости с тестами, которые создают движок через
        # AudioEngine.__new__() без вызова __init__ (паттерн из SenseVoice/Parakeet тестов).
        if getattr(self, "_whisperx_model", None) is not None:
            return self._whisperx_model
        if getattr(self, "_whisperx_load_error", None):
            raise RuntimeError(self._whisperx_load_error)
        if _whisperx is None:
            self._whisperx_load_error = (
                "whisperx не установлен — WhisperX adapter недоступен "
                "(установите: pip install whisperx)"
            )
            raise RuntimeError(self._whisperx_load_error)

        device = settings.WHISPERX_DEVICE
        # MPS доступен только на macOS с Apple Silicon (torch >= 1.12).
        # Если запрошен mps но недоступен — безопасный fallback на cpu.
        if device == "mps" and torch is not None:
            try:
                import torch as _torch
                if not _torch.backends.mps.is_available():
                    logger.warning("WhisperX: MPS недоступен, переключаюсь на cpu")
                    device = "cpu"
            except Exception:
                device = "cpu"
        elif torch is None:
            device = "cpu"

        # compute_type: на cpu/mps рекомендуется "float32" или "int8".
        # "float16" работает только на CUDA; на MPS может вызвать ошибку.
        compute_type = "float32" if device in ("cpu", "mps") else "float16"

        with _profiler.start_span(f"model_load_whisperx_{_short_model_name(settings.WHISPERX_MODEL)}"):
            try:
                self._whisperx_model = _whisperx.load_model(
                    settings.WHISPERX_MODEL,
                    device=device,
                    compute_type=compute_type,
                )
            except Exception as exc:
                self._whisperx_load_error = f"Не удалось загрузить WhisperX: {exc}"
                raise RuntimeError(self._whisperx_load_error)
        logger.info("WhisperX модель загружена: %s (device=%s)", settings.WHISPERX_MODEL, device)
        return self._whisperx_model

    def _transcribe_whisperx(self, audio_data: Any, language: str | None = None) -> dict[str, Any]:
        """Транскрибация через WhisperX с word-level timestamps и diarization.

        Args:
            audio_data: путь к wav-файлу (str/Path) или numpy.ndarray (16kHz mono float32).
            language: ISO 639-1 код языка ("ru", "es", "en") или None для авто.

        Returns:
            dict с ключами:
              - text: полный транскрипт (конкатенация всех сегментов)
              - engine: "whisperx"
              - language: детектированный язык (ISO 639-1) или None
              - segments: list[dict] whisper-сегменты
              - word_timestamps: list[{word, start, end, confidence}] — если WHISPERX_WORD_TIMESTAMPS=True
              - speaker_turns: list[{speaker, start, end}] — если WHISPERX_DIARIZATION=True
        """
        import numpy as _np

        model = self._load_whisperx_model()

        # whisperx.transcribe принимает numpy float32 16kHz mono.
        # Если передан путь к файлу — загружаем через whisperx.load_audio.
        if isinstance(audio_data, (str, Path)):
            audio_path = str(Path(audio_data).expanduser().resolve())
            audio_array = _whisperx.load_audio(audio_path)
        elif isinstance(audio_data, bytes):
            # bytes (из AudioRecorder) — конвертируем через numpy.
            arr = _np.frombuffer(audio_data, dtype=_np.int16).astype(_np.float32) / 32768.0
            audio_array = arr
        else:
            # Уже numpy array
            audio_array = audio_data

        # Транскрибация (batch_size=16 — безопасный дефолт для 36 GB RAM).
        lang_param = language if language else None
        result = model.transcribe(audio_array, batch_size=16, language=lang_param)

        detected_lang = result.get("language") or language

        # --- Word-level timestamps (phoneme alignment) ---
        word_timestamps = None
        if settings.WHISPERX_WORD_TIMESTAMPS and result.get("segments"):
            try:
                align_model, metadata = _whisperx.load_align_model(
                    language_code=detected_lang or "en",
                    device=settings.WHISPERX_DEVICE,
                )
                aligned = _whisperx.align(
                    result["segments"],
                    align_model,
                    metadata,
                    audio_array,
                    settings.WHISPERX_DEVICE,
                    return_char_alignments=False,
                )
                raw_words = []
                for seg in aligned.get("segments", []):
                    for w in seg.get("words", []):
                        raw_words.append({
                            "word": str(w.get("word", "")).strip(),
                            "start": float(w.get("start", 0.0)),
                            "end": float(w.get("end", 0.0)),
                            "confidence": float(w.get("score", 0.0)),
                        })
                word_timestamps = raw_words if raw_words else None
            except Exception as exc:
                logger.warning("WhisperX word alignment не удался: %s — продолжаю без timestamps", exc)

        # --- Diarization через pyannote внутри whisperx ---
        speaker_turns = None
        if settings.WHISPERX_DIARIZATION and settings.HF_TOKEN:
            try:
                diarize_model = _whisperx.DiarizationPipeline(
                    use_auth_token=settings.HF_TOKEN,
                    device=settings.WHISPERX_DEVICE,
                )
                diarize_segments = diarize_model(audio_array)
                # Если есть выровненные сегменты — присваиваем спикеров словам
                if word_timestamps is not None and aligned is not None:
                    aligned_with_speakers = _whisperx.assign_word_speakers(
                        diarize_segments, aligned
                    )
                    # Перестраиваем word_timestamps со speaker полем
                    raw_words_spk = []
                    for seg in aligned_with_speakers.get("segments", []):
                        for w in seg.get("words", []):
                            raw_words_spk.append({
                                "word": str(w.get("word", "")).strip(),
                                "start": float(w.get("start", 0.0)),
                                "end": float(w.get("end", 0.0)),
                                "confidence": float(w.get("score", 0.0)),
                            })
                    if raw_words_spk:
                        word_timestamps = raw_words_spk
                # Конвертируем diarize_segments в speaker_turns список
                turns: list[dict[str, Any]] = []
                if hasattr(diarize_segments, "itertracks"):
                    for turn, _, speaker in diarize_segments.itertracks(yield_label=True):
                        turns.append({
                            "speaker": str(speaker),
                            "start": float(turn.start),
                            "end": float(turn.end),
                        })
                elif hasattr(diarize_segments, "iterrows"):
                    for _, row in diarize_segments.iterrows():
                        turns.append({
                            "speaker": str(row.get("speaker", "")),
                            "start": float(row.get("start", 0.0)),
                            "end": float(row.get("end", 0.0)),
                        })
                speaker_turns = turns if turns else None
            except Exception as exc:
                logger.warning("WhisperX diarization не удалась: %s — продолжаю без спикеров", exc)
        elif settings.WHISPERX_DIARIZATION and not settings.HF_TOKEN:
            logger.warning(
                "WhisperX: WHISPERX_DIARIZATION=True, но HF_TOKEN не задан — "
                "diarization пропущена. Задайте KRAB_EAR_HF_TOKEN=<ваш_токен>."
            )

        # Собираем текст из сегментов
        segments = result.get("segments", [])
        full_text = " ".join(str(seg.get("text", "")).strip() for seg in segments).strip()

        logger.info(
            "WhisperX готово: %d chars, %d слов, %d спикер-отрезков, lang=%s",
            len(full_text),
            len(word_timestamps) if word_timestamps else 0,
            len(speaker_turns) if speaker_turns else 0,
            detected_lang or "—",
        )
        return {
            "text": full_text,
            "engine": "whisperx",
            "language": detected_lang,
            "segments": segments,
            "word_timestamps": word_timestamps,
            "speaker_turns": speaker_turns,
        }

    # --- Voxtral Mini 4B Realtime adapter (Mistral, Phase 4.4) ---
    # Mistral Voxtral-Mini-4B-Realtime-2602: аудио-encoder (970M) + Mistral Small 3.1 LM (3.4B).
    # STT + встроенный reasoning (summary/Q&A/function-calling). Мультиязычный (13 яз: RU/ES/EN).
    # MLX 4-bit quant: ~2–3 GB RAM (8.9 GB BF16). Лицензия: Apache 2.0.
    # Latency: 480ms recommended (качество ≈ Whisper offline); диапазон 80ms–2.4s.
    # Библиотека: mistral-inference (pip install mistral-inference).
    # При VOXTRAL_REASONING_ENABLED=True возвращает reasoning: str (summary/Q&A).

    def _load_voxtral_model(self) -> Any:
        """Ленивая загрузка Voxtral pipeline. Raises если mistral-inference недоступен."""
        # Совместимость с тестами через AudioEngine.__new__() (без __init__).
        if getattr(self, "_voxtral_model", None) is not None:
            return self._voxtral_model
        if getattr(self, "_voxtral_load_error", None):
            raise RuntimeError(self._voxtral_load_error)
        if not _voxtral_available:
            self._voxtral_load_error = (
                "mistral-inference не установлен — Voxtral adapter недоступен "
                "(установите: pip install mistral-inference)"
            )
            raise RuntimeError(self._voxtral_load_error)

        with _profiler.start_span(f"model_load_voxtral_{_short_model_name(settings.VOXTRAL_MODEL)}"):
            try:
                from huggingface_hub import snapshot_download  # type: ignore
                model_path = snapshot_download(repo_id=settings.VOXTRAL_MODEL)
                tokenizer = _VoxtralTokenizer.from_file(str(Path(model_path) / "tokenizer.model.v3"))
                model = _VoxtralTransformer.from_folder(model_path)
                self._voxtral_model = (model, tokenizer)
            except Exception as exc:
                self._voxtral_load_error = f"Не удалось загрузить Voxtral: {exc}"
                raise RuntimeError(self._voxtral_load_error)

        logger.info("Voxtral модель загружена: %s", settings.VOXTRAL_MODEL)
        return self._voxtral_model

    def _transcribe_voxtral(self, audio_data: Any, language: str | None = None) -> dict[str, Any]:
        """Транскрибация через Voxtral Mini 4B Realtime (STT + optional reasoning).

        Args:
            audio_data: путь к wav-файлу (str/Path), numpy.ndarray (16kHz mono float32),
                        или bytes (PCM int16 LE от AudioRecorder).
            language: ISO 639-1 код языка ("ru", "es", "en") или None для авто.

        Returns:
            dict с ключами:
              - text: транскрипт
              - engine: "voxtral"
              - language: детектированный или переданный язык (ISO 639-1) или None
              - segments: [] (Voxtral не возвращает временные сегменты)
              - reasoning: str | None — если VOXTRAL_REASONING_ENABLED=True
        """
        import numpy as _np
        import tempfile as _tmp
        import os as _os

        model, tokenizer = self._load_voxtral_model()

        # Нормализуем audio_data → временный wav-файл (mistral-inference принимает путь).
        if isinstance(audio_data, (str, Path)):
            audio_path = str(Path(audio_data).expanduser().resolve())
        elif isinstance(audio_data, bytes):
            # bytes (PCM int16 LE, 16kHz mono) → numpy → wav файл
            arr = _np.frombuffer(audio_data, dtype=_np.int16)
            tmp_f = _tmp.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_path = tmp_f.name
            tmp_f.close()
            try:
                import soundfile as _sf
                _sf.write(tmp_path, arr.astype(_np.float32) / 32768.0, 16000, subtype="PCM_16")
            except Exception:
                # Если soundfile недоступен — пробуем wave stdlib
                import wave
                with wave.open(tmp_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(16000)
                    wf.writeframes(audio_data)
            audio_path = tmp_path
        else:
            # numpy array → сохраняем во временный файл
            tmp_f = _tmp.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp_path = tmp_f.name
            tmp_f.close()
            try:
                import soundfile as _sf
                audio_arr = audio_data if audio_data.dtype == _np.float32 else audio_data.astype(_np.float32)
                _sf.write(tmp_path, audio_arr, 16000, subtype="PCM_16")
            except Exception as exc:
                raise RuntimeError(f"Voxtral: не удалось сохранить аудио: {exc}")
            audio_path = tmp_path

        try:
            # Формируем prompt: STT запрос, опционально с reasoning
            if settings.VOXTRAL_REASONING_ENABLED:
                prompt_text = (
                    "Transcribe the audio accurately. "
                    "Then provide a brief summary of the content."
                )
            else:
                prompt_text = "Transcribe the audio accurately."

            audio_chunk = _VoxtralAudioChunk(path=audio_path)
            completion_request = _VoxtralChatRequest(
                messages=[
                    _VoxtralUserMessage(content=[audio_chunk, prompt_text]),
                ]
            )

            tokens, _ = tokenizer.encode_chat_completion(completion_request)
            input_ids = tokens.tokens

            out_tokens, _ = _voxtral_generate(
                input_ids,
                model,
                max_tokens=_VOXTRAL_MAX_TOKENS,
                temperature=0.0,
                eos_id=tokenizer.instruct_tokenizer.tokenizer.eos_id,
            )

            raw_output = tokenizer.instruct_tokenizer.tokenizer.decode(out_tokens)

            # Парсим результат: разделяем transcript и reasoning если включён
            reasoning: str | None = None
            if settings.VOXTRAL_REASONING_ENABLED and "\n\n" in raw_output:
                parts = raw_output.split("\n\n", 1)
                transcript = parts[0].strip()
                reasoning = parts[1].strip() if len(parts) > 1 else None
            else:
                transcript = raw_output.strip()

        finally:
            # Удаляем временный файл если создавали
            if not isinstance(audio_data, (str, Path)):
                try:
                    _os.unlink(audio_path)
                except Exception:
                    pass

        logger.info(
            "Voxtral готово: %d chars, reasoning=%s, lang=%s",
            len(transcript),
            "да" if reasoning else "нет",
            language or "авто",
        )
        return {
            "text": transcript,
            "engine": "voxtral",
            "language": language,
            "segments": [],
            "reasoning": reasoning,
        }

    def _maybe_run_diarization(
        self,
        audio_data: Any,
        whisper_segments: list[dict[str, Any]],
        *,
        is_preview: bool,
    ) -> dict[str, Any]:
        """Пытается проставить спикеров для файловой транскрибации.

        Решение сделано мягким: любая ошибка diarization логируется и попадает в
        результат как служебное поле, но не ломает базовую STT-транскрибацию.
        """
        base_result: dict[str, Any] = {
            "enabled": False,
            "speaker_segments": [],
            "annotated_segments": [],
            "speaker_turns": [],
        }
        if is_preview or not settings.DIARIZATION_ENABLED:
            return base_result

        audio_path = self._resolve_audio_path(audio_data)
        if audio_path is None:
            return base_result

        try:
            speaker_segments = self._run_diarization(audio_path)
            annotated_segments = self._annotate_segments_with_speakers(whisper_segments, speaker_segments)
            speaker_turns = self._merge_speaker_turns(annotated_segments)
            return {
                "enabled": True,
                "speaker_segments": speaker_segments,
                "annotated_segments": annotated_segments,
                "speaker_turns": speaker_turns,
            }
        except Exception as exc:
            logger.warning("Diarization недоступен для %s: %s", audio_path, exc)
            return {**base_result, "error": str(exc)}

    def _resolve_audio_path(self, audio_data: Any) -> str | None:
        """Возвращает путь к файлу, если diarization можно запускать по месту."""
        if isinstance(audio_data, Path):
            return str(audio_data.expanduser().resolve())
        if isinstance(audio_data, str):
            candidate = Path(audio_data).expanduser().resolve()
            if candidate.exists():
                return str(candidate)
        return None

    def _load_diarization_pipeline(self) -> Pipeline:
        """Ленивая загрузка pyannote pipeline с токеном Hugging Face."""
        if self._diarization_pipeline is not None:
            return self._diarization_pipeline
        if self._diarization_load_error:
            raise RuntimeError(self._diarization_load_error)

        hf_token = os.environ.get("HF_TOKEN") or settings.HF_TOKEN or None

        # Используем ленивую инициализацию, чтобы не тянуть модель в realtime-пути.
        # Если HF_HUB_OFFLINE=1, модель загружается из кэша без token.
        # Span фиксируется только при первом реальном load'е (guard выше гарантирует
        # что повторные вызовы сразу возвращают кэш).
        with _profiler.start_span(f"model_load_{_short_model_name(settings.DIARIZATION_MODEL)}"):
            try:
                kwargs = {"token": hf_token} if hf_token else {}
                self._diarization_pipeline = Pipeline.from_pretrained(settings.DIARIZATION_MODEL, **kwargs)
            except Exception as e:
                self._diarization_load_error = f"Не удалось загрузить pyannote pipeline: {e}"
                raise RuntimeError(self._diarization_load_error)
            diarization_device = self._resolve_diarization_device()
            self._diarization_pipeline.to(diarization_device)
            logger.info("Diarization pipeline загружен на устройство %s", diarization_device)
            return self._diarization_pipeline

    @staticmethod
    def _resolve_diarization_device() -> torch.device:
        """Выбирает устройство для pyannote diarization.

        MPS (Metal) re-enabled: torch 2.11 + pyannote 4.0.4 на M4 Max
        больше не вызывает Metal GPU assertion failure.
        Протестировано 2026-04-11 — 0.2s на 5 сек аудио, без crash.
        """
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")

    def _run_diarization(self, audio_path: str) -> list[dict[str, Any]]:
        """Запускает pyannote diarization и нормализует результат в словари."""
        with _profiler.start_span("diarization"):
            return self._run_diarization_impl(audio_path)

    def _run_diarization_impl(self, audio_path: str) -> list[dict[str, Any]]:
        """Внутренняя реализация diarization. Вынесена чтобы обернуть весь chain
        одним span'ом без изменения обработки ошибок."""
        pipeline = self._load_diarization_pipeline()
        prepared_audio_path, should_cleanup = self._prepare_audio_for_diarization(audio_path)
        try:
            diarization = pipeline(prepared_audio_path)
        except Exception as e:
            # --- Krab's Black Box ---
            import traceback
            error_log_path = "/tmp/krab_ear_diarization_error.log"
            logging.error(f"FATAL: Unhandled exception in diarization pipeline. Writing details to {error_log_path}")
            with open(error_log_path, "w") as f:
                f.write("A critical error occurred in the pyannote.audio pipeline block.\\n")
                f.write(f"Exception Type: {type(e).__name__}\\n")
                f.write(f"Exception Args: {e}\\n\\n")
                f.write("--- Traceback ---\\n")
                traceback.print_exc(file=f)
            # Re-raise to let the outer handler log and continue
            raise e
        finally:
            if should_cleanup:
                Path(prepared_audio_path).unlink(missing_ok=True)
        if hasattr(diarization, "speaker_diarization"):
            diarization = diarization.speaker_diarization
        speaker_segments: list[dict[str, Any]] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_segments.append(
                {
                    "start": round(float(turn.start), 3),
                    "end": round(float(turn.end), 3),
                    "speaker": str(speaker),
                }
            )
        return speaker_segments

    def _prepare_audio_for_diarization(self, audio_path: str) -> tuple[str, bool]:
        """Подготавливает совместимый WAV для pyannote.

        Для `.m4a` и других контейнеров прогоняем ffmpeg в mono/16k WAV, потому
        что torchcodec/pyannote иногда получают нестабильное число сэмплов.
        """
        source_path = Path(audio_path)
        if source_path.suffix.lower() == ".wav":
            return str(source_path), False

        with tempfile.NamedTemporaryFile(prefix="krab_ear_diarization_", suffix=".wav", delete=False) as handle:
            temp_path = Path(handle.name)

        cmd = [
            _FFMPEG_PATH,
            "-y",
            "-i",
            str(source_path),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(temp_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg не смог подготовить аудио для diarization: {completed.stderr.strip()}")
        return str(temp_path), True

    def _annotate_segments_with_speakers(
        self,
        whisper_segments: list[dict[str, Any]],
        speaker_segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Назначает каждому whisper-сегменту спикера по максимальному overlap."""
        annotated: list[dict[str, Any]] = []
        for segment in whisper_segments:
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            best_speaker = "SPEAKER_UNKNOWN"
            best_overlap = -1.0

            for speaker_segment in speaker_segments:
                overlap = self._segment_overlap(
                    start,
                    end,
                    float(speaker_segment.get("start", 0.0)),
                    float(speaker_segment.get("end", 0.0)),
                )
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_speaker = str(speaker_segment.get("speaker", "SPEAKER_UNKNOWN"))

            annotated.append({**segment, "speaker": best_speaker})
        return annotated

    @staticmethod
    def _segment_overlap(start_a: float, end_a: float, start_b: float, end_b: float) -> float:
        """Вычисляет длительность пересечения двух сегментов."""
        return max(0.0, min(end_a, end_b) - max(start_a, start_b))

    def _merge_speaker_turns(self, annotated_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Склеивает соседние сегменты одного спикера в более читаемые реплики."""
        turns: list[dict[str, Any]] = []
        for segment in annotated_segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            speaker = str(segment.get("speaker", "SPEAKER_UNKNOWN"))
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))

            if turns and turns[-1]["speaker"] == speaker:
                turns[-1]["end"] = end
                turns[-1]["text"] = f"{turns[-1]['text']} {text}".strip()
                continue

            turns.append(
                {
                    "speaker": speaker,
                    "start": start,
                    "end": end,
                    "text": text,
                }
            )
        return turns

    def _transcribe_remote(self, audio_data: Any, prompt: str) -> dict[str, Any]:
        """Обращение к внешнему OpenAI-совместимому API.

        audio_data может быть str (путь к существующему WAV файлу) или
        numpy.ndarray (raw audio buffer из live recording). Для ndarray мы
        сериализуем во временный WAV, отправляем, и гарантированно удаляем
        temp-файл в finally-блоке.
        """
        import tempfile
        import numpy as np

        cleanup_temp_path: str | None = None
        try:
            if isinstance(audio_data, np.ndarray):
                # Live buffer: пишем в temp WAV (16kHz mono float32 — whisper native rate)
                import soundfile as sf
                with tempfile.NamedTemporaryFile(
                    suffix=".wav", delete=False, dir=str(settings.DATA_DIR)
                ) as tmp:
                    cleanup_temp_path = tmp.name
                sf.write(cleanup_temp_path, audio_data, 16000)
                audio_path = cleanup_temp_path
            elif isinstance(audio_data, (str, bytes, os.PathLike)):
                audio_path = str(audio_data)
            else:
                raise TypeError(
                    f"_transcribe_remote: unsupported audio_data type {type(audio_data).__name__}"
                )

            with open(audio_path, "rb") as f:
                resp = requests.post(
                    settings.STT_GATEWAY_URL,
                    headers={"Authorization": "Bearer token_here"},  # Placeholder: local gateway не требует auth
                    files={"file": (os.path.basename(audio_path), f, "audio/wav")},
                    data={"model": settings.STT_MODEL, "prompt": prompt},
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                return {"text": data.get("text", ""), "engine": "remote"}
        except Exception as e:
            logger.error("Ошибка Remote STT: %s", e)
            raise
        finally:
            if cleanup_temp_path is not None:
                try:
                    os.unlink(cleanup_temp_path)
                except OSError:
                    pass

    def speak(self, text: str, rate: int = 185) -> None:
        """Озвучка текста через macOS `say`."""
        if not text.strip():
            return
        cmd = ["say", "-r", str(rate)]
        if settings.SAY_VOICE:
            import re as _re
            voice = settings.SAY_VOICE
            if not _re.match(r'^[a-zA-Z0-9 _-]+$', voice):
                voice = "Milena"  # безопасный fallback
            cmd.extend(["-v", voice])
        cmd.append(text)
        subprocess.run(cmd, check=False)
