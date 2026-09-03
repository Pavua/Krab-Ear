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
import threading
import time
from typing import Any, Callable, Optional, TYPE_CHECKING
from pathlib import Path

if TYPE_CHECKING:
    from backend.llm_rewriter import LLMRewriter

import numpy as np

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
from core.mlx_inter_lock import MLXInterLockTimeout, mlx_inter_process_lock  # noqa: E402
from core.mlx_lock import MLXLockTimeoutError  # noqa: E402
from core.mlx_subprocess import MLXTimeoutError, get_watchdog  # noqa: E402
from core.mlx_memory_gate import should_skip_second_mlx_checkpoint  # noqa: E402
from core.transcript_context import build_initial_prompt, merge_language_hotwords
from core import stt_budget  # noqa: E402

try:
    import soundfile as sf  # type: ignore
except Exception:
    sf = None  # type: ignore[assignment]

try:
    import torch  # type: ignore
except Exception:
    torch = None  # type: ignore[assignment]

# Телеметрия pyannote 4.x (OpenTelemetry-метрики) — выключаем ДО импорта:
# privacy-first продукт не шлёт usage-метрики, плюс её track_pipeline_apply
# крашится TypeError на файлах с неопределяемой длительностью
# (record(duration=None) — живой репро e2e_speakers_smoke 2026-07-16).
# Явный пользовательский opt-in через env уважается (только default).
if "PYANNOTE_METRICS_ENABLED" not in os.environ:
    os.environ["PYANNOTE_METRICS_ENABLED"] = "false"

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
from .gigaam_compat import GIGAAM_SHORTFORM_MAX_SEC, engine_name_from_mode
from .text_diff import TextDiffAnalyzer
from .utils import TextUtils, is_likely_repetition_loop

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

# Module-level flag: emit pipeline_v2 warning only once.
_pipeline_v2_warned: bool = False


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
# W1223 / W1535 security: разрешённые HuggingFace repo для Voxtral loader.
# snapshot_download с произвольным repo_id открывает supply-chain вектор
# (вредоносный вес), DoS через гигантский репозиторий и resource-exhaustion
# через repo с тысячами файлов. Только явно одобренные repo допустимы.
# ---------------------------------------------------------------------------
_VOXTRAL_REPO_ALLOWLIST: frozenset[str] = frozenset({
    "mistralai/Voxtral-Mini-3B-2507",
    "mistralai/Voxtral-Small-24B-2507",
    "mlx-community/Voxtral-Mini-3B-2507-mlx-bf16",
    "mlx-community/Voxtral-Mini-3B-2507-mlx-4bit",
    # Legacy / original Phase 4.4 model retained for backwards compatibility.
    "mistralai/Voxtral-Mini-4B-Realtime-2602",
    "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit",
    "mlx-community/Voxtral-Mini-4B-Realtime-2602-bf16",
})


def _validate_voxtral_repo(repo_id: str) -> str:
    """Проверяет repo_id против _VOXTRAL_REPO_ALLOWLIST.

    Returns:
        repo_id — если он в allowlist.

    Raises:
        ValueError — если repo_id не в allowlist (supply-chain защита).
    """
    if repo_id in _VOXTRAL_REPO_ALLOWLIST:
        return repo_id
    allowed = ", ".join(sorted(_VOXTRAL_REPO_ALLOWLIST))
    raise ValueError(
        f"Voxtral repo не в allowlist: '{repo_id}'. "
        f"Допустимы только: {allowed}"
    )


# ---------------------------------------------------------------------------
# Утилита: проверка доступной памяти macOS через vm_stat
# ---------------------------------------------------------------------------

# Минимум свободной (free + inactive) памяти для загрузки тяжёлых моделей.
# whisper-large-v3-mlx занимает ~3GB, pyannote ~1.5GB. Оставляем запас.
_HEAVY_MODEL_MIN_FREE_GB = 4.0

# TTL (секунд) для записей в _unavailable_models.
# Transient failures (timeout, ImportError at cold start) blacklist adapters only
# for this duration; after expiry the adapter gets one retry automatically.
# Uses time.monotonic() — immune to wall-clock skew / NTP jumps.
_UNAVAILABLE_MODEL_TTL_SEC = 300  # 5 минут
# W1562: backward-compat alias for _UNAVAILABLE_MODEL_TTL_SEC
_UNAVAILABLE_TTL_SEC = _UNAVAILABLE_MODEL_TTL_SEC


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

# --- Гарды вырожденного аудио (живой инцидент 2026-08-12) -------------------
# Окно нулевой длины доехало от live_subs до GigaAM и уронило адаптер на WAV
# без фреймов ("both buffer length (0) and count (-1) must not be 0").

# Ниже этой длины STT физически нечего распознавать: 10 мс при 16 кГц.
# Порог намеренно минимальный — он чинит краш, но не отсекает короткие
# реплики, которые Whisper/GigaAM ещё способны разобрать.
_MIN_TRANSCRIBE_SAMPLES = 160

# 🔴 Запас между внутренним «убийцей» и внешним «сдающимся» (инцидент 03.09.2026).
# На один вызов транскрибации наложены ДВА предела, и прекратить работу умеет
# только внутренний: он убивает subprocess-воркер (threading.Timer в
# MLXWhisperSession._send). Внешний предел (бюджет попытки) умеет лишь перестать
# ждать — executor.shutdown(cancel_futures=True) на запущенную задачу не влияет
# вовсе, поток остаётся жив и держит MLXWhisperSession._lock весь readline().
# Пока внутренний предел был длиннее внешнего (120 с против 25 с), КАЖДОЕ
# зависание оставляло брошенный поток с замком, и следующие запросы — включая
# клип длиной 0.5 с — вставали за ним в очередь до самого fail-fast REST-процесса.
_WORKER_TIMEOUT_GRACE_SEC = 2.0
# Доля бюджета для случая, когда абсолютного запаса не хватает (остаток дедлайна
# меньше самого запаса). Ноль или отрицательное значение здесь недопустимы:
# мгновенный таймаут неотличим от настоящего зависания и убил бы здоровый воркер.
_WORKER_TIMEOUT_MIN_FRACTION = 0.8


def _fit_worker_timeout(setting_sec: float, attempt_timeout_sec: float | None) -> float:
    """Внутренний предел, гарантированно срабатывающий раньше внешнего.

    ``attempt_timeout_sec`` — бюджет попытки, который вызывающий ставит на
    ``future.result``. ``None`` (прямые вызовы без внешнего предела) оставляет
    прежнее поведение настройки.

    Обе ветви одной формулой: для обычных бюджетов работает абсолютный запас,
    для крошечного остатка — доля, которая никогда не даёт неположительное
    число. Щедрый пакетный бюджет не поднимает таймаут выше настройки — ``min``
    оставляет защиту не слабее прежней.

    🔴 Асимметрия с веткой адаптеров ниже (``ADAPTER_MIN_BUDGET_SEC``, §4.8)
    НАМЕРЕННАЯ, не забытое выравнивание. Там ту же коллизию двух пределов решают
    противоположно — ПОДНИМАЮТ внешний, чтобы не бросать subprocess. Разными
    решения делает цена сироты: осиротевший subprocess адаптера держит лишь GPU-
    память и умирает от собственного таймаута, страдает только текущий запрос.
    Осиротевший поток whisper-воркера держит ``MLXWhisperSession._lock``
    синглтона — за ним встают ВСЕ последующие запросы процесса, включая клипы в
    полсекунды. Поэтому здесь внутренний предел опускают, а не внешний поднимают.
    """
    if attempt_timeout_sec is None:
        return setting_sec
    inner = max(
        attempt_timeout_sec - _WORKER_TIMEOUT_GRACE_SEC,
        attempt_timeout_sec * _WORKER_TIMEOUT_MIN_FRACTION,
    )
    return min(setting_sec, inner)


# NoiseProfiler разбивает аудио на фреймы по 2048 сэмплов и на более коротком
# входе возвращает _silent_profile() — то есть snr_db=0.0 без всякого измерения.
_DENOISE_MIN_SAMPLES = 2048

# |SNR| ниже этого значения — не измерение, а sentinel «оценить не смог»:
# ровно 0.0 возвращают _silent_profile() и четыре ветки _compute_snr
# (нулевой signal_rms, слишком короткий сегмент, пустой спектр, пустой floor).
# Даже если 0.0 дБ действительно измерены, деноизить бессмысленно: порог маски
# совпадает с уровнем сигнала, и spectral gating выжигает речь вместе с шумом.
_SNR_UNRELIABLE_EPS_DB = 0.05

# Доля исходной энергии, ниже которой выход деноизера считается разрушенным
# сигналом, а не очищенным (0.01 = −20 дБ). Легитимное подавление даже в режиме
# strong снижает энергию в разы, а не на два порядка.
_DENOISE_MIN_ENERGY_RATIO = 0.01


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


# Бюджет ожидания mlx_lock для НЕОБЯЗАТЕЛЬНОЙ очистки Metal-кэша при смене
# профиля. Сама смена профиля уже произошла к этому моменту; очистка — только
# оптимизация освобождения GPU-буферов. Ждать её дольше нескольких секунд
# бессмысленно и опасно: живой инцидент 2026-08-13 показал, что ожидание
# здесь блокирует финальную транскрибацию диктовки до backstop-таймаута 180с.
# Сознательно больше, чем у превью (1.0с, transcriber.py): смена профиля —
# редкая операция на пути, где результат ждёт пользователь.
MLX_CACHE_FLUSH_LOCK_TIMEOUT_SEC = 2.0


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
        skip_gigaam_warmup: bool = False,
    ) -> None:
        """Инициализирует двигатель, загружая настройки из централизованного конфига.

        Args:
            llm_rewriter: опциональный D.10a LLM клиент для post-cleanup rewrite'а.
                          Если None — LLM hook отключён, работает как до D.10a.
            settings_get: callback (key, default) -> value для runtime toggle'ов.
                          Инжектируется из BackendService чтобы engine не знал про StateStore.
            skip_gigaam_warmup: если True — фоновый warmup GigaAM subprocess пропускается.
                          Используется REST-сервером, который проксирует STT через BackendService
                          IPC и не нуждается в собственном GigaAM worker'е (предотвращает
                          дублирование subprocess'а — Wave 69 fix).
        """
        self.current_model = settings.MODEL_BALANCED
        self.quality_profile = "balanced"
        self._unavailable_models: dict[str, float] = {}
        self._diarization_pipeline: Pipeline | None = None
        self._diarization_load_error: str | None = None
        # Фактически загруженная модель диаризации (может отличаться от
        # settings.DIARIZATION_MODEL при непустом DIARIZATION_MODEL_CANDIDATES).
        self._diarization_active_model: str | None = None
        self._diarization_load_lock: threading.RLock = threading.RLock()
        # C2b: сериализация САМИХ инференсов pyannote (полная диаризация phase C
        # vs DIAR_WINDOW-тик meeting-сессии). Load-lock выше защищает только загрузку.
        self._diarization_run_lock: threading.Lock = threading.Lock()

        # SenseVoice adapter state (lazy-loaded FunASR pipeline).
        # Если funasr не установлен или модель не грузится — адаптер навсегда
        # отключается через _sensevoice_load_error, whisper chain продолжает жить.
        self._sensevoice_model = None  # type: ignore[var-annotated]
        self._sensevoice_load_error: str | None = None
        self._sensevoice_load_lock: threading.RLock = threading.RLock()

        # Parakeet-TDT-1.1B adapter state (lazy-loaded NeMo ASR model).
        # Если nemo не установлен или модель не грузится — адаптер навсегда
        # отключается через _parakeet_load_error, whisper chain продолжает жить.
        self._parakeet_model = None  # type: ignore[var-annotated]
        self._parakeet_load_error: str | None = None
        self._parakeet_load_lock: threading.RLock = threading.RLock()

        # WhisperX adapter state (Phase 4.3, lazy-loaded).
        # Если whisperx не установлен или модель не грузится — адаптер навсегда
        # отключается через _whisperx_load_error, chain продолжает жить.
        self._whisperx_model = None  # type: ignore[var-annotated]
        self._whisperx_load_error: str | None = None
        self._whisperx_load_lock: threading.RLock = threading.RLock()

        # Voxtral Mini 4B adapter state (Phase 4.4, lazy-loaded).
        # W1474 F1: Инициализируется явно в __init__ для double-checked locking.
        self._voxtral_model = None  # type: ignore[var-annotated]
        self._voxtral_load_error: str | None = None
        self._voxtral_load_lock: threading.RLock = threading.RLock()

        # D.10a: LLM rewriter integration
        self._llm_rewriter = llm_rewriter
        self._last_llm_diff = None
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._confidence_calibrator = ConfidenceCalibrator()

        # Language-aware STT router — используется для GigaAM и будущих RU-адаптеров.
        # get_gigaam_adapter() возвращает адаптер если STT_GIGAAM_ENABLED=True и
        # пакет gigaam доступен. При недоступности возвращает None (мягкая деградация).
        from core.stt_router import STTRouter  # noqa: E402 — lazy import to avoid circular
        self._router = STTRouter(settings)

        logger.info(
            "AudioEngine инициализирован. Профиль=%s, Модель=%s, Max Candidates=%d, LLM=%s",
            self.quality_profile,
            self.current_model,
            len(settings.model_max_list),
            "enabled" if llm_rewriter is not None else "disabled",
        )

        # Warmup GigaAM в background если enabled — избегаем cold-start latency
        # на первой диктовке (subprocess spawn + model load = ~30 сек).
        # Wave 525: persist the flag so all later call-sites (chain-building,
        # _transcribe_gigaam) also honour the "no GigaAM here" contract —
        # skip_gigaam_warmup previously only blocked the startup thread, not
        # on-demand adapter creation triggered by real transcription requests.
        self._skip_gigaam: bool = skip_gigaam_warmup

        # skip_gigaam_warmup=True используется REST-сервером чтобы не создавать дубликат
        # subprocess'а — он проксирует через BackendService IPC (Wave 69).
        if getattr(settings, "STT_GIGAAM_ENABLED", False) and not skip_gigaam_warmup:
            def _warmup_bg() -> None:
                try:
                    self._router.warmup_gigaam()
                except Exception as exc:
                    logger.warning("GigaAM warmup в background failed: %s", exc)
            threading.Thread(target=_warmup_bg, name="GigaAM-warmup", daemon=True).start()
            logger.info("GigaAM warmup запущен в background thread")
        elif getattr(settings, "STT_GIGAAM_ENABLED", False) and skip_gigaam_warmup:
            logger.info("GigaAM warmup пропущен (skip_gigaam_warmup=True) — этот engine не spawn'ит worker")

    def unload_stt_models(self) -> None:
        """Отпускает загруженные STT-модели, оставляя движок рабочим.

        Отличие от ``close()``: тот нужен при остановке владельца, а этот —
        для кнопки «выгрузить сейчас» в панели. Кэш адаптера сбрасывается,
        следующая транскрибация поднимет его заново, поэтому вызов безопасен
        в любой момент, кроме идущей записи (её гейтит вызывающая сторона).

        Возникло из просьбы владельца управлять жизнью модели в памяти из
        интерфейса: загрузка (`warmup_stt`) была, выгрузки не было вовсе —
        освободить память можно было только перезапуском бэкенда.
        """
        self._router.close()

    def close(self) -> None:
        """Останавливает фоновые ресурсы движка (живой инцидент 2026-08-04).

        Единственный владелец GigaAM subprocess-воркера, спавненного background-
        warmup-тредом в __init__, — self._router. Без этого вызова процесс
        остаётся сиротой при остановке владельца (Transcriber/BackendService).
        Never raises — вызывается из чужих finally/close-цепочек.
        """
        try:
            self._router.close()
        except Exception:
            logger.warning("AudioEngine.close: ошибка закрытия STTRouter", exc_info=True)
        try:
            from core.mlx_whisper_session import close_mlx_whisper_session

            close_mlx_whisper_session()
        except Exception:
            logger.warning(
                "AudioEngine.close: ошибка закрытия mlx_whisper worker",
                exc_info=True,
            )

    def warmup(self) -> dict[str, Any]:
        """Prewarm Whisper model to eliminate first-dictation cold-start latency.

        Loads the currently active model by running a tiny (1-second silent)
        inference.  The audio buffer is all-zeros — Whisper returns quickly with
        an empty or minimal result, but the model weights are now resident in GPU
        memory so the next real transcription pays no load penalty.

        Must be called in a background thread (MLX is single-threaded / GPU-bound).
        Returns a dict:
          {
            "loaded": bool,      # True if warmup inference succeeded
            "latency_ms": int,   # wall-clock ms for the inference call
            "model_name": str,   # model that was warmed up
            "error": str | None, # error message if loaded=False
          }
        """
        from core.mlx_whisper_session import (
            mlx_whisper_worker_enabled,
            transcribe_via_mlx_worker,
        )

        model_name = self.current_model
        # 1 second of silence at 16 kHz — float32 zeros.
        import numpy as _np
        silent_audio = _np.zeros(16000, dtype=_np.float32)

        import time as _time
        t0 = _time.monotonic()
        try:
            if mlx_whisper_worker_enabled():
                # P0c: warmup в child, иначе REST всё равно грузит Metal в родителе.
                with mlx_inter_process_lock():
                    transcribe_via_mlx_worker(
                        silent_audio,
                        {
                            "path_or_hf_repo": model_name,
                            "language": "ru",
                            "temperature": 0.0,
                            "verbose": False,
                        },
                        timeout_sec=float(
                            getattr(settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 45.0)
                        ),
                        model_name=model_name,
                    )
            elif mlx_whisper is None:
                return {
                    "loaded": False,
                    "latency_ms": 0,
                    "model_name": "",
                    "error": "mlx_whisper not available",
                }
            else:
                with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock + intra-process RLock
                    mlx_whisper.transcribe(
                        silent_audio,
                        path_or_hf_repo=model_name,
                        language="ru",
                        temperature=0.0,
                        verbose=False,
                    )
            latency_ms = int((_time.monotonic() - t0) * 1000)
            logger.info("STT warmup завершён: модель=%s, latency=%dms", model_name, latency_ms)
            return {"loaded": True, "latency_ms": latency_ms, "model_name": model_name, "error": None}
        except MLXInterLockTimeout as exc:
            logger.error("STT warmup: mlx_inter_lock timeout — %s", exc)
            return {"loaded": False, "latency_ms": 0, "model_name": model_name, "error": str(exc)}
        except Exception as exc:
            latency_ms = int((_time.monotonic() - t0) * 1000)
            logger.warning("STT warmup не удался (модель=%s): %s", model_name, exc)
            return {"loaded": False, "latency_ms": latency_ms, "model_name": model_name, "error": str(exc)}

    def _llm_rewrite_allowed(self) -> bool:
        """Runtime check: включён ли LLM rewriter И user runtime toggle.

        Returns False when privacy_mode_enabled=True to prevent sending text
        to an external LLM service (W1229 F3 MED fix).
        """
        if self._llm_rewriter is None:
            return False
        if self._settings_get("privacy_mode_enabled", False):
            return False
        return bool(self._settings_get("llm_rewrite_enabled", False))

    def _punctuation_pass_allowed(self) -> bool:
        """Runtime check: включён ли punctuation-only LLM pass.

        W1755 defense-in-depth: mirrors _llm_rewrite_allowed — blocks when privacy_mode_enabled
        so that even if the LLMRewriter._settings_getter wiring is ever lost again, the engine
        itself will not invoke fix_punctuation_only() and exfiltrate transcript text to LM Studio.
        """
        if self._llm_rewriter is None:
            return False
        if self._settings_get("privacy_mode_enabled", False):
            return False
        return bool(self._settings_get("stt_punctuation_llm_pass_enabled", False))

    def _cloud_rewrite_allowed(self) -> bool:
        """Runtime check: включён ли cloud rewriter И не в режиме конфиденциальности.

        Privacy gate: privacy_mode_enabled=True ВСЕГДА возвращает False —
        транскрипт не должен покидать устройство в режиме приватности.
        """
        if self._settings_get("privacy_mode_enabled", False):
            return False
        return bool(self._settings_get("cloud_rewriter_enabled", False))

    def _remote_stt_retry_configured(self) -> bool:
        """Проверяет ключ выбранного cloud STT до добавления retry-кандидата.

        Неизвестный provider намеренно пропускается дальше: `_transcribe_remote`
        сохранит прежний явный RuntimeError о неверной конфигурации. Для известного
        provider пустой ключ — штатное состояние конфигурации, а не ошибка горячего
        STT-пути, поэтому remote retry даже не запускается.
        """
        from backend.cloud_stt import has_cloud_stt_api_key  # noqa: PLC0415

        provider_name = str(
            self._settings_get("cloud_stt_provider", "openai") or "openai"
        ).lower()
        try:
            has_api_key = has_cloud_stt_api_key(provider_name)
        except Exception as exc:
            logger.warning(
                "[STT] remote retry key preflight failed for provider=%s: %s; "
                "preserving existing retry path",
                provider_name,
                exc,
                extra={"provider": provider_name, "reason": "key_preflight_failed"},
            )
            return True
        if has_api_key is None:
            return True
        if has_api_key:
            return True

        logger.debug(
            "[STT] skip remote retry: provider=%s API key is not configured",
            provider_name,
            extra={"provider": provider_name, "reason": "no_api_key"},
        )
        return False

    def _is_model_unavailable(self, model_id: str) -> bool:
        """Проверяет, заблокирован ли адаптер/модель в _unavailable_models с учётом TTL.

        Если запись есть, но TTL истёк (now - timestamp >= _UNAVAILABLE_MODEL_TTL_SEC),
        запись вычищается и возвращается False — адаптер получает шанс на retry.
        Использует time.monotonic() — устойчив к NTP-коррекциям и скачкам системных часов.
        """
        ts = self._unavailable_models.get(model_id)
        if ts is None:
            return False
        if time.monotonic() - ts >= _UNAVAILABLE_MODEL_TTL_SEC:
            # TTL истёк — убираем запись, адаптер снова доступен
            self._unavailable_models.pop(model_id, None)
            return False
        return True

    def _blacklist_allowed_for(self, exc: BaseException, *, is_adapter: bool = False) -> bool:
        """§4.7 (спека 2026-08-26), уточнено финальным гейтом волны (находка 1):
        можно ли писать модель в _unavailable_models по этому исключению.

        Любое НЕ-таймаутное исключение (MLX watchdog, крах воркера, OOM)
        блэклист заслуживает независимо от ветки — проверяется первым.

        Дальше ветки РАСХОДЯТСЯ по источнику многоминутного ожидания:

        - whisper-каскад и multipass-ретраи (is_adapter=False, дефолт):
          единственный НЕОГРАНИЧЕННЫЙ источник ожидания здесь — очередь за
          внутрипроцессным mlx_lock() (общий GPU-лок с любой конкурентной
          операцией, например часовым импортом), а НЕ зависший инференс.
          Настоящее зависание ловит собственный watchdog MLX и приходит
          отдельным типом (MLXTimeoutError, RuntimeError-наследник — не
          матчится этой проверкой, у него своя ветка except, блэклистит
          всегда). Проверка через budget_exhausted() бюджета ЗАПРОСА здесь
          не годится: попытка истекает по СВОЕМУ бюджету (104-180с — в разы
          меньше бюджета запроса), так что запрос почти всегда ещё "жив" —
          инцидент 2026-08-26 показал именно это: 4.71с аудио держали общий
          GPU-лок в очереди позади часового импорта и уходили в блэклист,
          хотя обе модели были полностью здоровы. Поэтому здесь TimeoutError
          НИКОГДА не блэклистит — сигнал попросту неотличим от очереди.
        - adapter-ветка (is_adapter=True: GigaAM/Parakeet/SenseVoice/
          WhisperX/Voxtral) — другой контракт: внешний таймаут там floor'ится
          ADAPTER_MIN_BUDGET_SEC (200с), заведомо выше внутренних таймаутов
          subprocess (120с shortform / 180с load) — сработавший внешний
          таймаут означает, что subprocess не уложился даже в собственный
          лимит, законный сигнал нездоровья; решает по остатку дедлайна
          ЗАПРОСА, как и раньше (stt_budget.timeout_blacklist_allowed()).
        """
        # 🔴 Ожидание ОЧЕРЕДИ за GPU — не отказ движка (волна 2026-08-29).
        # MLXLockTimeoutError означает «лок держит сосед» (превью, импорт,
        # смена профиля), а сам движок здоров и GPU даже не трогал. Блэклист за
        # это выбивает рабочий GigaAM на 300 с и отправляет следующую диктовку
        # в облако, которого нет, — тот же дефект, что разбирала спека #1956,
        # только в adapter-ветке, куда он попадает как наследник TimeoutError.
        if isinstance(exc, MLXLockTimeoutError):
            return False
        if not isinstance(exc, (TimeoutError, concurrent.futures.TimeoutError)):
            return True
        if is_adapter:
            return stt_budget.timeout_blacklist_allowed()
        return False

    def _push_error(self, code: str, message_debug: str, severity: str | None = None) -> None:
        """Push KrabError to attached ErrorBus if available. Late-injected attribute.

        Never raises — production paths must not break due to error reporting.
        Phase B.2: called from stt chain / diarization / mlx oom paths.
        """
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get(code, {})
            component = code.split(".")[0] if "." in code else "stt"
            err = KrabError(
                severity=severity or entry.get("severity", "warn"),
                component=component,
                code=code,
                message_user=entry.get("user_msg_ru", "STT ошибка"),
                message_debug=message_debug,
                timestamp=datetime.now(timezone.utc),
                context={"model": self.current_model, "profile": self.quality_profile},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception as e:  # noqa: BLE001
            # Wave 222: surface push failures to Sentry instead of silent swallow
            try:
                from backend.observability import capture_exception
                capture_exception(e, "_push_error_internal")
            except Exception:
                pass  # Sentry itself failing — stay silent
            logger.exception("error_bus.push failed for code=%s", code)

    # ------------------------------------------------------------------
    # Phase B.2 F11 — worker subprocess OOM detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_subprocess_oom(returncode: int, stderr: str) -> tuple[bool, str | None]:
        """Return (is_oom, signal_name) for subprocess exit.

        is_oom: True if returncode or stderr indicates OOM/fatal MLX crash.
        signal_name: Human-readable signal (e.g. 'SIGABRT', 'SIGKILL',
                     'SIGSEGV', 'SIGBUS') OR 'stderr_oom_pattern' OR None.

        Delegates to core.pipeline.stt_gigaam.detect_subprocess_oom.
        """
        from core.pipeline.stt_gigaam import detect_subprocess_oom
        return detect_subprocess_oom(returncode, stderr)

    def _push_mlx_oom_for_worker(self, name: str, rc: int, stderr: str) -> None:
        """Push mlx.oom KrabError for a crashed worker subprocess.

        Called after _detect_subprocess_oom returns True. Never raises.
        Includes signal_name in message_debug for Sentry grouping.
        """
        try:
            from core.pipeline.stt_gigaam import detect_subprocess_oom
            _is_oom, signal_name = detect_subprocess_oom(rc, stderr)
            stderr_tail = (stderr or "")[-200:]
            self._push_error(
                "mlx.oom",
                f"worker={name} returncode={rc} signal={signal_name} stderr_tail={stderr_tail!r}",
                severity="critical",
            )
        except Exception:
            pass  # helper must never raise

    def preview_needs_whisper_profile(self) -> bool:
        """Нужен ли превью whisper-профиль (и стоит ли ради него чистить MLX-кэш).

        🔴 Живой инцидент 01.09.2026. Превью звало ``set_quality_profile("balanced")``
        безусловно, а тот делает ``mx.clear_cache()`` — то есть ВЫБРАСЫВАЕТ
        загруженную whisper-модель. При ``quality_profile=max`` финальная
        транскрипция после каждой такой чистки перезагружала
        whisper-large-v3 (~3 ГБ) и не укладывалась в бюджет
        ``3 × длительность`` (93–97с на 31–34с речи), падала с «Все доступные
        STT-движки вышли из строя», и владелец получал текст из накопителя
        превью — медленно и заметно хуже качеством. Каскад
        самоподдерживающийся: пока идёт 90-секундная финальная транскрипция,
        превью СЛЕДУЮЩЕЙ диктовки голодает на том же локе.

        Превью идёт ``single_pass=True`` — фоллбэк-цепочки у него НЕТ. Значит
        whisper-профиль важен ему ровно тогда, когда whisper окажется ПЕРВЫМ
        движком. Для русского первым идёт GigaAM (условие ниже зеркалит
        ``_transcribe_with_fallback_impl``), и тогда чистить кэш незачем.

        Консервативно: при недоступном GigaAM возвращаем True — прежнее
        поведение сохраняется, превью снова получает лёгкую модель.
        """
        try:
            gigaam_ready = (
                bool(getattr(settings, "STT_GIGAAM_ENABLED", False))
                and not self._is_model_unavailable(self._GIGAAM_MARKER)
                and not getattr(self, "_skip_gigaam", False)
            )
        except Exception:
            return True
        return not gigaam_ready

    def set_quality_profile(self, profile: str) -> bool:
        """Переключает профиль качества (balanced или max)."""
        clean_profile = profile.strip().lower()
        if clean_profile not in {"balanced", "max"}:
            clean_profile = "balanced"

        new_model = settings.MODEL_BALANCED if clean_profile == "balanced" else settings.model_max_list[0]

        if clean_profile == self.quality_profile and new_model == self.current_model:
            return False

        # 🔴 Модель могла НЕ измениться: у владельца (01.09.2026)
        # MODEL_BALANCED и model_max_list[0] оба указывают на
        # whisper-large-v3-mlx, поэтому «смена профиля» balanced↔max меняла
        # только строку профиля. Гард выше требует совпадения ОБОИХ полей и
        # потому не срабатывал, а очистка кэша ниже выбрасывала ровно ту
        # модель, которая сейчас снова понадобится, — и следующая
        # транскрипция грузила те же 3 ГБ заново. Дважды на каждую диктовку.
        _model_changed = new_model != self.current_model
        logger.info(
            "Смена профиля STT: %s -> %s (модель: %s%s)",
            self.quality_profile, clean_profile, new_model,
            "" if _model_changed else ", модель не менялась — кэш сохранён",
        )
        self.quality_profile = clean_profile
        self.current_model = new_model
        # H2: при смене профиля balanced↔max старая модель выгружается из MLX.
        # Явный flush Metal cache освобождает GPU буферы немедленно, не дожидаясь GC.
        # W1618/W63: clear_cache — MLX op, must hold mlx_lock to prevent concurrent SIGSEGV.
        # W1635: degrade_on_timeout=True — non-critical cache flush, not inference.
        # 2026-08-13 (живой инцидент, диктовка владельца потеряна): внутрипроцессный
        # mlx_lock брался БЕЗ таймаута, хотя соседний межпроцессный в той же строке
        # уже деградировал (degrade_on_timeout=True) — асимметрия соседних гейтов.
        # Пока превью держало лок повисшим под нехваткой памяти Whisper'ом,
        # финальная транскрибация стояла ЗДЕСЬ — на необязательной очистке кэша —
        # до backstop-таймаута 180с, после чего агент убивал бэкенд.
        # Поля профиля выставлены ВЫШЕ, поэтому пропуск очистки безопасен.
        # Чистить кэш имеет смысл ТОЛЬКО когда действительно сменилась модель:
        # flush существует, чтобы освободить буферы СТАРОЙ модели. При той же
        # модели он лишь выбрасывает нужное и оплачивается перезагрузкой.
        #
        # 🔴 Ранний return, а не тернарник на mlx_lock(): source-контракт W1618
        # (test_engine_mlx_lock_clear_cache_W1618) распознаёт AST-разбором ровно
        # две формы захвата — `with mlx_lock():` и `lk = mlx_lock()` простым
        # присваиванием. `mlx_lock() if cond else None` инвариант не нарушает,
        # но гард его НЕ видит и краснеет. Сохраняем форму, понятную гарду,
        # вместо того чтобы ослаблять сам гард.
        if not _model_changed:
            return True

        _cache_lock = mlx_lock()
        if _cache_lock.acquire(timeout=MLX_CACHE_FLUSH_LOCK_TIMEOUT_SEC):
            try:
                import mlx.core as _mx
                with mlx_inter_process_lock(degrade_on_timeout=True):  # W1635
                    _mx.clear_cache()
            except (ImportError, AttributeError):
                pass  # MLX не установлен или старая версия без clear_cache
            finally:
                _cache_lock.release()
        else:
            logger.debug(
                "Смена профиля: GPU занят дольше %.1fс — очистка Metal-кэша пропущена",
                MLX_CACHE_FLUSH_LOCK_TIMEOUT_SEC,
            )
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

    @staticmethod
    def _empty_transcription_result(engine: str, language: str | None) -> dict[str, Any]:
        """Пустой результат транскрибации в контракте обычного ответа.

        Единственный источник схемы для всех ранних возвратов «распознавать
        нечего» (VAD отфильтровал тишину, вырожденное аудио): потребители
        читают эти поля напрямую, и разъезд двух копий словаря обернулся бы
        KeyError уже в проде.

        ``reason`` (2026-08-19) дублирует значение ``engine`` отдельным явным
        ключом: у обоих вызывающих мест ``engine`` на деле несёт не имя STT-
        движка, а причину пустого результата ("empty_audio" / "vad_skip") —
        переименовывать/трогать сам ``engine`` этой волной не стали (риск для
        потребителей, читающих его как есть). ``stt_engine`` добавлен как
        честный контрактный ключ: на раннем возврате STT ещё не запускался,
        поэтому его значение ``None``. REST подставляет реальное имя движка
        только для обычного результата (см. backend/rest_server.py::transcribe_audio).
        """
        return {
            "text": "", "raw_text": "", "cleaned_text": "",
            "llm_applied": False, "llm_latency_ms": None,
            "llm_fallback_reason": None, "llm_diff": None,
            "confidence": 0.0, "raw_confidence": 0.0,
            "confidence_adjustments": [], "duration_ms": 0,
            "engine": engine, "model": None,
            "stt_engine": None,
            "language": language, "segments": [],
            "diarization": None, "emotion": None,
            "reason": engine,
        }

    @staticmethod
    def _audio_energy(audio: np.ndarray) -> float:
        """Суммарная энергия сигнала (Σx²) в float64 — устойчиво к dtype входа."""
        arr = np.asarray(audio, dtype=np.float64).ravel()
        if arr.size == 0:
            return 0.0
        return float(np.dot(arr, arr))

    @classmethod
    def _guard_denoised(cls, original: np.ndarray, denoised: Any) -> np.ndarray:
        """Отвергает выход деноизера, потерявший сигнал (инцидент 2026-08-12).

        Проверяются три вида порчи: не-массив/пустой результат, изменившаяся
        форма (сдвинет таймстемпы Whisper; сюда же попадает моно-даунмикс
        многоканального входа — в STT-пути аудио всегда моно) и обвал энергии
        ниже ``_DENOISE_MIN_ENERGY_RATIO`` от исходной.

        Направление отказа выбрано намеренно: распознать шумное аудио лучше,
        чем не распознать ничего, поэтому при любом сомнении возвращается
        ИСХОДНЫЙ массив, а не результат обработки.
        """
        if not isinstance(denoised, np.ndarray) or denoised.size == 0:
            logger.warning(
                "[STT] denoising вернул пустой результат (%s) — используем исходное аудио",
                type(denoised).__name__,
            )
            return original

        if denoised.shape != original.shape:
            logger.warning(
                "[STT] denoising изменил форму аудио %s → %s — используем исходное аудио",
                original.shape, denoised.shape,
            )
            return original

        orig_energy = cls._audio_energy(original)
        if orig_energy <= 0.0:
            # Исходное аудио само по себе тишина — терять нечего, но и
            # обрабатывать нечего: отдаём вход без изменений.
            return original

        ratio = cls._audio_energy(denoised) / orig_energy
        if ratio < _DENOISE_MIN_ENERGY_RATIO:
            logger.warning(
                "[STT] denoising срезал энергию до %.4f от исходной (< %.4f) — "
                "используем исходное аудио",
                ratio, _DENOISE_MIN_ENERGY_RATIO,
            )
            return original

        return denoised

    def _maybe_denoise(self, audio: np.ndarray) -> np.ndarray:
        """Проверяет SNR и применяет шумоподавление при необходимости.

        Использует NoiseProfiler для оценки SNR. Если SNR < порога из настроек →
        запускает AudioDenoiser с заданной силой. Возвращает (возможно обработанный)
        аудиомассив той же dtype и формы.

        Деноизинг пропускается в двух случаях, когда оценка SNR недостоверна:
        аудио короче окна NoiseProfiler и |SNR| ≈ 0 (sentinel «оценить не смог» —
        см. ``_SNR_UNRELIABLE_EPS_DB``). Результат обработки проходит через
        ``_guard_denoised`` — деноизер не имеет права вернуть в пайплайн пустое
        или выжженное аудио.

        Исключения внутри не должны ломать транскрибацию — ловим и логируем.
        """
        try:
            from core.noise_profiler import NoiseProfiler
            from core.audio_denoiser import AudioDenoiser

            sample_rate = 16000  # mlx-whisper ожидает 16 кГц

            n_samples = int(np.asarray(audio).size)
            if n_samples < _DENOISE_MIN_SAMPLES:
                # NoiseProfiler на таком входе вернёт _silent_profile() с
                # snr_db=0.0 — «денойзить» по этой цифре значит верить
                # несуществующему измерению.
                logger.warning(
                    "[STT] аудио %d сэмплов (< %d) — оценка SNR недостоверна, "
                    "denoising пропущен",
                    n_samples, _DENOISE_MIN_SAMPLES,
                )
                return audio

            profile = NoiseProfiler().profile(audio, sample_rate)
            snr = profile.snr_db
            threshold = settings.STT_DENOISE_SNR_THRESHOLD_DB
            strength = settings.STT_DENOISE_STRENGTH

            if abs(snr) < _SNR_UNRELIABLE_EPS_DB:
                logger.warning(
                    "[STT] noise SNR=%.2f dB — оценка недостоверна (sentinel "
                    "NoiseProfiler), denoising пропущен",
                    snr,
                )
                return audio

            if snr < threshold:
                logger.info(
                    "[STT] noise SNR=%.1f dB < %.1f dB → denoising applied (strength=%s)",
                    snr, threshold, strength,
                )
                denoised = AudioDenoiser().denoise(audio, sample_rate, strength=strength)  # type: ignore[arg-type]
                return self._guard_denoised(audio, denoised)
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
        history_context: list[Any] | None = None,
        stt_hotwords: list[str] | None = None,
        silence_ranges: list[tuple[float, float]] | None = None,
        diarize: bool | None = None,
        skip_vad_prefilter: bool = False,
        context_free: bool = False,
        single_pass: bool = False,
    ) -> dict[str, Any]:
        """Основной метод распознавания речи. Поддерживает динамические промпты и доменные подсказки.

        Args:
            lang_hint: ISO 639-1 код языка ("ru", "es", "en") или None/"auto" для
                       автоопределения whisper'ом. По умолчанию берётся из конфига (settings.TRANSCRIBE_LANGUAGE).
            progress_callback: Опциональный колбэк для отчёта о прогрессе. Вызывается с именем
                       этапа ("audio_load", "normalize", "stt", "cleanup", "diarize", "llm_rewrite").
                       Исключения внутри колбэка подавляются — отчёт о прогрессе не должен ломать
                       транскрибацию.
            history_context: Последние элементы истории (HistoryItem или dict) для построения
                       initial_prompt. Передаются из BackendService; engine не знает про StateStore.
                       None / [] → контекст не используется.
            stt_hotwords: Пользовательские термины для Glossary-префикса в initial_prompt.
                       None / [] → Glossary-блок не добавляется.
            silence_ranges: Диапазоны тишины (start_sec, end_sec) от RealtimeSilenceFilter.
                       Семплы в этих диапазонах будут обнулены перед STT (не удалены — таймстемпы сохранены).
                       Не применяется для preview-транскрибации.
            context_free: 2026-08-12, живой инцидент — live-субтитры чужого YouTube-видео
                       показали «Сохраняй смысл 0 тяги»: TRANSCRIBE_PROMPT (инструкция для
                       Whisper) утёк в распознанный текст, потому что live subs флашит
                       каждые ~3s — то же "короткие буферы", от которых уже защищён preview
                       path. True → initial_prompt формируется пустым, БЕЗ остальных трёх
                       эффектов is_preview (диаризация/loop-детектор/LLM-passes остаются
                       включены — live subs не preview, а полноценный путь STT).
            single_pass: 2026-08-12, живой инцидент — окно live-субтитров длиной 2.5с
                       прошло GigaAM (пусто) → whisper-large-v3 (conf 0.61, retry) →
                       whisper-large-v3-turbo (retry) = 9.49с на окно, которое приходит
                       каждые ~3с. True → отключает ДВА прохода, спроектированных для
                       диктовки, а не для потока: (1) confidence-driven multi-pass retry
                       (_maybe_multipass_retry) не выполняется; (2) request-local fallback
                       на Whisper после пустого успешного результата GigaAM не выполняется —
                       что первый движок в chain вернул, то и есть финальный результат,
                       включая пустоту. Путь диктовки (single_pass=False, по умолчанию)
                       не меняется — там ретрай по уверенности оправдан.
        """
        def _report(stage: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(stage)
                except Exception:
                    pass

        start_time = time.time()

        # --- Гард вырожденного аудио (живой инцидент 2026-08-12) ---
        # Окно нулевой длины из live_subs проходило весь препроцессинг и падало
        # уже в STT-адаптере (GigaAM: WAV без фреймов → ValueError в np.frombuffer).
        # Отсекаем здесь, ДО pipeline_v2 и остальных веток, возвращая пустой
        # результат по тому же контракту, что и vad_skip.
        if (
            isinstance(audio_data, np.ndarray)
            and audio_data.size < _MIN_TRANSCRIBE_SAMPLES
        ):
            logger.warning(
                "transcribe: вырожденное аудио (%d сэмплов < %d) — STT пропущен",
                audio_data.size, _MIN_TRANSCRIBE_SAMPLES,
            )
            _empty_lang = (
                self._resolve_language(lang_hint)
                if lang_hint is not None
                else settings.TRANSCRIBE_LANGUAGE
            )
            return self._empty_transcription_result("empty_audio", _empty_lang)

        # --- MAX_AUDIO_MB guard (MED wave-26 DoS fix) ---
        # Must run BEFORE pipeline_v2 early-return so that oversized files are
        # rejected on both the pipeline_v2 path and the legacy path.  The legacy
        # path repeats the same check at step 3 (defence-in-depth).
        if isinstance(audio_data, (str, Path)) and os.path.exists(str(audio_data)):
            _early_size_mb = os.path.getsize(str(audio_data)) / _BYTES_PER_MB
            _max_mb = self._settings_get("max_audio_mb", 1000)
            try:
                _max_mb = float(_max_mb)
            except (TypeError, ValueError):
                _max_mb = 1000
            if _early_size_mb > _max_mb:
                raise ValueError(
                    f"Файл слишком большой: {_early_size_mb:.1f}MB > {_max_mb}MB"
                )

        # --- pipeline_v2 opt-in gate (W1263 F1) ---
        _pipeline_v2_enabled = False
        try:
            _pipeline_v2_enabled = bool(
                getattr(settings, "PIPELINE_V2_ENABLED", None)
                if getattr(settings, "PIPELINE_V2_ENABLED", None) is not None
                else getattr(settings, "PIPELINE_V2", False)
            )
        except Exception:
            pass
        if _pipeline_v2_enabled:
            global _pipeline_v2_warned
            if not _pipeline_v2_warned:
                logger.warning("pipeline_v2 EXPERIMENTAL — Phase 4 deterministic pipeline activated.")
                _pipeline_v2_warned = True
            try:
                from core.pipeline.bridge import transcribe_v2 as _transcribe_v2
                return _transcribe_v2(
                    engine=self, audio_input=audio_data,
                    llm_rewriter=getattr(self, "_llm_rewriter", None),
                    translator=getattr(self, "_translator", None),
                    cleanup_profile=cleanup_profile, is_preview=is_preview,
                    domain=domain, extra_vocabulary=extra_vocabulary, lang_hint=lang_hint,
                )
            except Exception as _v2_exc:
                logger.warning("pipeline_v2 failed (%s), falling back", _v2_exc)

        resolved_lang = self._resolve_language(lang_hint) if lang_hint is not None else settings.TRANSCRIBE_LANGUAGE

        try:
            from backend.observability import add_breadcrumb as _add_bc  # lazy — avoid circular
            _add_bc(
                category="transcription",
                message="transcribe_start",
                level="info",
                data={
                    "cleanup_profile": cleanup_profile,
                    "is_preview": is_preview,
                    "domain": domain,
                    "lang_hint": lang_hint or "auto",
                    "diarize": diarize,
                },
            )
        except Exception:
            pass  # telemetry must never break transcription

        # 1. Формирование динамического промпта.
        # Preview path идёт с пустым prompt'ом: короткие аудиобуферы (<3s)
        # провоцируют whisper на "leakage" initial_prompt'а в output как
        # артефакта. Финальный stop_recording по-прежнему использует полный
        # TRANSCRIBE_PROMPT для пунктуации/брендов/имён. Defense-in-depth:
        # _postprocess_preview_text в service.py срезает известные фрагменты
        # промпта как safety net.
        # context_free (2026-08-12, живой инцидент — «Сохраняй смысл 0 тяги» в
        # live-субтитрах чужого YouTube-видео): live subs флашит каждые ~3s,
        # то есть та же «короткие буферы» ситуация, что и у preview, ПЛЮС
        # инструкция и история/hotwords владельца вредны для чужого системного
        # звука (смещают распознавание в лексику диктовок владельца). Не
        # переиспользуем is_preview: тот дополнительно гейтит диаризацию/
        # loop-детектор/LLM-passes, которые live subs использует как обычно —
        # навесить на is_preview значило бы получить три незапрошенных
        # изменения поведения.
        if is_preview or context_free:
            dynamic_prompt = ""
        else:
            domain_desc = self.DOMAIN_PROMPTS.get(domain, self.DOMAIN_PROMPTS["casual"])
            dynamic_prompt = f"{settings.TRANSCRIBE_PROMPT} Тематика: {domain_desc}"
            if extra_vocabulary:
                dynamic_prompt += f" Ключевые слова: {', '.join(extra_vocabulary)}"
            # Обогащаем prompt контекстом недавней истории и пользовательскими hotwords.
            # build_initial_prompt возвращает пустую строку если нет ни контекста, ни hotwords.
            # Языковой словарь подключается только для своего языка: испанские
            # медицинские термины бесполезны в русской диктовке, а место в
            # промпте выкупают у контекста истории (бюджет 224 токена уже
            # режется — см. лог «initial_prompt truncated»).
            _lang_hotwords = {
                "ru": list(getattr(settings, "STT_HOTWORDS_RU", []) or []),
                "es": list(getattr(settings, "STT_HOTWORDS_ES", []) or []),
                "en": list(getattr(settings, "STT_HOTWORDS_EN", []) or []),
            }
            context_suffix = build_initial_prompt(
                history_items=history_context or [],
                hotwords=merge_language_hotwords(
                    stt_hotwords or [], resolved_lang, _lang_hotwords
                ),
            )
            if context_suffix:
                dynamic_prompt = f"{context_suffix} {dynamic_prompt}"

            # Добавляем speaker-aware dialogue hint если включено и спикеров ≥ threshold.
            _diarize_for_prompt = diarize if diarize is not None else settings.DIARIZATION_ENABLED
            if settings.STT_SPEAKER_AWARE_PROMPT_ENABLED and _diarize_for_prompt:
                try:
                    _speaker_cache: dict[str, Any] = {}
                    num_spk = self._estimate_num_speakers(audio_data, cache=_speaker_cache)
                    if num_spk is not None and num_spk >= settings.STT_DIALOGUE_HINT_THRESHOLD:
                        spk_hint = self._build_speaker_context_prompt(num_spk, resolved_lang)
                        if spk_hint:
                            dynamic_prompt = f"{dynamic_prompt}\n{spk_hint}"
                except Exception as _spk_exc:
                    logger.debug("Speaker-aware prompt: оценка не удалась, пропускаем: %s", _spk_exc)

        # 1.9 Диаризованный конвейер (W-C, opt-in): ТОЛЬКО файловые входы —
        # живые диктовки (ndarray/bytes) не попадают сюда никогда. Ранний
        # return до rewrite/cleanup/paste: спикер-транскрипт не должен идти
        # в LLM-переписывание и автовставку. Не зависит от STT_STREAMING_ENABLED.
        if (
            not is_preview
            and getattr(settings, "DIARIZED_TRANSCRIPTION_ENABLED", False)
            and isinstance(audio_data, (str, Path))
            and os.path.exists(str(audio_data))
        ):
            _diar_duration: float | None = None
            try:
                import soundfile as _sf_diar
                _diar_duration = _sf_diar.info(str(audio_data)).duration
            except Exception:
                pass
            if (
                _diar_duration is not None
                and _diar_duration > settings.DIARIZED_MIN_DURATION_SEC
            ):
                try:
                    _spk_cache: dict[str, Any] = {}
                    _num_spk = self._estimate_num_speakers(audio_data, cache=_spk_cache)
                except Exception:
                    _num_spk = None
                if _num_spk is not None and _num_spk <= settings.DIARIZED_MAX_SPEAKERS:
                    from core.diarized_transcription import run_diarized_transcription
                    try:
                        return run_diarized_transcription(
                            self, audio_data, language=lang_hint,
                        )
                    except Exception as _diar_exc:
                        # Soft-fail: любой сбой конвейера → обычный путь ниже.
                        logger.warning(
                            "Diarized pipeline failed (%s) — обычный путь",
                            str(_diar_exc)[:200],
                        )

        # 2. Routing: chunked path для длинных записей (если включён)
        # Для numpy-буферов определяем длительность через len/sample_rate.
        # Для файлов — через soundfile.info (lazy import, мягкий fallback).
        if not is_preview and settings.STT_STREAMING_ENABLED:
            audio_duration_hint: float | None = None
            if isinstance(audio_data, np.ndarray):
                audio_duration_hint = len(audio_data) / 16000.0
            elif isinstance(audio_data, (str, Path)) and os.path.exists(str(audio_data)):
                try:
                    import soundfile as _sf_hint
                    audio_duration_hint = _sf_hint.info(str(audio_data)).duration
                except Exception:
                    pass
            if (
                audio_duration_hint is not None
                and audio_duration_hint > settings.STT_STREAMING_MIN_AUDIO_SEC
            ):
                return self.transcribe_chunked(
                    audio_data,
                    sample_rate=16000,
                    chunk_sec=settings.STT_STREAMING_CHUNK_SEC,
                    overlap_sec=settings.STT_STREAMING_OVERLAP_SEC,
                    cleanup_profile=cleanup_profile,
                    domain=domain,
                    extra_vocabulary=extra_vocabulary,
                    lang_hint=lang_hint,
                    progress_callback=progress_callback,
                )

        # 3. Проверка лимитов и iCloud workaround для файлов
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
            # 2.4 Адаптивное шумоподавление (применяется только к numpy-массивам,
            # т.е. к живым записям; файловые импорты пропускаются для скорости).
            # ВАЖНО: Denoiser должен видеть исходный audio ДО обнуления RSF-диапазонов,
            # чтобы шумовой профиль строился по реальному ambient-шуму, а не по нулям.
            if (
                settings.STT_DENOISE_ENABLED
                and not is_preview
                and isinstance(audio_data, np.ndarray)
            ):
                audio_data = self._maybe_denoise(audio_data)

            # 2.5 Обнуление диапазонов тишины от RealtimeSilenceFilter.
            # Семплы обнуляются (не удаляются) — таймстемпы Whisper сохраняются.
            # RSF-маска применяется к очищенному сигналу (после деноизера).
            if silence_ranges and isinstance(audio_data, np.ndarray) and not is_preview:
                try:
                    from backend.realtime_silence_filter import zero_silence_ranges as _zero_sr
                    audio_data = _zero_sr(audio_data, silence_ranges, sample_rate=16000)
                    logger.debug(
                        "transcribe: silence_ranges применены (%d диапазонов)",
                        len(silence_ranges),
                    )
                except Exception:
                    logger.debug("transcribe: ошибка применения silence_ranges, пропускаем")

            # 2.6 Нормализация усиления (GainNormalizer.auto_gain).
            # Выравнивает тихие записи до -20 дБFS RMS, ограничивает громкие
            # через soft-knee limiter. При ошибке — продолжаем с оригинальным аудио.
            if (
                settings.STT_GAIN_NORMALIZE_ENABLED
                and not is_preview
                and isinstance(audio_data, np.ndarray)
            ):
                try:
                    from core.gain_normalizer import GainNormalizer
                    _gain_result = GainNormalizer().auto_gain(audio_data)
                    if isinstance(_gain_result, np.ndarray):
                        audio_data = _gain_result
                except Exception:
                    logger.debug("GainNormalizer.auto_gain failed — продолжаем без нормализации")

            # 2.7 Удаление длинных внутренних пауз (SmartSilenceSkipper).
            # Убирает долгие тихие участки внутри аудио до STT — уменьшает
            # шанс галлюцинаций Whisper на длинных паузах и ускоряет транскрибацию.
            # Default OFF: SMART_SILENCE_SKIP_ENABLED=False.
            # W1096 F3: если SmartSilenceSkipper активен — VAD prefilter пропускается
            # (mutex, чтобы избежать двойного сдвига временных меток).
            _smart_silence_active = False
            if (
                settings.SMART_SILENCE_SKIP_ENABLED
                and not is_preview
                and isinstance(audio_data, np.ndarray)
            ):
                try:
                    from core.smart_silence_skipper import SmartSilenceSkipper
                    _skip_result = SmartSilenceSkipper().process(audio_data, 16000)
                    audio_data = _skip_result.processed_audio
                    _smart_silence_active = True
                    logger.debug(
                        "SmartSilenceSkipper: %.2fs → %.2fs (удалено %.2fs тишины)",
                        _skip_result.original_duration_sec,
                        _skip_result.processed_duration_sec,
                        _skip_result.original_duration_sec - _skip_result.processed_duration_sec,
                    )
                except Exception:
                    logger.exception("smart_silence_skipper: failed, continuing with original audio")

            # 3. Вызов распознавания с механизмом деградации (fallback)
            _report("stt")

            # VAD pre-filter: убираем длинные паузы ДО Whisper → меньше галлюцинаций.
            # Работает только с numpy-массивами (не с file path).
            # skip_vad_prefilter=True — для путей где VAD-модель неэффективна
            # (например live_subs захватывает system audio с YouTube — VAD model
            # тренирована на mic input и speech_ratio=0.0 на компрессированном
            # потоке → STT никогда не вызывается).
            if (
                settings.STT_VAD_PREFILTER_ENABLED
                and not skip_vad_prefilter
                and not _smart_silence_active  # W1096 F3: mutex — skip VAD when SmartSilenceSkipper ran
                and isinstance(audio_data, np.ndarray)
            ):
                vad_result = self._apply_vad_prefilter(audio_data)
                if vad_result is None:
                    # Тишина или слишком мало речи — возвращаем пустой результат
                    return self._empty_transcription_result("vad_skip", resolved_lang)
                audio_data = vad_result

            # single_pass=False (по умолчанию, путь диктовки) — НЕ передаём keyword
            # вовсе: старые test doubles, мокающие _transcribe_with_fallback
            # фиксированной сигнатурой (audio, prompt=None, language=None),
            # не должны увидеть новый параметр, если он не используется.
            _fallback_call_kwargs: dict[str, Any] = {}
            if single_pass:
                _fallback_call_kwargs["single_pass"] = single_pass
            result = self._transcribe_with_fallback(
                audio_data, prompt=dynamic_prompt, language=resolved_lang, **_fallback_call_kwargs,
            )

            # 3a. Confidence-driven multi-pass retry (только для финальных транскрибаций).
            # single_pass=True (live subs) отключает эту ветку — см. docstring выше.
            if not is_preview and not single_pass and settings.STT_MULTIPASS_ENABLED:
                result = self._maybe_multipass_retry(
                    audio_data, dynamic_prompt, resolved_lang, result,
                )

            raw_text = str(result.get("text", "")).strip()
            _is_loop = False  # set below; guards rewriter/punct-pass skip

            # Phase C C.4-wire: stt.repetition_loop — fires when Whisper returns a
            # hallucination loop (repeated bigrams / sentences / low unique-ratio).
            # Text is returned UNMODIFIED — «не врём про input»: user sees actual
            # Whisper output plus warning toast and can decide whether to re-record.
            # _is_loop=True also skips LLM rewrite/punct passes — sending a
            # 500-char bigram loop to LM Studio reliably returns HTTP 400 (J bug).
            if raw_text and not is_preview:
                _is_loop, _loop_reason = is_likely_repetition_loop(raw_text)
                if _is_loop:
                    logger.warning("Whisper repetition loop detected: %s", _loop_reason)
                    self._push_error(
                        "stt.repetition_loop",
                        f"reason={_loop_reason} text_len={len(raw_text)}",
                    )
                    # Перепрогон другим движком: тост «перезапиши» — плохой ответ,
                    # когда вторая попытка стоит секунды. single_pass (live subs)
                    # исключён: там поток, а не диктовка.
                    if not single_pass:
                        result = self._maybe_repetition_loop_retry(
                            audio_data, dynamic_prompt, resolved_lang, result, _loop_reason,
                        )
                        if result.get("loop_retry_applied"):
                            raw_text = str(result.get("text", "")).strip()
                            _is_loop, _loop_reason = is_likely_repetition_loop(raw_text)

            # Phase B.2: stt.empty_text — fires when STT returns empty AND audio is
            # non-trivial (>2s), to distinguish real silence from transcription failures.
            if not raw_text and not is_preview:
                _audio_dur = result.get("audio_duration_sec") or 0.0
                if _audio_dur <= 0.0 and isinstance(audio_data, __import__("numpy").ndarray):
                    _audio_dur = len(audio_data) / 16000.0
                if _audio_dur > 2.0:
                    self._push_error(
                        "stt.empty_text",
                        f"empty STT result for {_audio_dur:.1f}s audio (model={result.get('model_used', self.current_model)})",
                        severity="info",
                    )

            segments = result.get("segments", [])
            _diarize_effective = diarize if diarize is not None else settings.DIARIZATION_ENABLED
            if not is_preview and _diarize_effective:
                _report("diarize")
            diarization = self._maybe_run_diarization(audio_data, segments, is_preview=is_preview, diarize=diarize)

            # После STT + diarization — освобождаем MLX промежуточные массивы.
            # MLX держит Metal-буферы пока Python GC не удалит ссылки на mx.array.
            import gc as _gc
            _gc.collect()
            # H2: явный flush MLX Metal cache — без этого GPU буферы остаются в
            # Metal heap и backend не возвращается к baseline RSS после каждого STT.
            # W1618/W63: clear_cache — MLX op, must hold mlx_lock to prevent concurrent SIGSEGV.
            # W1635: degrade_on_timeout=True — non-critical cache flush, not inference.
            try:
                from core.mlx_whisper_session import mlx_whisper_worker_enabled

                if not mlx_whisper_worker_enabled():
                    import mlx.core as _mx
                    with mlx_inter_process_lock(degrade_on_timeout=True), mlx_lock():  # W1635
                        _mx.clear_cache()
            except (ImportError, AttributeError):
                pass  # MLX не установлен или старая версия без clear_cache

            # 4. Очистка результата через утилиты (D.7 normalization)
            _report("cleanup")
            cleaned_text = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)
            text = cleaned_text

            # 4.3 Голосовые команды диктовки (opt-in, перед punctuation pass)
            # «запятая» → «,», «новый абзац» → «\n\n», «удалить последнее слово» и т.д.
            from core.voice_commands import VoiceCommandProcessor  # lazy — avoid circular
            _vc_processor = VoiceCommandProcessor(settings_get=self._settings_get)
            _vc_lang = resolved_lang or settings.TRANSCRIBE_LANGUAGE
            _vc_result = _vc_processor.process(text, language=_vc_lang)
            if _vc_result != text:
                _report("voice_commands")
                logger.info(
                    "VoiceCommands: %d chars → %d chars (lang=%s)",
                    len(text), len(_vc_result), _vc_lang,
                )
                text = _vc_result

            # 4.3a Голосовые текстовые сниппеты (opt-in, после voice_commands)
            # trigger-фразы → user-defined expansions, напр. "вставь подпись" → "С уважением,\nПавел"
            _snippets_provider = getattr(self, "_snippets_provider", None)
            if _snippets_provider is not None:
                from core.text_snippet_expander import TextSnippetExpander  # lazy — avoid circular
                _snip_expander = TextSnippetExpander(
                    settings_get=self._settings_get,
                    snippets_provider=_snippets_provider,
                )
                _snip_result = _snip_expander.expand(text)
                if _snip_result != text:
                    _report("text_snippets")
                    logger.info(
                        "TextSnippets: %d chars → %d chars",
                        len(text), len(_snip_result),
                    )
                    text = _snip_result

            # 4.3b Фонетический словарь пользователя (opt-in, после text_snippets)
            # Варианты → канонические написания, напр. "пашел"/"павэл" → "Павел"
            _phonetic_provider = getattr(self, "_phonetic_provider", None)
            if _phonetic_provider is not None:
                from core.phonetic_vocabulary import PhoneticVocabulary  # lazy — avoid circular
                _phonetic_vocab = PhoneticVocabulary(
                    settings_get=self._settings_get,
                    entries_provider=_phonetic_provider,
                )
                _phonetic_result = _phonetic_vocab.correct(text)
                if _phonetic_result != text:
                    _report("phonetic_vocab")
                    logger.info(
                        "PhoneticVocab: %d chars → %d chars",
                        len(text), len(_phonetic_result),
                    )
                    text = _phonetic_result

            # 4.4a Нормализация числительных и дат/времени (post-STT, pre-LLM)
            # «сто двадцать три» → «123», «третье ноября» → «03.11» и т.д.
            _norm_lang = resolved_lang or settings.TRANSCRIBE_LANGUAGE
            if settings.NUMBER_NORMALIZATION_ENABLED or self._settings_get(
                "number_normalization_enabled", settings.NUMBER_NORMALIZATION_ENABLED
            ):
                from core.number_normalizer import NumberNormalizer  # lazy
                _nn_result = NumberNormalizer().normalize(text, language=_norm_lang)
                if _nn_result != text:
                    _report("number_normalization")
                    logger.info(
                        "NumberNormalizer: %d chars → %d chars (lang=%s)",
                        len(text), len(_nn_result), _norm_lang,
                    )
                    text = _nn_result
            if settings.DATETIME_NORMALIZATION_ENABLED or self._settings_get(
                "datetime_normalization_enabled", settings.DATETIME_NORMALIZATION_ENABLED
            ):
                from core.datetime_normalizer import DateTimeNormalizer  # lazy
                _dt_result = DateTimeNormalizer().normalize(text, language=_norm_lang)
                if _dt_result != text:
                    _report("datetime_normalization")
                    logger.info(
                        "DateTimeNormalizer: %d chars → %d chars (lang=%s)",
                        len(text), len(_dt_result), _norm_lang,
                    )
                    text = _dt_result

            # 4.5a Punctuation-only LLM pass (opt-in, перед полным rewrite)
            # Модели с нативной пунктуацией (gigaam-mlx) помечают результат
            # native_punctuation=True — дорогой LLM-проход для них холостой.
            _native_punct = isinstance(result, dict) and bool(
                result.get("native_punctuation")
            )
            if self._punctuation_pass_allowed() and not _is_loop and not _native_punct:
                _report("punctuation_pass")
                punct_result = self._llm_rewriter.fix_punctuation_only(
                    text, language=resolved_lang or settings.TRANSCRIBE_LANGUAGE
                )
                if punct_result is not None:
                    logger.info(
                        "Punctuation pass: %d chars -> %d chars",
                        len(text), len(punct_result),
                    )
                    text = punct_result
                else:
                    logger.debug("Punctuation pass: rejected or unavailable, keeping original")

            # 4.5 D.10a: LLM rewrite hook (только если admin+runtime toggle=true)
            llm_result = None
            llm_diff = None
            if self._llm_rewrite_allowed() and not _is_loop:
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
                    # 4.5a Cloud rewriter fallback: когда локальный LM Studio недоступен,
                    # опционально полируем транскрипт через облачный LLM.
                    # PRIVACY CONTRACT: разрешено ТОЛЬКО если cloud_rewriter_enabled=True
                    # И privacy_mode_enabled=False (проверяется в _cloud_rewrite_allowed).
                    if self._cloud_rewrite_allowed():
                        try:
                            from backend.cloud_rewriter import cloud_rewrite as _cloud_rewrite  # noqa: PLC0415
                            _cr_lang = resolved_lang or settings.TRANSCRIBE_LANGUAGE
                            cloud_text = _cloud_rewrite(cleaned_text, _cr_lang)
                            if cloud_text:
                                logger.info(
                                    "Cloud rewrite fallback applied: %d->%d chars lang=%s",
                                    len(cleaned_text), len(cloud_text), _cr_lang,
                                )
                                llm_diff = TextDiffAnalyzer().compute_diff(cleaned_text, cloud_text)
                                self._last_llm_diff = llm_diff
                                text = cloud_text
                                # Privacy audit trail: данные покинули устройство.
                                try:
                                    from backend.privacy_audit import get_privacy_audit_logger  # noqa: PLC0415
                                    _cr_provider = self._settings_get("cloud_rewriter_provider", "openai")
                                    get_privacy_audit_logger().log_event(
                                        category="cloud_rewrite",
                                        action="cloud_rewrite_used",
                                        details={
                                            "provider": _cr_provider,
                                            "input_chars": len(cleaned_text),
                                            "output_chars": len(cloud_text),
                                            "language": _cr_lang,
                                        },
                                    )
                                except Exception:
                                    pass  # audit trail must never break transcription
                        except Exception as _cr_exc:
                            logger.warning("Cloud rewrite error (ignored): %s", _cr_exc)

            # 5. Расчет метрик уверенности (единый источник — helper: результаты
            # без segments, но с явным confidence (GigaAM) не зануляются)
            confidence = self._raw_confidence_from_result(result)

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

            try:
                from backend.observability import add_breadcrumb as _add_bc  # lazy — avoid circular
                _add_bc(
                    category="transcription",
                    message="transcribe_finish",
                    level="info",
                    data={
                        "duration_ms": int(duration * 1000),
                        "confidence": round(calibrated_score.calibrated, 3),
                        "language": result.get("language", resolved_lang) or "auto",
                        "engine": result.get("engine", "mlx-whisper"),
                        "llm_applied": bool(llm_result is not None and llm_result.ok),
                        "is_preview": is_preview,
                    },
                )
            except Exception:
                pass  # telemetry must never break transcription

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
                # 🔴 Откуда взялось число: "logprob" — посчитано по сегментам
                # (путь Whisper), "constant" — подставлено адаптером, который
                # оценку не выдаёт (GigaAM). Умолчание "logprob" намеренно: этот
                # сборщик обслуживает whisper-путь, и пометить его "constant"
                # значило бы оболгать честный замер в обратную сторону.
                "confidence_source": result.get("confidence_source", "logprob"),
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
                # Multi-pass debug metadata: список попыток {model, confidence, latency_ms}.
                # Присутствует только если STT_MULTIPASS_ENABLED=True и не preview.
                "multipass_attempts": result.get("multipass_attempts"),
            }
        except Exception as exc:
            logger.exception("Критическая ошибка распознавания")
            # Wave 77: push stt.critical_recognition_error (68 occurrences in production logs)
            self._push_error(
                "stt.critical_recognition_error",
                f"broad except in transcribe(): {type(exc).__name__}: {exc}",
                severity="critical",
            )
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

    @staticmethod
    def _raw_confidence_from_result(result: dict[str, Any]) -> float:
        """Вычисляет сырую уверенность из segments dict, возвращает 0.0 если нет данных.

        Результаты без segments, но с явным confidence (GigaAM-адаптеры) —
        берём явное поле: иначе multipass считает их провальными (0.0) и
        всегда перегоняет через whisper, выбрасывая GigaAM-текст.
        """
        segments = result.get("segments", [])
        if not segments:
            return float(result.get("confidence") or 0.0)
        return float(np.mean([np.exp(s.get("avg_logprob", -1.0)) for s in segments]))

    @staticmethod
    def _estimate_audio_duration_sec(
        audio_data: Any, sample_rate: int | float | None = None
    ) -> float | None:
        """Длительность аудио в секундах для бюджета попытки STT (§4.2).

        Общий хелпер для _transcribe_with_fallback_impl (whisper-каскад) и
        _maybe_multipass_retry (ретраи) — находка 4 финального гейта волны
        2026-08-26: до этого multipass считал длительность ТОЛЬКО для
        np.ndarray, для пути к файлу молча отдавал None (ретрай получал
        потолок профиля вместо бюджета, масштабированного от реальной
        длительности), а сиблинг честно читал файл через soundfile.
        Неизвестный тип / отсутствующий файл / сбой чтения → None (вызывающая
        сторона фолбэчится на потолок профиля — не на час).
        """
        if isinstance(audio_data, np.ndarray):
            sr = 16000.0 if sample_rate is None else float(sample_rate)
            if sr > 0 and len(audio_data) > 0:
                return len(audio_data) / sr
            return None
        if isinstance(audio_data, (str, Path)) and os.path.exists(str(audio_data)):
            try:
                import soundfile as _sf_dur
                return float(_sf_dur.info(str(audio_data)).duration)
            except Exception:
                return None
        return None

    def _maybe_repetition_loop_retry(
        self,
        audio_data: Any,
        prompt: str,
        language: str | None,
        first_result: dict[str, Any],
        loop_reason: str,
    ) -> dict[str, Any]:
        """Перепрогон зацикленной транскрипции другим движком.

        Детектор `is_likely_repetition_loop` до 03.09.2026 только предупреждал:
        текст возвращался неизменным, владелец получал тост «перезапиши». Для
        русского это была единственная защита вовсе — ретрай по уверенности там
        не может сработать, потому что GigaAM отдаёт константу 0.9
        (``confidence_source == "constant"``), а порог заведомо ниже.

        🔴 Повтор берётся ТОЛЬКО если он сам не зациклен и не пуст. Иначе
        возвращается исходный текст: правило «не врём про input» остаётся в
        силе, и вырожденный ответ второго движка не выдаётся за спасение.
        Попытка ровно одна — диктовка не должна удлиняться вдвое ради случая,
        который в норме не наступает.
        """
        if not bool(getattr(settings, "STT_LOOP_RETRY_ENABLED", True)):
            return first_result

        # Кандидат — модель Whisper, отличная от движка, который зациклился.
        # Для GigaAM (движок русского) это whisper-large; для самого whisper —
        # тяжёлая max-модель, если она доступна.
        first_engine = str(first_result.get("engine", ""))
        candidates: list[str] = []
        if "whisper" not in first_engine:
            candidates.append(self.current_model)
        for model in getattr(settings, "model_max_list", []) or []:
            if model != self.current_model and not self._is_model_unavailable(model):
                candidates.append(model)
        if not candidates:
            return first_result

        model_label = candidates[0]
        logger.warning(
            "[STT] зацикливание (%s, engine=%s) → перепрогон через %s",
            loop_reason, first_engine or "unknown", model_label,
        )
        attempt_start = time.time()
        try:
            retry_result = self._transcribe_model(
                audio_data, model_label, prompt, language,
            )
        except Exception as exc:  # noqa: BLE001 — сбой повтора не смеет ломать диктовку
            logger.warning("[STT] перепрогон после зацикливания не удался: %s", exc)
            first_result["loop_retry_error"] = str(exc)
            return first_result

        retry_text = str(retry_result.get("text", "")).strip()
        retry_looped = is_likely_repetition_loop(retry_text)[0] if retry_text else False
        latency_ms = int((time.time() - attempt_start) * 1000)

        if not retry_text or retry_looped:
            logger.warning(
                "[STT] перепрогон не помог (%s, %d мс) — остаёмся на исходном тексте",
                "пусто" if not retry_text else "снова зациклен", latency_ms,
            )
            first_result["loop_retry_applied"] = False
            return first_result

        retry_result["loop_retry_applied"] = True
        retry_result["loop_retry_from_engine"] = first_engine
        retry_result["loop_retry_latency_ms"] = latency_ms
        retry_result["model_used"] = model_label
        logger.info(
            "[STT] перепрогон спас диктовку: %s → %s, %d знаков за %d мс",
            first_engine or "unknown", model_label, len(retry_text), latency_ms,
        )
        return retry_result

    def _maybe_multipass_retry(
        self,
        audio_data: Any,
        prompt: str,
        language: str | None,
        first_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Confidence-driven multi-pass retry.

        Если первый pass (balanced) вернул уверенность ниже
        settings.STT_MIN_CONFIDENCE_THRESHOLD — пробует тяжелее модели.
        Записывает каждую попытку в result["multipass_attempts"].
        Возвращает результат с наибольшей уверенностью среди всех попыток.
        """
        threshold = settings.STT_MIN_CONFIDENCE_THRESHOLD
        max_retries = settings.STT_MAX_RETRIES

        # threshold == 0 → никогда не ретраить
        if threshold <= 0.0 or max_retries <= 0:
            return first_result

        first_conf = self._raw_confidence_from_result(first_result)
        t0 = time.time()
        attempts = [
            {
                "model": first_result.get("model_used", self.current_model),
                "confidence": round(first_conf, 4),
                "latency_ms": int((time.time() - t0) * 1000),
            }
        ]

        if first_conf >= threshold:
            first_result["multipass_attempts"] = attempts
            return first_result

        # Строим список кандидатов для retry: max-model(s) + optional remote
        retry_candidates: list[dict[str, Any]] = []
        for model in settings.model_max_list:
            if not self._is_model_unavailable(model):
                retry_candidates.append({"kind": "model", "name": model})
        if (
            settings.NETWORK_MODE != "offline_strict"
            and self._remote_stt_retry_configured()
        ):
            retry_candidates.append({"kind": "remote", "name": "remote"})

        best_result = first_result
        best_conf = first_conf

        # Спека 2026-08-26: бюджет ретрая масштабируется от длительности.
        # Находка 4: общий хелпер с _transcribe_with_fallback_impl — раньше
        # здесь считался только np.ndarray, путь к файлу отдавал None.
        _mp_duration_sec = self._estimate_audio_duration_sec(audio_data)

        retries_done = 0
        first_model = str(first_result.get("model_used") or getattr(self, "current_model", "") or "")
        skip_second_mlx = should_skip_second_mlx_checkpoint()
        for candidate in retry_candidates:
            if retries_done >= max_retries:
                break
            if stt_budget.budget_exhausted(stt_budget.MIN_USEFUL_ATTEMPT_SEC):
                logger.warning(
                    "[STT] multipass: бюджет запроса исчерпан — ретраи "
                    "прерваны перед %s", candidate["name"],
                )
                break

            model_label = candidate["name"]
            if candidate["kind"] == "model":
                if first_model and model_label == first_model:
                    logger.info("[STT] skip same-checkpoint retry %s", model_label)
                    continue
                if skip_second_mlx:
                    logger.warning(
                        "[STT] skip second MLX checkpoint %s (vm_pressure); keep %s conf=%.2f",
                        model_label, first_model, first_conf,
                    )
                    attempts.append({
                        "model": model_label,
                        "confidence": round(first_conf, 4),
                        "latency_ms": 0,
                        "skipped": "vm_pressure",
                    })
                    continue
            logger.info(
                "[STT] balanced→%s retry: confidence %.2f < %.2f threshold",
                model_label, best_conf, threshold,
            )

            attempt_start = time.time()
            try:
                if candidate["kind"] == "model":
                    # Один источник истины для обоих пределов: то же число уходит
                    # и воркеру (как срок жизни), и сюда (как срок ожидания).
                    # Разъехавшись, они дают брошенный поток с замком сессии.
                    _attempt_timeout = stt_budget.resolve_attempt_timeout_sec(
                        _mp_duration_sec
                    )
                    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = _executor.submit(
                            self._transcribe_model, audio_data, model_label, prompt, language,
                            attempt_timeout_sec=_attempt_timeout,
                        )
                        attempt_result = future.result(timeout=_attempt_timeout)
                    except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                        _executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception:
                        _executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    else:
                        _executor.shutdown(wait=False)
                    attempt_result["model_used"] = model_label
                else:
                    attempt_result = self._transcribe_remote(audio_data, prompt)

                attempt_conf = self._raw_confidence_from_result(attempt_result)
                latency_ms = int((time.time() - attempt_start) * 1000)
                attempts.append({
                    "model": model_label,
                    "confidence": round(attempt_conf, 4),
                    "latency_ms": latency_ms,
                })

                if attempt_conf > best_conf:
                    best_conf = attempt_conf
                    best_result = attempt_result

                if best_conf >= threshold:
                    logger.info(
                        "[STT] multipass: %s достиг порога %.2f >= %.2f",
                        model_label, best_conf, threshold,
                    )
                    break

            except Exception as exc:
                latency_ms = int((time.time() - attempt_start) * 1000)
                logger.warning("[STT] multipass retry %s не сработал: %s", model_label, exc)
                attempts.append({
                    "model": model_label,
                    "confidence": 0.0,
                    "latency_ms": latency_ms,
                    "error": str(exc),
                })
                if self._blacklist_allowed_for(exc):
                    self._unavailable_models[model_label] = time.monotonic()

            retries_done += 1

        best_result["multipass_attempts"] = attempts
        return best_result

    # ------------------------------------------------------------------
    # Streaming chunked transcription
    # ------------------------------------------------------------------

    @staticmethod
    def _lcs_length(a: list[str], b: list[str]) -> int:
        """Длина наибольшей общей подпоследовательности (LCS) двух списков слов."""
        m, n = len(a), len(b)
        if m == 0 or n == 0:
            return 0
        # Используем два ряда DP для экономии памяти.
        prev = [0] * (n + 1)
        curr = [0] * (n + 1)
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if a[i - 1] == b[j - 1]:
                    curr[j] = prev[j - 1] + 1
                else:
                    curr[j] = max(prev[j], curr[j - 1])
            prev, curr = curr, [0] * (n + 1)
        return prev[n]

    @staticmethod
    def _stitch_overlap(text_prev: str, text_next: str, overlap_words: int) -> str:
        """Сшивает два текстовых фрагмента, удаляя дублирующиеся слова на шве.

        Алгоритм:
        1. Берём хвост text_prev (последние overlap_words слов) и голову text_next
           (первые overlap_words слов).
        2. Ищем наибольшую общую подпоследовательность (LCS) двух окон.
        3. Если LCS ≥ половины overlap_words — считаем, что шов найден, и
           отрезаем у text_next всё до конца совпавшего участка.
        4. Иначе — просто соединяем через пробел.
        """
        if not text_prev:
            return text_next
        if not text_next:
            return text_prev

        words_prev = text_prev.split()
        words_next = text_next.split()

        window = max(1, overlap_words)
        tail = words_prev[-window:]
        head = words_next[:window * 2]

        lcs = AudioEngine._lcs_length(tail, head)
        if lcs >= max(1, window // 2):
            # Находим конец последнего совпадения в head через обратный поиск
            # последнего слова tail['s LCS в head.
            # Подход: найти наибольший суффикс tail, который является подпоследовательностью
            # head, и запомнить позицию в head после него.
            matched_up_to = 0
            best_coverage = 0
            # Пробуем разные стартовые позиции tail чтобы найти лучшее выравнивание
            for tail_start in range(len(tail)):
                sub_tail = tail[tail_start:]
                ti, hi = 0, 0
                last_matched_hi = -1
                while ti < len(sub_tail) and hi < len(head):
                    if sub_tail[ti] == head[hi]:
                        ti += 1
                        last_matched_hi = hi
                    hi += 1
                matched = ti  # сколько слов из sub_tail нашли в head
                if matched > best_coverage:
                    best_coverage = matched
                    matched_up_to = last_matched_hi + 1
            # Отбрасываем совпавшую часть head из text_next
            remaining = words_next[matched_up_to:]
            joined = text_prev.rstrip()
            if remaining:
                joined = joined + " " + " ".join(remaining)
            return joined
        else:
            separator = " " if text_prev.endswith((".", "!", "?", ",")) else " "
            return text_prev.rstrip() + separator + text_next.lstrip()

    def transcribe_chunked(
        self,
        audio_data: Any,
        sample_rate: int = 16000,
        chunk_sec: float = 15.0,
        overlap_sec: float = 2.0,
        cleanup_profile: str = "soft",
        domain: str = "casual",
        extra_vocabulary: Optional[list[str]] = None,
        lang_hint: Optional[str] = None,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Транскрибирует длинное аудио чанками с перекрытием.

        Разбивает numpy-массив или аудиофайл на чанки по chunk_sec секунд
        с перекрытием overlap_sec. Транскрибирует каждый чанк независимо
        через `_transcribe_with_fallback`, затем сшивает результаты, удаляя
        дублирующиеся слова на швах (LCS-based seam detection).

        Args:
            audio_data: numpy.ndarray (16 kHz, mono, float32) или путь к файлу.
            sample_rate: частота дискретизации для ndarray. Файлы читаются через soundfile.
            chunk_sec: длительность одного чанка в секундах.
            overlap_sec: перекрытие между соседними чанками в секундах.
            cleanup_profile: "soft" | "strict" — профиль постобработки текста.
            domain: доменная подсказка для промпта (casual, medical, …).
            extra_vocabulary: дополнительные слова для промпта.
            lang_hint: ISO-639-1 код языка или None для автоопределения.
            progress_callback: вызывается со строкой-именем этапа.

        Returns:
            dict с полями: text, chunks (list[dict]), confidence, duration_ms,
            engine, model, language, segments (пустой — no diarization per chunk).
        """
        def _report(stage: str) -> None:
            if progress_callback is not None:
                try:
                    progress_callback(stage)
                except Exception:
                    pass

        start_time = time.time()
        resolved_lang = (
            self._resolve_language(lang_hint)
            if lang_hint is not None
            else settings.TRANSCRIBE_LANGUAGE
        )
        domain_desc = self.DOMAIN_PROMPTS.get(domain, self.DOMAIN_PROMPTS["casual"])
        dynamic_prompt = f"{settings.TRANSCRIBE_PROMPT} Тематика: {domain_desc}"
        if extra_vocabulary:
            dynamic_prompt += f" Ключевые слова: {', '.join(extra_vocabulary)}"

        # --- Загрузка аудио в numpy-массив ---
        _report("audio_load")
        audio_array: np.ndarray
        effective_sr: int
        if isinstance(audio_data, np.ndarray):
            audio_array = audio_data
            effective_sr = sample_rate
        elif isinstance(audio_data, (str, Path)) and os.path.exists(str(audio_data)):
            try:
                import soundfile as _sf_chunk
                audio_array, effective_sr = _sf_chunk.read(str(audio_data), dtype="float32", always_2d=False)
            except Exception as exc:
                logger.error("transcribe_chunked: не удалось загрузить файл: %s", exc)
                return {"text": "", "chunks": [], "error": str(exc), "status": "error"}
        else:
            logger.error("transcribe_chunked: неподдерживаемый тип audio_data: %s", type(audio_data))
            return {
                "text": "",
                "chunks": [],
                "error": f"unsupported audio_data type {type(audio_data).__name__}",
                "status": "error",
            }

        # --- Разбивка на чанки ---
        chunk_samples = int(chunk_sec * effective_sr)
        overlap_samples = int(overlap_sec * effective_sr)
        step_samples = max(1, chunk_samples - overlap_samples)
        total_samples = len(audio_array)
        overlap_words = max(1, int(overlap_sec * 3))  # ~3 слова/с как эвристика

        chunks_info: list[dict[str, Any]] = []
        start = 0
        chunk_idx = 0
        while start < total_samples:
            end = min(start + chunk_samples, total_samples)
            chunk_audio = audio_array[start:end]
            chunk_start_sec = start / effective_sr
            chunk_end_sec = end / effective_sr
            chunks_info.append({
                "idx": chunk_idx,
                "start_sec": round(chunk_start_sec, 3),
                "end_sec": round(chunk_end_sec, 3),
                "audio": chunk_audio,
            })
            if end >= total_samples:
                break
            start += step_samples
            chunk_idx += 1

        logger.info(
            "transcribe_chunked: %d чанков, chunk=%.1fs, overlap=%.1fs, total=%.1fs",
            len(chunks_info),
            chunk_sec,
            overlap_sec,
            total_samples / effective_sr,
        )

        # --- Транскрибирование каждого чанка ---
        _report("stt")
        chunk_results: list[dict[str, Any]] = []
        for info in chunks_info:
            _report(f"stt_chunk_{info['idx']}")
            try:
                # Файловые чанки остаются на исходной частоте. Передаём её
                # дальше только когда она отличается от продуктовых 16 кГц:
                # GigaAM нормализует сигнал один раз непосредственно перед
                # вычислением длительности, остальные адаптеры не меняем.
                _fallback_rate_kwargs = (
                    {"audio_sample_rate": effective_sr}
                    if effective_sr != 16000
                    else {}
                )
                raw_result = self._transcribe_with_fallback(
                    info["audio"],
                    prompt=dynamic_prompt,
                    language=resolved_lang,
                    **_fallback_rate_kwargs,
                )
                raw_text = str(raw_result.get("text", "")).strip()
                cleaned = TextUtils.cleanup_transcript(raw_text, profile=cleanup_profile)
                conf = self._raw_confidence_from_result(raw_result)
                chunk_results.append({
                    "idx": info["idx"],
                    "start_sec": info["start_sec"],
                    "end_sec": info["end_sec"],
                    "text": cleaned,
                    "confidence": round(conf, 3),
                    "engine": raw_result.get("engine", "mlx-whisper"),
                    "model": raw_result.get("model_used", self.current_model),
                    "language": raw_result.get("language", resolved_lang),
                    "ok": True,
                })
                logger.debug(
                    "Chunk %d/%.1f-%.1fs: %d chars, conf=%.2f",
                    info["idx"],
                    info["start_sec"],
                    info["end_sec"],
                    len(cleaned),
                    conf,
                )
            except Exception as exc:
                logger.warning(
                    "Chunk %d failed: %s", info["idx"], exc
                )
                chunk_results.append({
                    "idx": info["idx"],
                    "start_sec": info["start_sec"],
                    "end_sec": info["end_sec"],
                    "text": "",
                    "confidence": 0.0,
                    "engine": "mlx-whisper",
                    "model": self.current_model,
                    "language": resolved_lang,
                    "ok": False,
                    "error": str(exc),
                })

        # --- Сшивание результатов ---
        _report("cleanup")
        stitched = ""
        ok_chunks = [c for c in chunk_results if c["ok"] and c["text"]]
        for i, chunk in enumerate(ok_chunks):
            if i == 0:
                stitched = chunk["text"]
            else:
                stitched = AudioEngine._stitch_overlap(stitched, chunk["text"], overlap_words)

        # Итоговая уверенность — среднее по успешным чанкам
        confidences = [c["confidence"] for c in chunk_results if c["ok"]]
        avg_confidence = float(np.mean(confidences)) if confidences else 0.0
        engine_used = ok_chunks[0]["engine"] if ok_chunks else "mlx-whisper"
        model_used = ok_chunks[0]["model"] if ok_chunks else self.current_model
        lang_used = ok_chunks[0]["language"] if ok_chunks else resolved_lang

        duration_ms = int((time.time() - start_time) * 1000)
        logger.info(
            "transcribe_chunked готово: %d/%d чанков ОК, %d chars, %.1fs",
            len(ok_chunks),
            len(chunk_results),
            len(stitched),
            duration_ms / 1000,
        )

        # Очищаем большие numpy-массивы из результатов (экономим память)
        for c in chunk_results:
            c.pop("audio", None)

        return {
            "text": stitched,
            "raw_text": stitched,
            "cleaned_text": stitched,
            "chunks": chunk_results,
            "confidence": round(avg_confidence, 3),
            "raw_confidence": round(avg_confidence, 3),
            "duration_ms": duration_ms,
            "engine": engine_used,
            "model": model_used,
            "language": lang_used,
            "segments": [],
            "diarization": None,
            "llm_applied": False,
            "llm_latency_ms": None,
            "llm_fallback_reason": None,
            "llm_diff": None,
            "confidence_adjustments": [],
            "emotion": None,
        }

    def _transcribe_with_fallback(
        self,
        audio_data: Any,
        prompt: str,
        language: str | None = None,
        audio_sample_rate: int | float | None = None,
        single_pass: bool = False,
    ) -> dict[str, Any]:
        """Пробует несколько моделей при возникновении ошибок (например, нехватка VRAM).

        Перед загрузкой тяжёлых моделей (не balanced) проверяет свободную память
        через vm_stat, чтобы macOS Jetsam не убил процесс (SIGKILL).
        """
        with _profiler.start_span("stt_with_fallback"):
            # Старые test doubles и внутренние вызовы ожидают три аргумента.
            # Не передаём новый keyword для канонических 16 кГц/неизвестной
            # частоты и single_pass=False (default) — non-16k chunked-путь и
            # single_pass=True используют расширенный контракт.
            _extra_kwargs: dict[str, Any] = {}
            if audio_sample_rate is not None:
                _extra_kwargs["audio_sample_rate"] = audio_sample_rate
            if single_pass:
                _extra_kwargs["single_pass"] = single_pass
            if not _extra_kwargs:
                return self._transcribe_with_fallback_impl(audio_data, prompt, language)
            return self._transcribe_with_fallback_impl(
                audio_data,
                prompt,
                language,
                **_extra_kwargs,
            )

    _SENSEVOICE_MARKER: str = "sensevoice:adapter"
    _PARAKEET_MARKER: str = "parakeet:adapter"
    _WHISPERX_MARKER: str = "whisperx:adapter"
    _VOXTRAL_MARKER: str = "voxtral:adapter"
    _RU_FINETUNE_MARKER: str = "ru_finetune:adapter"
    _GIGAAM_MARKER: str = "gigaam:adapter"

    def _transcribe_with_fallback_impl(
        self,
        audio_data: Any,
        prompt: str,
        language: str | None = None,
        audio_sample_rate: int | float | None = None,
        single_pass: bool = False,
    ) -> dict[str, Any]:
        """Внутренняя реализация fallback chain. Отделена от публичной _transcribe_with_fallback
        чтобы обернуть весь chain одним span'ом без изменения retry/timeout логики.

        single_pass: 2026-08-12, live subs — отключает request-local fallback на
        Whisper после пустого успешного результата GigaAM (единственное место
        этого класса в chain: остальные адаптеры при пустом тексте не ретраятся,
        они просто возвращают его как обычный результат). Что первый движок
        вернул, то и результат, включая пустоту.
        """
        # Все локальные STT-адаптеры принимают голый ndarray как mono/16 кГц.
        # Поэтому файловый chunked-путь обязан нормализовать массив ДО выбора
        # кандидата, а не только внутри GigaAM: иначе его пустой результат
        # передаст, например, 15 секунд @48 кГц в Whisper как 45 секунд @16 кГц.
        chain_audio_data = audio_data
        chain_sample_rate = audio_sample_rate
        if audio_sample_rate is not None and isinstance(audio_data, np.ndarray):
            chain_audio_data = self._resample_audio_to_mono_16k(
                audio_data,
                audio_sample_rate,
            )
            chain_sample_rate = 16000

        # Спека 2026-08-26: длительность считается по chain_audio_data (после
        # ресемпла) — chunked-путь подаёт сюда уже нарезанный кусок и потому
        # бесплатно получает бюджет на чанк, а не на весь файл. Общий хелпер
        # с _maybe_multipass_retry (находка 4) — единственное отличие здесь:
        # частота уже известна после ресемпла (chain_sample_rate).
        _chain_duration_sec = self._estimate_audio_duration_sec(
            chain_audio_data, chain_sample_rate
        )

        candidates = [self.current_model]
        if self.quality_profile == "max":
            candidates = list(dict.fromkeys(settings.model_max_list))

        balanced_model = settings.MODEL_BALANCED

        _effective_lang = language if language is not None else settings.TRANSCRIBE_LANGUAGE

        # --- RU fine-tune adapter: позиция перед balanced (только для языка "ru") ---
        # antony66/whisper-large-v3-russian — fine-tune на русском Common Voice/OpenSTT.
        # Даёт ~2pp WER improvement vs базового whisper-large-v3 на русской речи.
        # Работает через тот же mlx_whisper.transcribe (drop-in checkpoint).
        # Активируется только если язык определён как "ru" (не None, не "es", не "en").
        # При ошибке загрузки маркер помечается недоступным, chain продолжается без него.
        if (
            settings.STT_USE_RU_FINETUNE
            and _effective_lang == "ru"
            and not self._is_model_unavailable(self._RU_FINETUNE_MARKER)
        ):
            candidates = [self._RU_FINETUNE_MARKER] + candidates

        # --- GigaAM-RNNT adapter: позиция 0 (первый в chain, только для языка "ru") ---
        # GigaAM v2-RNNT (244M) — RU-специализированная модель Sber (salute-developers).
        # ~3.8% WER на Common Voice RU vs ~9.8% у whisper-large-v3 (2.5× улучшение).
        # Порядок когда оба включены: GigaAM → RU-finetune → Whisper balanced → max → remote.
        # Работает через PyTorch MPS — mlx_lock НЕ нужен.
        # При ImportError или ошибке загрузки маркер помечается недоступным, chain продолжается.
        if (
            getattr(settings, "STT_GIGAAM_ENABLED", False)
            and _effective_lang == "ru"
            and not self._is_model_unavailable(self._GIGAAM_MARKER)
            and not getattr(self, "_skip_gigaam", False)  # Wave 525: REST-engine guard
        ):
            gigaam_adapter = self._router.get_gigaam_adapter() if self._router is not None else None
            if gigaam_adapter is not None:
                candidates = [self._GIGAAM_MARKER] + candidates
                logger.info(
                    "GigaAM-RNNT добавлен в chain первым (lang=%s, engine=gigaam-rnnt)",
                    _effective_lang,
                )

        # --- Parakeet adapter: позиция 2 (после balanced, до SenseVoice) ---
        # Вставляем маркер ПОСЛЕ первого кандидата (balanced/turbo). Parakeet
        # EN-оптимизирован и пробуется перед SenseVoice (которая RU+эмоция).
        # Гейт по settings.PARAKEET_ENABLED И языку "en".
        # W1644 F4: Parakeet — EN-only модель (NVIDIA NeMo OpenASR leaderboard EN).
        # На RU/ES аудио возвращает мусор, который может прервать chain ложным успехом.
        # Зеркалит паттерн GigaAM (_effective_lang == "ru"): строгий gate только на "en".
        # При сбое маркер помечается недоступным, chain продолжается без него.
        if (
            settings.PARAKEET_ENABLED
            and _effective_lang == "en"
            and not self._is_model_unavailable(self._PARAKEET_MARKER)
        ):
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
        if settings.SENSEVOICE_ENABLED and not self._is_model_unavailable(self._SENSEVOICE_MARKER):
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
        if settings.WHISPERX_ENABLED and not self._is_model_unavailable(self._WHISPERX_MARKER):
            # W1303 F1: WhisperX goes AFTER Parakeet AND SenseVoice (wave1305 fix).
            _wx_anchor_markers = {self._PARAKEET_MARKER, self._SENSEVOICE_MARKER}
            insert_pos = 1
            for i, c in enumerate(candidates):
                if c in _wx_anchor_markers:
                    insert_pos = i + 1
            candidates = candidates[:insert_pos] + [self._WHISPERX_MARKER] + candidates[insert_pos:]

        # --- Voxtral adapter: позиция 5 (после WhisperX, перед max-candidates) ---
        # Mistral Voxtral Mini 4B Realtime — STT + встроенный reasoning (Phase 4.4).
        # Chain order (когда все адаптеры включены):
        #   balanced → Parakeet → SenseVoice → WhisperX → Voxtral → max-candidates
        # Маркер вставляется сразу за WHISPERX_MARKER (или за последним adapter-маркером).
        # При сбое маркер помечается как недоступный, chain продолжается на whisper'ах.
        if settings.VOXTRAL_ENABLED and not self._is_model_unavailable(self._VOXTRAL_MARKER):
            # Ищем позицию вставки: после WHISPERX_MARKER если есть, иначе после всех
            # adapter-маркеров в начале списка (PARAKEET / SENSEVOICE / WHISPERX).
            _adapter_markers = {self._PARAKEET_MARKER, self._SENSEVOICE_MARKER, self._WHISPERX_MARKER}
            vx_insert_pos = 1
            for i, c in enumerate(candidates):
                if c in _adapter_markers:
                    vx_insert_pos = i + 1
            candidates = candidates[:vx_insert_pos] + [self._VOXTRAL_MARKER] + candidates[vx_insert_pos:]

        # Таблица маркеров адаптеров: marker → (span_prefix, model_setting, transcribe_fn)
        _ru_finetune_model = settings.STT_RU_FINETUNE_MODEL
        _gigaam_mode = getattr(settings, "STT_GIGAAM_MODE", "rnnt")
        _gigaam_model_label = f"gigaam-{_gigaam_mode}"
        _gigaam_source_rate = 16000 if chain_sample_rate is None else chain_sample_rate
        _adapter_dispatch = [
            (
                self._GIGAAM_MARKER,
                "stt_gigaam",
                _gigaam_model_label,
                lambda: self._transcribe_gigaam(
                    chain_audio_data,
                    language=language,
                    sample_rate=_gigaam_source_rate,
                ),
            ),
            (
                self._RU_FINETUNE_MARKER,
                "stt_ru_finetune",
                _ru_finetune_model,
                lambda: self._transcribe_model(
                    chain_audio_data, _ru_finetune_model, prompt, language,
                ),
            ),
            (
                self._PARAKEET_MARKER,
                "stt_parakeet",
                settings.PARAKEET_MODEL,
                lambda: self._transcribe_parakeet(chain_audio_data, language=language),
            ),
            (
                self._SENSEVOICE_MARKER,
                "stt_sensevoice",
                settings.SENSEVOICE_MODEL,
                lambda: self._transcribe_sensevoice(chain_audio_data, language=language),
            ),
            (
                self._WHISPERX_MARKER,
                "stt_whisperx",
                settings.WHISPERX_MODEL,
                lambda: self._transcribe_whisperx(chain_audio_data, language=language),
            ),
            (
                self._VOXTRAL_MARKER,
                "stt_voxtral",
                settings.VOXTRAL_MODEL,
                lambda: self._transcribe_voxtral(chain_audio_data, language=language),
            ),
        ]
        _adapter_map = {marker: (span_pfx, model, fn) for marker, span_pfx, model, fn in _adapter_dispatch}

        for model_name in candidates:
            if stt_budget.budget_exhausted(stt_budget.MIN_USEFUL_ATTEMPT_SEC):
                logger.warning(
                    "STT: бюджет запроса исчерпан — каскад прерван перед %s",
                    model_name,
                )
                self._push_error(
                    "stt.budget_exhausted",
                    f"budget exhausted before {model_name} "
                    f"(duration={_chain_duration_sec}, "
                    f"profile={stt_budget.current_profile()})",
                    severity="error",
                )
                break
            # Adapter ветки (не whisper).
            if model_name in _adapter_map:
                span_pfx, adapter_model, adapter_fn = _adapter_map[model_name]
                try:
                    span_name = f"{span_pfx}_{_short_model_name(adapter_model)}"
                    # W1219 F2: guard adapter calls with same timeout used for Whisper
                    # branches — prevents GPU stall from blocking IPC indefinitely.
                    # §4.8: floor поверх бюджета — внешний таймаут не смеет
                    # быть короче внутренних таймаутов GigaAM-subprocess
                    # (120s shortform / 180s load), иначе брошенный
                    # subprocess осиротеет с моделью на GPU.
                    # 🔴 Fix-раунд 1, находка 1: дедлайн ЗАПРОСА главнее floor'а —
                    # floor поднимает бюджет (оптимизация ВНУТРИ дедлайна), но
                    # остаток дедлайна режет сверху, когда дедлайн задан. Иначе
                    # floor 200с переживает 30-секундный остаток на 170с.
                    # Осиротевший subprocess не бессмертен: у него есть свой
                    # внутренний таймаут (120/180с), цена его смерти ограничена —
                    # в отличие от переживания дедлайна запроса.
                    _adapter_timeout = max(
                        stt_budget.resolve_attempt_timeout_sec(_chain_duration_sec),
                        stt_budget.ADAPTER_MIN_BUDGET_SEC,
                    )
                    _adapter_remaining_sec = stt_budget.remaining_sec()
                    if _adapter_remaining_sec is not None:
                        _adapter_timeout = min(_adapter_timeout, _adapter_remaining_sec)
                    _adapter_timeout = max(_adapter_timeout, stt_budget.MIN_USEFUL_ATTEMPT_SEC)
                    with _profiler.start_span(span_name):
                        _pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                        try:
                            _fut = _pool.submit(adapter_fn)
                            try:
                                adapter_result = _fut.result(timeout=_adapter_timeout)
                            except concurrent.futures.TimeoutError:
                                _fut.cancel()
                                raise TimeoutError(
                                    f"{span_pfx} adapter таймаут {_adapter_timeout}s — GPU stall?"
                                )
                        finally:
                            _pool.shutdown(wait=False)
                    if model_name == self._GIGAAM_MARKER:
                        _gigaam_text = str(adapter_result.get("text", "")).strip()
                        _gigaam_engine = str(adapter_result.get("engine", ""))
                        _gigaam_error = adapter_result.get("error")
                        if _gigaam_error or _gigaam_engine.endswith("-error"):
                            raise RuntimeError(
                                "GigaAM вернул аварийный результат: "
                                f"{_gigaam_error or _gigaam_engine or 'empty_text'}"
                            )
                        if not _gigaam_text:
                            # single_pass=True (live subs, 2026-08-12): request-local
                            # fallback на Whisper — второй тяжёлый проход на то же
                            # окно — отключён. Пустой ответ GigaAM = в окне нет речи,
                            # субтитр не показываем; это дешевле и честнее, чем
                            # добывать из шума низкоуверенный текст. Путь диктовки
                            # (single_pass=False) продолжает fallback как раньше.
                            if single_pass:
                                logger.info(
                                    "GigaAM не распознал речь — single_pass: "
                                    "пустой результат первого движка, fallback пропущен"
                                )
                                adapter_result["model_used"] = adapter_model
                                return adapter_result
                            # Пустой успешный ответ бывает на тишине и не доказывает,
                            # что модель сломана. Переключаем только этот запрос на
                            # Whisper, не записывая GigaAM в 300-секундный blacklist.
                            logger.info(
                                "GigaAM не распознал речь — request-local fallback на Whisper"
                            )
                            continue
                    adapter_result["model_used"] = adapter_model
                    return adapter_result
                except Exception as exc:
                    logger.warning("%s adapter не сработал: %s — продолжаю chain", span_pfx, exc)
                    if self._blacklist_allowed_for(exc, is_adapter=True):
                        self._unavailable_models[model_name] = time.monotonic()
                    continue

            if self._is_model_unavailable(model_name):
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
                timeout = stt_budget.resolve_attempt_timeout_sec(_chain_duration_sec)
                span_name = f"stt_model_{_short_model_name(model_name)}"
                with _profiler.start_span(span_name):
                    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                    try:
                        future = _executor.submit(
                            self._transcribe_model,
                            chain_audio_data,
                            model_name,
                            prompt,
                            language,
                            attempt_timeout_sec=timeout,
                        )
                        result = future.result(timeout=timeout)
                    except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                        _executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception:
                        _executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    else:
                        _executor.shutdown(wait=False)
                result["model_used"] = model_name
                return result
            except concurrent.futures.TimeoutError as exc:
                # Лог обязан называть СРАБОТАВШЕЕ число, не глобальную
                # константу — иначе следующий разбор идёт по ложному следу.
                logger.error(
                    "Таймаут %.0fs (профиль %s) при транскрибации моделью %s — пропускаю",
                    timeout, stt_budget.current_profile(), model_name,
                )
                if self._blacklist_allowed_for(exc):
                    self._unavailable_models[model_name] = time.monotonic()
            except MLXTimeoutError as e:
                # Watchdog-таймаут: Metal GPU завис.
                # Помечаем модель недоступной → fallback на следующий адаптер.
                logger.error(
                    "MLX watchdog timeout %.1fs для модели %s — Metal GPU stuck? Переключаюсь на следующий адаптер.",
                    e.timeout_sec, model_name,
                )
                self._unavailable_models[model_name] = time.monotonic()
            except MemoryError:
                logger.error("MemoryError при загрузке модели %s — помечаю как недоступную", model_name)
                self._unavailable_models[model_name] = time.monotonic()
                # Phase B.2: stt.load_fail — model failed to init due to OOM
                self._push_error(
                    "stt.load_fail",
                    f"MemoryError loading {model_name} — switching to balanced",
                    severity="error",
                )
                # Wave 60: stt.oom_model_evicted — model evicted from fallback chain
                self._push_error(
                    "stt.oom_model_evicted",
                    f"MemoryError evicted {model_name} from STT chain",
                )
            except OSError as e:
                # errno 12 = Cannot allocate memory — ядро отказало в mmap
                if e.errno == 12 or "Cannot allocate memory" in str(e):
                    logger.error("OOM (OSError) при модели %s: %s — помечаю как недоступную", model_name, e)
                    self._unavailable_models[model_name] = time.monotonic()
                    # Phase B.2: stt.load_fail — OOM at OS level
                    self._push_error(
                        "stt.load_fail",
                        f"OOM (OSError errno={e.errno}) loading {model_name}",
                        severity="error",
                    )
                    # Wave 60: stt.oom_model_evicted — OS-level OOM eviction
                    self._push_error(
                        "stt.oom_model_evicted",
                        f"OSError errno={e.errno} evicted {model_name} from STT chain",
                    )
                else:
                    logger.warning("Модель %s не сработала (OSError): %s", model_name, e)
                    self._unavailable_models[model_name] = time.monotonic()
            except Exception as e:
                logger.warning("Модель %s не сработала: %s", model_name, e)
                self._unavailable_models[model_name] = time.monotonic()

        # Если локально ничего не вышло — пробуем облако (если разрешено И настроено).
        # 🔴 Sibling-гейт (инцидент 2026-08-26): волна 22.08 закрыла ровно этот
        # класс у ДРУГОГО вызывающего (retry_candidates в multipass), а здесь
        # проверка осталась только на NETWORK_MODE. Итог у владельца: после
        # часового таймаута локального движка каскад шёл в облако без ключа и
        # выдавал «Критическая ошибка распознавания речи», подменяя настоящую
        # причину (все локальные движки не справились) сообщением про облако.
        if settings.NETWORK_MODE != "offline_strict":
            if self._remote_stt_retry_configured():
                logger.info("Локальные модели недоступны, переключаюсь на Remote STT...")
                with _profiler.start_span("stt_remote"):
                    return self._transcribe_remote(chain_audio_data, prompt)
            logger.info(
                "Локальные модели недоступны, облачный STT не настроен "
                "(нет ключа провайдера) — остаёмся с локальной ошибкой"
            )

        raise RuntimeError("Все доступные STT-движки вышли из строя.")

    @staticmethod
    def _classify_mlx_error_code(_emsg: str, is_memory_error: bool) -> str | None:
        """Classify a caught (MemoryError, RuntimeError) from mlx transcribe into an ErrorBus code.

        2026-08-19 fix: the two `_transcribe_model` exception handlers (worker-enabled
        path and direct in-process/watchdog path) independently duplicated this
        classification — a sibling-asymmetry class bug (see CLAUDE.md). The oom
        keyword set includes bare "metal", which is a substring of "iogpumetal" —
        so an IOGPUMetal command-buffer assertion (Wave 64, self-recovers via the
        subprocess worker, not an OOM) was ALSO matching the oom check whenever both
        conditions were tested independently, pushing a false-positive critical
        "not enough memory" toast to the owner. The assertion signature is more
        specific, so it MUST be checked first (if/elif, not two independent ifs) —
        a genuine OOM message ("failed to allocate ... metal buffer") never contains
        any of the assertion keywords, so it still falls through to the oom branch.
        """
        if any(
            kw in _emsg for kw in (
                "iogpumetal", "validate failed assertion",
                "commit command buffer", "uncommitted encoder",
            )
        ):
            return "mlx.metal_assertion_failure"
        if is_memory_error or any(
            kw in _emsg for kw in ("allocat", "out of memory", "metal", "oom")
        ):
            return "mlx.oom"
        return None

    def _transcribe_model(
        self,
        audio_data: Any,
        model_name: str,
        prompt: str,
        language: str | None = None,
        attempt_timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Низкоуровневый вызов MLX Whisper с обработкой несовместимых аргументов.

        Все MLX вызовы сериализуются через глобальный RLock (mlx_lock) во избежание
        race condition в __hash_table<MTL::Resource*> внутри libmlx.dylib (SIGSEGV).
        RLock позволяет повторный захват из того же потока (fallback chain).

        Если MLX_CRASH_RECOVERY_ENABLED=True, каждый вызов mlx_whisper.transcribe()
        оборачивается в MLXWatchdog.run_with_timeout() — при зависании GPU поток
        обрывается через MLXTimeoutError, который всплывает в fallback chain.
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

        recovery_enabled = getattr(settings, "MLX_CRASH_RECOVERY_ENABLED", True)
        # 🔴 Внутренний предел обязан срабатывать РАНЬШЕ внешнего бюджета попытки:
        # только он умеет реально прекратить работу (убить subprocess-воркер), тогда
        # как внешний лишь перестаёт ждать, оставляя поток с замком сессии.
        # Бюджет приходит параметром, а не читается здесь: stt_budget живёт в
        # ContextVar, который НЕ наследуется потоком пула (см. call_in_scope).
        timeout_sec = _fit_worker_timeout(
            getattr(settings, "MLX_TRANSCRIBE_TIMEOUT_SEC", 60.0),
            attempt_timeout_sec,
        )

        last_err: Exception | None = None
        from core.mlx_whisper_session import (
            MLXWorkerCrashed,
            mlx_whisper_worker_enabled,
            transcribe_via_mlx_worker,
        )

        if mlx_whisper_worker_enabled():
            # P0c: Metal только в child. Родитель держит flock (если флаг ON).
            # Child flock не берёт — иначе deadlock: родитель ждёт JSON, child ждёт flock.
            with mlx_inter_process_lock():
                for params in variants:
                    try:
                        return transcribe_via_mlx_worker(
                            audio_data,
                            params,
                            timeout_sec=float(timeout_sec),
                            model_name=model_name,
                        )
                    except MLXTimeoutError as e:
                        logger.error(
                            "MLX worker timeout %.1fs (model=%s) — fallback",
                            e.timeout_sec, model_name,
                        )
                        self._push_error(
                            "stt.mlx_timeout",
                            f"MLXTimeoutError {e.timeout_sec}s (model={model_name})",
                            severity="error",
                        )
                        raise
                    except MLXWorkerCrashed:
                        logger.error(
                            "mlx_whisper worker crashed (model=%s) — REST PID жив",
                            model_name,
                        )
                        raise
                    except TypeError as e:
                        last_err = e
                    except (MemoryError, RuntimeError) as e:
                        _emsg = str(e).lower()
                        _code = self._classify_mlx_error_code(_emsg, isinstance(e, MemoryError))
                        if _code is not None:
                            self._push_error(
                                _code,
                                f"{type(e).__name__}: {e} (model={model_name})",
                                severity="critical" if _code == "mlx.oom" else "error",
                            )
                        last_err = e
            raise last_err or RuntimeError("Ошибка вызова mlx_whisper.transcribe")

        # Сериализуем доступ к GPU через глобальный MLX lock.
        # W1635: also wrap with mlx_inter_process_lock for cross-process GPU safety.
        # Raises MLXInterLockTimeout — let it propagate to transcribe() callers.
        # Минимальный critical section: только сам mlx_whisper.transcribe вызов.
        with mlx_inter_process_lock(), mlx_lock():  # W1635: cross-process flock (outer) + intra-process RLock (inner)
            for params in variants:
                try:
                    if recovery_enabled:
                        # Watchdog: запускает в daemon-thread, бросает MLXTimeoutError при зависании.
                        # W1604 F1 fix: MLXTimeoutError перехватывается ЗДЕСЬ (внутри loop),
                        # чтобы variants fallthrough работал так же, как при recovery_enabled=False.
                        captured_params = params  # closure capture
                        return get_watchdog().run_with_timeout(
                            fn=lambda: mlx_whisper.transcribe(audio_data, **captured_params),
                            timeout_sec=timeout_sec,
                            model_name=model_name,
                        )
                    else:
                        return mlx_whisper.transcribe(audio_data, **params)
                except MLXTimeoutError as e:
                    # KRAB-EAR-BACKEND-1V: при таймауте watchdog (Metal GPU завис)
                    # перебор вариантов kwargs бессмысленен (тот же GPU, та же модель).
                    # Повторные попытки лишь умножали задержку (3x таймаут), приводя к
                    # 180с IPC backstop. Прерываемся немедленно для перехода к fallback chain.
                    logger.error(
                        "MLX watchdog timeout %.1fs (model=%s) — прерываю variants loop для fallback",
                        e.timeout_sec, model_name,
                    )
                    self._push_error(
                        "stt.mlx_timeout",
                        f"MLXTimeoutError {e.timeout_sec}s (model={model_name})",
                        severity="error",
                    )
                    raise
                except TypeError as e:
                    last_err = e
                except (MemoryError, RuntimeError) as e:
                    # Phase B.2: mlx.oom / Wave 64: mlx.metal_assertion_failure —
                    # classification centralized in _classify_mlx_error_code (2026-08-19
                    # sibling-asymmetry fix: assertion is more specific, checked first).
                    _emsg = str(e).lower()
                    _code = self._classify_mlx_error_code(_emsg, isinstance(e, MemoryError))
                    if _code is not None:
                        self._push_error(
                            _code,
                            f"{type(e).__name__}: {e} (model={model_name})",
                            severity="critical" if _code == "mlx.oom" else "error",
                        )
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
        with self._sensevoice_load_lock:
            # Re-check после получения блокировки (W1235 double-checked lock).
            if self._sensevoice_model is not None:
                return self._sensevoice_model
            if self._sensevoice_load_error:
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
        with self._parakeet_load_lock:
            # Re-check после получения блокировки (W1235 double-checked lock).
            if self._parakeet_model is not None:
                return self._parakeet_model
            if self._parakeet_load_error:
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
            if tmp_path:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass

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

        with self._whisperx_load_lock:
            # Re-check после получения блокировки (W1235 double-checked lock).
            if getattr(self, "_whisperx_model", None) is not None:
                return self._whisperx_model
            if getattr(self, "_whisperx_load_error", None):
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
                self._push_error(
                    "stt.diarization_skipped",
                    f"WhisperX diarization failed: {type(exc).__name__}: {exc}",
                    severity="info",
                )
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
        """Ленивая загрузка Voxtral pipeline. Raises если mistral-inference недоступен.

        W1474 F1: Double-checked locking через _voxtral_load_lock (threading.RLock)
        предотвращает двойную загрузку (~2-3 GB) при конкурентных IPC-вызовах.
        """
        # Fast-path без блокировки — если модель уже загружена.
        if getattr(self, "_voxtral_model", None) is not None:
            return self._voxtral_model
        # Acquire lock for slow-path (double-checked locking pattern)
        with getattr(self, "_voxtral_load_lock", threading.RLock()):
            # Re-check после получения блокировки
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

            # W1223/W1535 security: validate repo against allowlist before download.
            try:
                _validate_voxtral_repo(settings.VOXTRAL_MODEL)
            except ValueError as exc:
                self._voxtral_load_error = (
                    f"Voxtral repo не разрешён (допустимы только allowlist repos): {exc}"
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

    @staticmethod
    def _resample_audio_to_mono_16k(
        audio: np.ndarray,
        source_sample_rate: int | float,
    ) -> np.ndarray:
        """Приводит float32 сигнал к mono/16 кГц ровно один раз.

        Голый ndarray не несёт метаданных о частоте, поэтому GigaAM, Whisper и
        остальные адаптеры должны видеть один и тот же канонический массив.
        Для stereo soundfile-входа каналы сводятся до ресемплинга.
        """
        try:
            source_rate = float(source_sample_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Некорректная частота аудио для GigaAM: {source_sample_rate!r}"
            ) from exc
        if not np.isfinite(source_rate) or source_rate <= 0:
            raise ValueError(
                f"Частота аудио для GigaAM должна быть положительной: {source_sample_rate!r}"
            )

        mono = np.asarray(audio, dtype=np.float32)
        if mono.ndim == 2:
            mono = mono.mean(axis=1, dtype=np.float32)
        elif mono.ndim != 1:
            raise ValueError(f"STT ожидает mono/stereo-массив, получена shape={mono.shape}")
        if mono.size == 0 or source_rate == 16000.0:
            return np.ascontiguousarray(mono, dtype=np.float32)

        target_length = max(1, int(round(mono.size * 16000.0 / source_rate)))
        try:
            import scipy.signal as scipy_signal  # type: ignore[import]

            normalized = scipy_signal.resample(mono, target_length)
        except ImportError:
            old_positions = np.arange(mono.size, dtype=np.float64)
            new_positions = np.linspace(
                0.0,
                float(mono.size - 1),
                target_length,
                dtype=np.float64,
            )
            normalized = np.interp(new_positions, old_positions, mono)
        return np.ascontiguousarray(normalized, dtype=np.float32)

    def _transcribe_gigaam(
        self,
        audio_data: Any,
        language: str | None = None,
        sample_rate: int | float = 16000,
    ) -> dict[str, Any]:
        """Транскрибация через GigaAM v1-v3 (русскоязычный STT, Sber).

        Args:
            audio_data: путь к wav-файлу (str/Path), numpy.ndarray (любая частота),
                        или bytes (PCM int16 LE от AudioRecorder).
            language: ISO 639-1 код языка; GigaAM поддерживает только "ru",
                      передаётся для совместимости интерфейса.
            sample_rate: реальная частота ndarray/PCM bytes; для файла берётся
                         непосредственно из контейнера через soundfile.

        Returns:
            dict с ключами:
                text (str): распознанный текст.
                language (str): "ru".
                confidence (float): уверенность из адаптера (0.0–1.0).
                engine (str): "gigaam-rnnt".
                model_used (str): идентификатор модели (заполняется caller'ом).

        Raises:
            ImportError: если пакет gigaam или core.pipeline.stt_gigaam недоступен.
            RuntimeError: если адаптер не удалось получить из router.
            Exception: любая ошибка транскрибации пробрасывается вверх для fallback chain.
        """
        # Wave 525: REST-engine (skip_gigaam=True) must never reach here because
        # the chain-builder already excludes GIGAAM_MARKER.  If somehow called
        # directly, raise immediately so the fallback chain uses Whisper.
        if getattr(self, "_skip_gigaam", False):
            raise RuntimeError(
                "GigaAM вызван на REST-engine (skip_gigaam=True) — это баг; "
                "используй BackendService IPC для GigaAM транскрибации"
            )
        adapter = self._router.get_gigaam_adapter() if self._router is not None else None
        if adapter is None:
            raise RuntimeError(
                "GigaAM adapter недоступен (STT_GIGAAM_ENABLED=False или ImportError)"
            )

        # Phase B.2 F11: wire OOM callback so worker subprocess crashes surface as
        # mlx.oom ErrorBus events. Idempotent — already set callbacks are preserved.
        if getattr(adapter, "_oom_callback", None) is None:
            adapter._oom_callback = self._push_mlx_oom_for_worker
            # Propagate to already-spawned session (if any).
            sess = getattr(adapter, "_subprocess", None)
            if sess is not None and getattr(sess, "oom_callback", None) is None:
                sess.oom_callback = self._push_mlx_oom_for_worker

        # W1688 (W1686 F4 fix): wire _error_bus onto adapter so worker timeout/crash
        # errors reach the Loud Errors toast. Idempotent — already wired is preserved.
        # GigaAMAdapter._get_subprocess_session() propagates this further to each new
        # _GigaAMSubprocessSession at spawn time.
        engine_error_bus = getattr(self, "_error_bus", None)
        if engine_error_bus is not None and getattr(adapter, "_error_bus", None) is None:
            adapter._error_bus = engine_error_bus
            # Propagate to already-spawned session (if any).
            sess = getattr(adapter, "_subprocess", None)
            if sess is not None and getattr(sess, "_error_bus", None) is None:
                sess._error_bus = engine_error_bus

        # Нормализуем вход в mono float32 и сохраняем реальную исходную частоту.
        source_sample_rate: int | float = sample_rate
        if isinstance(audio_data, (str, Path)):
            if sf is None:
                raise ImportError("soundfile не установлен, не могу читать аудио-файл")
            audio_array, source_sample_rate = sf.read(
                str(audio_data), dtype="float32", always_2d=False,
            )
            if audio_array.ndim > 1:
                audio_array = audio_array.mean(axis=1)
            audio_data_np = audio_array.astype(np.float32)
        elif isinstance(audio_data, bytes):
            # PCM int16 LE от AudioRecorder
            audio_data_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        elif isinstance(audio_data, np.ndarray):
            audio_data_np = audio_data.astype(np.float32)
            if audio_data_np.ndim > 1:
                audio_data_np = audio_data_np.mean(axis=1)
        else:
            raise TypeError(f"Неподдерживаемый тип audio_data для GigaAM: {type(audio_data)}")

        # Критический инвариант: duration, chunker и адаптер работают с одним
        # каноническим mono/16k массивом. Адаптер получает sample_rate=16000 и
        # потому не выполняет второй реальный ресемплинг.
        audio_data_np = self._resample_audio_to_mono_16k(
            audio_data_np,
            source_sample_rate,
        )

        # GigaAM `transcribe()` имеет hard limit ~25 сек на одну операцию.
        # Для длинных аудио поддерживаем два пути:
        #   1. AudioChunker (предпочтительный): silence-based split без зависимостей.
        #      Чанки 20s с 5s запасом до предела. Не требует pyannote / HF token.
        #   2. transcribe_longform() (fallback): pyannote VAD — требует HF token
        #      + принятие TOS на huggingface.co/pyannote/segmentation-3.0.
        #      Используется только если AudioChunker недоступен.
        # Граница точная: upstream отвергает любой массив длиннее 25 * 16000.
        # Старый приблизительный порог 30s терял реальные записи 25–30s.
        _GIGAAM_MAX_CHUNK_SEC = 20.0  # с 5s запасом до hard limit ~25s
        duration_sec = len(audio_data_np) / 16000.0
        use_longform = duration_sec > GIGAAM_SHORTFORM_MAX_SEC
        hf_token = settings.STT_GIGAAM_HF_TOKEN or ""

        try:
            if use_longform:
                # Пробуем AudioChunker path — не требует pyannote/HF token.
                try:
                    from core.audio_chunker import AudioChunker
                    chunker = AudioChunker()
                    chunks = chunker.chunk(audio_data_np, sample_rate=16000,
                                           max_chunk_sec=_GIGAAM_MAX_CHUNK_SEC)
                    logger.info(
                        "GigaAM chunker path: duration=%.1fs → %d chunks (max %.0fs each)",
                        duration_sec, len(chunks), _GIGAAM_MAX_CHUNK_SEC,
                    )
                    chunk_results: list[dict] = []
                    chunks_native_punct = False
                    for ch in chunks:
                        ch_result = adapter.transcribe(ch.audio, sample_rate=16000)
                        chunks_native_punct = chunks_native_punct or (
                            isinstance(ch_result, dict)
                            and bool(ch_result.get("native_punctuation"))
                        )
                        chunk_results.append({
                            "text": ch_result.get("text", "") if isinstance(ch_result, dict) else str(ch_result),
                            "confidence": float(ch_result.get("confidence", 0.9)) if isinstance(ch_result, dict) else 0.9,
                            "start_sec": ch.start_sec,
                            "end_sec": ch.end_sec,
                        })
                    merged = AudioChunker.merge_results(chunk_results)
                    result = {
                        "text": merged["text"],
                        "language": "ru",
                        "confidence": merged["confidence"],
                        "engine": (
                            f"{engine_name_from_mode(getattr(settings, 'STT_GIGAAM_MODE', 'rnnt'))}"
                            "-chunked"
                        ),
                        # Пересборка не должна терять флаг адаптера (гейт
                        # punctuation-LLM-pass ниже по transcribe()).
                        "native_punctuation": chunks_native_punct,
                    }
                except Exception as chunker_exc:
                    # AudioChunker failed — fallback на transcribe_longform() (pyannote path).
                    logger.warning(
                        "GigaAM AudioChunker failed (%.1fs): %s — пробуем longform",
                        duration_sec, str(chunker_exc)[:200],
                    )
                    logger.info(
                        "GigaAM longform path: duration=%.1fs (> 24s), hf_token=%s",
                        duration_sec,
                        "set" if hf_token else "cached",
                    )
                    result = adapter.transcribe(
                        audio_data_np,
                        sample_rate=16000,
                        longform=True,
                        hf_token=hf_token,
                    )
            else:
                result = adapter.transcribe(audio_data_np, sample_rate=16000)
        except Exception as exc:
            # Оба пути упали. Исключение — единственный однозначный сигнал
            # fallback-chain перейти к Whisper; error-dict раньше считался
            # успешным и молча сохранял пустую транскрипцию.
            logger.warning(
                "GigaAM transcribe failed (duration=%.1fs, longform=%s): %s",
                duration_sec, use_longform, str(exc)[:200],
            )
            # Detect HF cache miss: huggingface_hub raises LocalEntryNotFoundError /
            # RepositoryNotFoundError / ConnectionError when model not cached offline.
            exc_str = str(exc).lower()
            _hf_cache_miss_keywords = (
                "localentrynotfound", "repositorynotfound", "connection error",
                "not found in cache", "gated repo", "access to model",
                "cannot find the requested files",
            )
            if any(kw in exc_str for kw in _hf_cache_miss_keywords):
                self._push_error(
                    "stt.gigaam_hf_cache_miss",
                    f"GigaAM HF cache miss (duration={duration_sec:.1f}s): {str(exc)[:300]}",
                    severity="warn",
                )
            raise RuntimeError(
                f"GigaAM transcribe failed: {str(exc)[:300]}"
            ) from exc

        # Нормализуем формат ответа адаптера
        text = result.get("text", "") if isinstance(result, dict) else str(result)
        confidence = float(result.get("confidence", 0.0)) if isinstance(result, dict) else 0.0
        engine_name = result.get("engine", "gigaam-rnnt") if isinstance(result, dict) else "gigaam-rnnt"
        native_punctuation = (
            bool(result.get("native_punctuation", False)) if isinstance(result, dict) else False
        )

        logger.info(
            "GigaAM транскрибация завершена: len=%d chars, confidence=%.3f, engine=%s",
            len(text),
            confidence,
            engine_name,
        )

        return {
            "text": text,
            "language": "ru",
            "confidence": confidence,
            "engine": engine_name,
            "native_punctuation": native_punctuation,
        }

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

            # W1219 F1: serialize Voxtral MLX inference through mlx_lock to prevent
            # concurrent GPU access SIGSEGV (mistral_inference uses MLX ops).
            with mlx_lock():
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

    # ------------------------------------------------------------------
    # Speaker-aware initial_prompt helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_speaker_context_prompt(num_speakers: int | None, language: str | None) -> str:
        """Строит подсказку для Whisper о диалоговом характере записи.

        Args:
            num_speakers: Оценённое количество спикеров (None или <2 → пустая строка).
            language: ISO 639-1 код языка ("ru", "es", "en") или None.

        Returns:
            Строку-подсказку на языке записи, или пустую строку если спикеров < 2.
        """
        if not num_speakers or num_speakers < 2:
            return ""

        lang = (language or "").lower()

        if lang == "ru":
            if num_speakers == 2:
                return "Запись диалога двух собеседников."
            return "Запись беседы нескольких участников."

        if lang == "es":
            if num_speakers == 2:
                return "Grabación del diálogo de dos interlocutores."
            return "Grabación de una conversación de varios participantes."

        # Default: English (also covers None / unknown languages)
        if num_speakers == 2:
            return "Recording of a dialogue between two speakers."
        return "Recording of a multi-speaker conversation."

    def _estimate_num_speakers(
        self,
        audio_data: Any,
        sample_rate: int = 16000,
        *,
        cache: dict[str, Any] | None = None,
    ) -> int | None:
        """Оценивает количество спикеров lightweight методом до Whisper STT.

        Использует pyannote VAD-based сегментацию без полного speaker embedding —
        быстро и дёшево по памяти. Результат кешируется в `cache` словаре
        (ключ "_estimated_num_speakers") чтобы не пересчитывать дважды.

        Args:
            audio_data: Путь к аудиофайлу или numpy array.
            sample_rate: Частота дискретизации для numpy array (игнорируется для файлов).
            cache: Опциональный словарь для кеширования результата.

        Returns:
            Целое число ≥ 1 (количество спикеров), или None если оценка не удалась.
        """
        _CACHE_KEY = "_estimated_num_speakers"
        if cache is not None and _CACHE_KEY in cache:
            return cache[_CACHE_KEY]  # type: ignore[return-value]

        result: int | None = None
        tmp_path_to_cleanup: str | None = None
        try:
            audio_path = self._resolve_audio_path(audio_data)
            if audio_path is None:
                # numpy array path: write to temp WAV for pyannote
                if hasattr(audio_data, "shape") and sf is not None:
                    import tempfile as _tempfile
                    with _tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                        tmp_path_to_cleanup = tmp.name
                    sf.write(tmp_path_to_cleanup, audio_data, sample_rate)
                    audio_path = tmp_path_to_cleanup

            if audio_path is None:
                return None

            import gc
            prepared_path, should_cleanup = self._prepare_audio_for_diarization(audio_path)
            try:
                # Тот же pyannote pipeline используется полной диаризацией и
                # meeting-окнами. Все три точки инференса обязаны делить один
                # lock, иначе два MPS-вызова могут пересечься и уронить Metal.
                # Pipeline получаем уже ВНУТРИ lock: hot reload HF-токена берёт
                # locks в том же порядке run → load и не оставляет нам старый
                # объект между invalidation и реальным вызовом.
                with self._diarization_run_lock:
                    pipeline = self._load_diarization_pipeline()
                    try:
                        annotation = pipeline(prepared_path)
                    finally:
                        gc.collect()
                        if (
                            torch is not None
                            and hasattr(torch, "mps")
                            and torch.backends.mps.is_available()
                        ):
                            try:
                                torch.mps.empty_cache()
                            except Exception:
                                pass
                if hasattr(annotation, "speaker_diarization"):
                    annotation = annotation.speaker_diarization
                speakers: set[str] = set()
                for _, _, speaker in annotation.itertracks(yield_label=True):
                    speakers.add(str(speaker))
                result = max(1, len(speakers))
            finally:
                if should_cleanup:
                    Path(prepared_path).unlink(missing_ok=True)

        except Exception as exc:
            logger.debug("_estimate_num_speakers: не удалось оценить спикеров: %s", exc)
            result = None
        finally:
            if tmp_path_to_cleanup is not None:
                try:
                    Path(tmp_path_to_cleanup).unlink(missing_ok=True)
                except Exception:
                    pass

        if cache is not None:
            cache[_CACHE_KEY] = result
        return result

    def _maybe_run_diarization(
        self,
        audio_data: Any,
        whisper_segments: list[dict[str, Any]],
        *,
        is_preview: bool,
        diarize: bool | None = None,
    ) -> dict[str, Any]:
        """Пытается проставить спикеров для файловой транскрибации.

        Решение сделано мягким: любая ошибка diarization логируется и попадает в
        результат как служебное поле, но не ломает базовую STT-транскрибацию.

        Args:
            diarize: Explicit override from caller (e.g. Transcriber skips when no HF_TOKEN).
                     When None, falls back to settings.DIARIZATION_ENABLED.
        """
        base_result: dict[str, Any] = {
            "enabled": False,
            "speaker_segments": [],
            "annotated_segments": [],
            "speaker_turns": [],
        }
        _diarize_enabled = diarize if diarize is not None else settings.DIARIZATION_ENABLED
        if is_preview or not _diarize_enabled:
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
            # Phase B.2: diarization.pipeline_fail — inference error (not startup/no_token)
            self._push_error(
                "diarization.pipeline_fail",
                f"{type(exc).__name__}: {exc}",
                severity="warn",
            )
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
        """Ленивая загрузка pyannote pipeline с токеном Hugging Face.

        W1235: double-checked locking — предотвращает повторную загрузку (~3 GB)
        при конкурентных вызовах из IPC-потока и REST-сервера (W1227 F1 HIGH).
        """
        # Fast-path: без блокировки (pipeline уже загружен или ошибка зафиксирована).
        if self._diarization_pipeline is not None:
            return self._diarization_pipeline
        if self._diarization_load_error:
            raise RuntimeError(self._diarization_load_error)

        with self._diarization_load_lock:
            # Re-check после получения блокировки: другой поток мог уже загрузить.
            if self._diarization_pipeline is not None:
                return self._diarization_pipeline
            if self._diarization_load_error:
                raise RuntimeError(self._diarization_load_error)

            hf_token = os.environ.get("HF_TOKEN") or settings.HF_TOKEN or None

            # Используем ленивую инициализацию, чтобы не тянуть модель в realtime-пути.
            # Если HF_HUB_OFFLINE=1, модель загружается из кэша без token.
            # Span фиксируется только при первом реальном load'е (guard выше гарантирует
            # что повторные вызовы сразу возвращают кэш).
            candidates_raw = getattr(settings, "DIARIZATION_MODEL_CANDIDATES", "") or ""
            candidates = [c.strip() for c in candidates_raw.split(",") if c.strip()]
            if not candidates:
                candidates = [settings.DIARIZATION_MODEL]

            last_error: Exception | None = None
            for model_name in candidates:
                with _profiler.start_span(f"model_load_{_short_model_name(model_name)}"):
                    try:
                        kwargs = {"token": hf_token} if hf_token else {}
                        pipeline = Pipeline.from_pretrained(model_name, **kwargs)
                    except Exception as e:
                        # Пер-кандидатная семантика: провал одного кандидата не
                        # блокирует следующих; латч ставится после провала ВСЕХ.
                        last_error = e
                        logger.warning(
                            "Diarization: кандидат %s не загрузился: %s",
                            model_name, str(e)[:200],
                        )
                        continue
                    diarization_device = self._resolve_diarization_device()
                    pipeline.to(diarization_device)
                    self._diarization_pipeline = pipeline
                    self._diarization_active_model = model_name
                    logger.info(
                        "Diarization pipeline (%s) загружен на устройство %s",
                        model_name, diarization_device,
                    )
                    return self._diarization_pipeline

            self._diarization_load_error = (
                f"Не удалось загрузить pyannote pipeline "
                f"(кандидаты: {', '.join(candidates)}): {last_error}"
            )
            raise RuntimeError(self._diarization_load_error)

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
        import gc
        prepared_audio_path, should_cleanup = self._prepare_audio_for_diarization(audio_path)
        try:
            with self._diarization_run_lock:
                pipeline = self._load_diarization_pipeline()
                try:
                    diarization = pipeline(prepared_audio_path)
                finally:
                    # Очистка MPS остаётся частью той же critical section:
                    # следующий pyannote-вызов не должен стартовать посреди
                    # освобождения общих Metal-ресурсов.
                    gc.collect()
                    if (
                        torch is not None
                        and hasattr(torch, "mps")
                        and torch.backends.mps.is_available()
                    ):
                        try:
                            torch.mps.empty_cache()
                        except Exception:
                            pass
        except Exception:
            logger.exception("FATAL: Unhandled exception in diarization pipeline")
            raise
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

    def diarize_window(self, audio_path: str) -> dict[str, Any]:
        """Узкий хелпер C2b (спека §2.5a): диаризация КОРОТКОГО окна встречи.

        Один прогон pipeline даёт и сегменты, и центроиды спикеров окна
        (pyannote 4.x: DiarizeOutput.speaker_embeddings, wespeaker 256-dim,
        порядок строк = diarization.labels()). NaN-строки (спикер без чистых
        фреймов) отбрасываются. НЕ трогает _maybe_run_diarization/phase C.

        Returns: {"segments": [{start, end, speaker}], "speaker_embeddings":
        {label: list[float]}} — времена относительны начала окна.
        """
        import gc
        with self._diarization_run_lock:
            pipeline = self._load_diarization_pipeline()
            try:
                out = pipeline(audio_path)
            finally:
                # Паттерн утечки MPS — как в _run_diarization_impl.
                gc.collect()
                if (
                    torch is not None
                    and hasattr(torch, "mps")
                    and torch.backends.mps.is_available()
                ):
                    try:
                        torch.mps.empty_cache()
                    except Exception:
                        pass
        diarization = getattr(out, "speaker_diarization", out)
        labels = list(diarization.labels())
        raw_emb = getattr(out, "speaker_embeddings", None)
        embeddings: dict[str, list[float]] = {}
        if raw_emb is not None:
            arr = np.asarray(raw_emb, dtype=np.float32)
            for i, label in enumerate(labels):
                if i < arr.shape[0] and not np.isnan(arr[i]).any():
                    embeddings[str(label)] = arr[i].tolist()
        segments: list[dict[str, Any]] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker": str(speaker),
            })
        return {"segments": segments, "speaker_embeddings": embeddings}

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
        """Облачная (cloud) транскрибация через уже захардненную `backend.cloud_stt`.

        Вызывается ТОЛЬКО из fallback chain, когда settings.NETWORK_MODE !=
        "offline_strict" (владелец явно разрешил выход в сеть). Переиспользует
        существующую абстракцию облачных STT-провайдеров (`get_cloud_stt_provider`
        — rate-limits, SSRF-guards, capped reads) — тот же провайдер, что уже
        используют WS `/v1/stream` мост Voice Gateway (`backend/rest_server.py`).

        Раньше здесь был мёртвый scaffold: POST на `settings.STT_GATEWAY_URL`,
        который указывал на локальный OpenClaw gateway (`/v1/chat/completions`
        хост:порт) — тот НЕ реализует `/v1/audio/transcriptions` → гарантированный
        404 на каждый вызов (см. logs/krab-ear-rest.err.log).

        PRIVACY CONTRACT: privacy_mode_enabled ВСЕГДА побеждает NETWORK_MODE —
        если включён режим приватности, аудио НИКУДА не отправляется, даже если
        владелец уже разрешил сеть через NETWORK_MODE (симметрично
        `_cloud_rewrite_allowed`, которая та же гарантия для облачного rewriter).

        audio_data может быть str/Path (путь к существующему WAV файлу) или
        numpy.ndarray (raw audio buffer из live recording, 16kHz mono float32,
        см. `_audio_data_to_pcm16`). `prompt` не передаётся облачным провайдерам
        напрямую (REST API OpenAI/Deepgram/AssemblyAI не поддерживают whisper-style
        initial_prompt в этом виде) — параметр сохранён для совместимости с
        вызывающим кодом (`_maybe_multipass_retry`, `_transcribe_with_fallback_impl`).
        """
        if self._settings_get("privacy_mode_enabled", False):
            raise RuntimeError(
                "Remote STT заблокирован: privacy_mode_enabled=True — аудио не "
                "должно покидать устройство в режиме приватности"
            )

        from backend.cloud_stt import get_cloud_stt_provider  # noqa: PLC0415 — lazy import, mirrors _cloud_rewrite_allowed

        provider_name = str(self._settings_get("cloud_stt_provider", "openai") or "openai")
        provider = get_cloud_stt_provider(provider_name)
        if provider is None:
            raise RuntimeError(f"Remote STT: неизвестный cloud_stt_provider '{provider_name}'")

        pcm_bytes, sample_rate = self._audio_data_to_pcm16(audio_data)
        source_lang = settings.TRANSCRIBE_LANGUAGE or "auto"

        result = provider.transcribe(pcm_bytes, sample_rate, source_lang)
        if "error" in result:
            error_code = result.get("error")
            log_method = logger.info if error_code == "no_api_key" else logger.error
            log_method(
                "Remote STT не сработал (провайдер=%s): %s %s",
                provider_name, error_code, result.get("message", ""),
                extra={"provider": provider_name, "error_code": error_code},
            )
            raise RuntimeError(
                f"Remote STT ({provider_name}) недоступен: {error_code}"
            )

        # Privacy audit trail: аудио покинуло устройство (симметрично cloud_rewrite).
        try:
            from backend.privacy_audit import get_privacy_audit_logger  # noqa: PLC0415
            get_privacy_audit_logger().log_event(
                category="cloud_stt",
                action="cloud_stt_used",
                details={"provider": provider_name, "language": source_lang},
            )
        except Exception:
            pass  # audit trail must never break transcription

        return {"text": result.get("text", ""), "engine": "remote"}

    @staticmethod
    def _audio_data_to_pcm16(audio_data: Any) -> tuple[bytes, int]:
        """Конвертирует audio_data (WAV-путь или live float32 ndarray) в (pcm16_bytes, sample_rate).

        Live buffer уже 16kHz mono float32 в диапазоне [-1.0, 1.0] — конвертируется
        через clip+scale (тот же паттерн, что `core/pipeline/stt_gigaam.py::_write_wav`
        и `backend/tts_service.py`). Файловый путь читается через soundfile с
        НАТИВНЫМ sample rate файла — без ресемплинга, т.к. `CloudSTTProvider.transcribe`
        принимает произвольный `sample_rate` явным параметром и провайдеры (OpenAI
        WAV-header, Deepgram query-param) корректно используют переданное значение;
        многоканальный звук сводится в моно усреднением каналов.
        """
        if isinstance(audio_data, np.ndarray):
            clipped = np.clip(audio_data, -1.0, 1.0)
            pcm = (clipped * 32767.0).astype(np.int16)
            return pcm.tobytes(), 16000

        if isinstance(audio_data, (str, bytes, os.PathLike)):
            import soundfile as sf
            samples, sample_rate = sf.read(str(audio_data), dtype="int16", always_2d=False)
            if samples.ndim > 1:
                samples = samples.astype(np.int32).mean(axis=1).astype(np.int16)
            return samples.tobytes(), int(sample_rate)

        raise TypeError(
            f"_transcribe_remote: unsupported audio_data type {type(audio_data).__name__}"
        )

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
        cmd.extend(["--", text])
        subprocess.run(cmd, check=False)
