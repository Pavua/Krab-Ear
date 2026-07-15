"""Централизованная конфигурация Krab Ear на базе Pydantic-Settings.

Все параметры могут быть переопределены через переменные окружения (.env
или ~/Library/Application Support/KrabEar/.secrets).

**Runtime overrides из settings.json**: backend's `set_settings` IPC
сохраняет runtime изменения в `~/Library/Application Support/KrabEar/settings.json`
(управляется через `backend.settings_service`). Чтобы pydantic Settings
видел эти overrides без backend restart, мы автоматически читаем JSON
ПОСЛЕ env vars + .env, перед finalizing instance.

Приоритет (high → low):
  1. Launchd / shell env vars (`KRAB_EAR_*`)
  2. .env / .secrets files
  3. settings.json runtime overrides ← bridged here
  4. Class defaults
"""

import json as _json
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import List, Any

# Абсолютный путь к .secrets — backend загружает его на старте через
# pydantic-settings env_file tuple. Порядок загрузки в env_file:
# сначала repo-local .env, затем .secrets — в pydantic-settings v2
# последний файл в tuple побеждает при конфликте ключей. Env vars из
# launchd plist всё равно имеют более высокий приоритет (env > env_file).
_SECRETS_FILE = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"

# Settings.json file managed by backend.settings_service. Read at startup
# чтобы pydantic Settings видел runtime overrides из IPC `set_settings`.
_SETTINGS_JSON_FILE = Path.home() / "Library" / "Application Support" / "KrabEar" / "settings.json"


def _load_settings_json_overrides() -> dict[str, Any]:
    """Читает settings.json и нормализует ключи в UPPER_CASE для pydantic match.

    settings.json использует lowercase ключи (`stt_gigaam_enabled`),
    pydantic Settings — UPPER_CASE (`STT_GIGAAM_ENABLED`). Метод upper'ит
    ключи и возвращает dict для применения как pydantic init kwargs.
    Не raise при missing file / parse error — silent fallback на defaults.
    """
    if not _SETTINGS_JSON_FILE.exists():
        return {}
    try:
        with open(_SETTINGS_JSON_FILE) as f:
            raw = _json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Убираем nested dicts (pydantic Settings нашего класса flat) и не-валидные значения.
    return {
        k.upper(): v for k, v in raw.items()
        if isinstance(k, str) and not isinstance(v, (dict, list))
        # Кроме списков — некоторые settings (например MODEL_MAX_LIST) принимают list.
    } | {
        k.upper(): v for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }


class Settings(BaseSettings):
    # --- Bulk re-process history (backend/bulk_reprocess.py) ---
    BULK_REPROCESS_BATCH_SIZE: int = 5

    # --- Realtime partial transcription overlay ---
    REALTIME_PARTIAL_ENABLED: bool = True
    RT_PARTIAL_INTERVAL_SEC: float = 3.0
    RT_PARTIAL_BUFFER_SEC: float = 8.0
    # --- Мониторинг дискового пространства (backend/disk_monitor.py) ---
    # True = фоновый поток DiskSpaceMonitor запускается при старте backend.
    DISK_MONITOR_ENABLED: bool = True
    # Интервал проверки в минутах.
    DISK_CHECK_INTERVAL_MIN: int = 30
    # Порог для события disk.warning (свободно меньше N GB).
    DISK_WARNING_GB: float = 5.0
    # Порог для события disk.critical (свободно меньше N GB).
    DISK_CRITICAL_GB: float = 1.0
    # Порог для события disk.history_large (history.ndjson > N MB).
    HISTORY_LARGE_MB: int = 500
    # Авто-очистка записей: opt-in, по умолчанию выключена.
    AUTO_CLEANUP_ENABLED: bool = False
    # Удалять записи старше N дней при авто-очистке.
    AUTO_CLEANUP_AFTER_DAYS: int = 365

    # --- Event-мост IPC->REST (backend/event_bridge.py, spec 2026-07-07) -------
    # True = EventBridge доставляет события из IPC-процесса в REST-процесс.
    # Killswitch, читается ОДИН РАЗ при старте (как DISK_MONITOR_ENABLED) —
    # НЕ live-toggle через set_settings.
    EVENT_BRIDGE_ENABLED: bool = True

    model_config = SettingsConfigDict(
        env_prefix="KRAB_EAR_",
        env_file=(".env", str(_SECRETS_FILE)),
        extra="ignore",
    )

    # Директории
    DATA_DIR: Path = Path.home() / ".krab_ear_data"

    # --- REST server bind port (backend/rest_server.py) -------------------------
    # ОДИН источник правды для порта REST — event_bridge.py читает то же самое
    # значение, чтобы не дублировать литерал 5005 в двух модулях.
    REST_SERVER_PORT: int = 5005

    # Модели STT
    MODEL_BALANCED: str = "mlx-community/whisper-large-v3-turbo"
    MODEL_MAX_CANDIDATES: str = "mlx-community/whisper-large-v3-mlx,mlx-community/whisper-large-v3-turbo"

    # Промпты и язык
    TRANSCRIBE_PROMPT: str = "Ты транскрибируешь русскую речь. Сохраняй смысл, ставь корректную пунктуацию и заглавные буквы."
    TRANSCRIBE_LANGUAGE: str = "ru"
    HF_TOKEN: str = ""
    DIARIZATION_ENABLED: bool = True
    DIARIZATION_MODEL: str = "pyannote/speaker-diarization-3.1"

    # Сетевые настройки
    # "offline_strict" — локальный MLX only, без Remote STT fallback.
    # "offline_default" / "online_preferred" — разрешают fallback на Cloud STT
    # (backend/cloud_stt.py: openai|deepgram|assemblyai, провайдер — см.
    # DEFAULT_SETTINGS["cloud_stt_provider"]). Дефолт strict: это явный opt-in,
    # т.к. отправляет аудио за пределы устройства (privacy_mode_enabled
    # ВСЕГДА побеждает даже если NETWORK_MODE разрешает сеть — engine.py
    # AudioEngine._transcribe_remote).
    NETWORK_MODE: str = "offline_strict"
    GATEWAY_URL: str = "http://127.0.0.1:18789/v1/chat/completions"
    AI_MODEL: str = "google/gemini-2.0-flash"

    # Лимиты
    MAX_AUDIO_MB: int = 1000
    MAX_DURATION_SEC: int = 300
    # TRANSCRIBE_TIMEOUT_SEC — верхний предел на одну попытку STT одной моделью.
    # Для часового файла whisper-large-v3 на M4 Max: ~10-20 мин; на max profile —
    # до 30 мин на кандидата. 300с (5 мин) покрывали только короткие диктовки.
    # 3600с хватает на 1-часовой файл + запас для max-candidates chain.
    TRANSCRIBE_TIMEOUT_SEC: int = 3600

    # TTS
    SAY_VOICE: str = ""

    # Voice Gateway
    VOICE_GATEWAY_URL: str = "http://127.0.0.1:8090"

    # D.10a LM Studio integration (OpenAI-compatible LLM rewriter)
    LLM_ENABLED: bool = False
    LLM_BASE_URL: str = "http://localhost:1234/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gemma-4-e4b-it-mlx"
    LLM_TIMEOUT_SEC: float = 240.0  # was 120.0 → bumped fix/lm-studio-warmup: External SSD cold-load gemma-4-26b-a4b-it-optiq ~3-4 min after 30min idle JIT eviction; 240s covers worst-case SSD cold load + queue.
    LLM_CIRCUIT_FAIL_THRESHOLD: int = 3
    LLM_CIRCUIT_INITIAL_RESET_SEC: int = 60
    LLM_CIRCUIT_MAX_RESET_SEC: int = 600
    # Fallback chain: ordered list of model names tried when primary fails.
    # Empty list = no fallback (legacy behaviour — degrade straight to raw text).
    # Comma-separated when overridden via env var KRAB_EAR_LLM_FALLBACK_CHAIN.
    LLM_FALLBACK_CHAIN: List[str] = [
        "qwen3-4b-instruct",
        "llama-3.2-3b-instruct",
    ]

    # Voice Assistant brain — большая модель для интерактивного разговора
    # (Phase 1 VA через OpenClaw). Когда STT recording активен, brain выгружается
    # из LM Studio чтобы освободить ~19-20 GB unified memory под Whisper+pyannote.
    # После stop_recording brain pre-loadится обратно.
    # Пустая строка = lifecycle hook отключён (single-tier setup).
    LLM_BRAIN_MODEL: str = ""
    # Автоматически unload brain при start_recording (освобождает память для STT).
    LLM_BRAIN_UNLOAD_ON_RECORDING: bool = True
    # Автоматически pre-load brain при stop_recording (warm для VA conversation).
    LLM_BRAIN_PRELOAD_ON_STOP: bool = True

    # Авто-резервное копирование
    AUTO_BACKUP_ENABLED: bool = True

    # Авто-экспорт истории по расписанию
    AUTO_EXPORT_ENABLED: bool = False
    # Включать метки спикеров в экспортах (MD/SRT/JSON/CSV/Obsidian).
    # False = обратная совместимость. include_speaker_labels в IPC-запросе имеет приоритет.
    EXPORT_INCLUDE_SPEAKER_LABELS: bool = False

    # Формат логов: "text" (стандартный) или "json" (структурированный JSON)
    LOG_FORMAT: str = "text"

    # REST API: опциональный ключ аутентификации для защищённых эндпоинтов.
    # Пустая строка = аутентификация отключена (обратная совместимость).
    # Если задан, защищённые эндпоинты требуют заголовок: Authorization: Bearer <key>
    REST_API_KEY: str = ""

    # Включить расширенное управление токенами (create/list/revoke через IPC).
    # Когда True, require_api_key использует RestAuth вместо одиночного ключа.
    REST_API_AUTH_ENABLED: bool = False

    # Rate limiting для REST API (flask-limiter).
    # False = rate limiting полностью отключён (удобно для тестов и локальной разработки).
    RATE_LIMIT_ENABLED: bool = True

    # CORS: список разрешённых Origins через запятую.
    # По умолчанию — явный localhost-allowlist (wave-21 MED fix: wildcard "*" позволял
    # любой странице читать транскрипты через EventSource/fetch с localhost:5005).
    # Чтобы разрешить все origins (локальная разработка): KRAB_EAR_CORS_ORIGINS="*"
    # Пример: "http://localhost:3000,https://app.example.com"
    CORS_ORIGINS: str = "http://127.0.0.1,http://localhost"

    # --- Адаптивное шумоподавление (Adaptive Denoising) ---
    # При STT_DENOISE_ENABLED=True: перед транскрибацией оцениваем SNR аудио через
    # NoiseProfiler. Если SNR < STT_DENOISE_SNR_THRESHOLD_DB → применяем деноизер.
    # Деноизер: noisereduce (если установлен) или встроенный spectral gating (scipy).
    # STT_DENOISE_STRENGTH: уровень подавления — "off"|"light"|"moderate"|"strong".
    STT_DENOISE_ENABLED: bool = True
    STT_DENOISE_SNR_THRESHOLD_DB: float = 15.0
    STT_DENOISE_STRENGTH: str = "moderate"

    # При STT_GAIN_NORMALIZE_ENABLED=True: после шумоподавления выравниваем
    # уровень громкости через GainNormalizer.auto_gain (target -20 dBFS RMS).
    STT_GAIN_NORMALIZE_ENABLED: bool = True

    # Умный пропуск тишины: удалять длинные паузы (>1 с) перед STT.
    SMART_SILENCE_SKIP_ENABLED: bool = False

    # --- Realtime silence filter (RealtimeSilenceFilter) ---
    REALTIME_SILENCE_FILTER_ENABLED: bool = False
    RT_SILENCE_CHECK_SEC: float = 5.0
    RT_SILENCE_WINDOW_SEC: float = 10.0
    RT_SILENCE_MAX_SEC: float = 8.0

    # --- Confidence-driven multi-pass STT retry ---
    # Если первый pass (balanced) вернул уверенность ниже порога → пробуем тяжелее.
    # Threshold 0.65 покрывает типичные "плохие" результаты (0.3-0.6) без лишних ретраев.
    # Установить в 0.0 — никогда не ретраить. 1.0 — ретраить всегда.
    STT_MULTIPASS_ENABLED: bool = True
    STT_MIN_CONFIDENCE_THRESHOLD: float = 0.65
    # Максимальное число дополнительных попыток поверх первого pass (balanced).
    # 2 = balanced + max + remote (при network_mode != offline_strict).
    STT_MAX_RETRIES: int = 2

    # Pipeline v2 feature flag.
    # True = BackendService использует transcribe_v2() (pipeline-based path).
    # False = legacy path через AudioEngine.transcribe() напрямую (по умолчанию).
    PIPELINE_V2: bool = False

    # IPC throttle: защита от злоупотребления тяжёлыми IPC-методами.
    # False = throttling полностью отключён (удобно для тестов и локальной разработки).
    IPC_THROTTLE_ENABLED: bool = True

    # IPC request signing: HMAC-SHA256 верификация входящих запросов.
    # False = подпись отключена (обратная совместимость, дефолт для локальной разработки).
    # True = все входящие IPC-запросы должны содержать поля signature, timestamp, nonce.
    # Секрет задаётся через KRAB_EAR_IPC_SIGNING_SECRET или .secrets файл.
    IPC_SIGNING_ENABLED: bool = False
    IPC_SIGNING_SECRET: str = ""

    # --- Voice Assistant wake word (Phase 1.5) ---
    # По умолчанию выключено — приватность.
    # Требует Porcupine AccessKey (KRAB_EAR_PORCUPINE_ACCESS_KEY) +
    # кастомный .ppn файл «Краб» (тренировка на https://console.picovoice.ai).
    WAKE_WORD_ENABLED: bool = False
    PORCUPINE_ACCESS_KEY: str = ""
    # Движок wake word: "openwakeword" | "porcupine" | "disabled"
    # "openwakeword" — free, Apache 2.0, без email/signup (встроенные: alexa,
    #   hey_mycroft, hey_jarvis; кастомные .onnx в {DATA_DIR}/wake_word_models/).
    # "porcupine" — высокая точность, требует AccessKey + .ppn файл «Краб».
    # "disabled" — wake word полностью выключен.
    WAKE_WORD_ENGINE: str = "disabled"

    # --- SenseVoice adapter (Phase 4 quick win) ---
    # Alibaba SenseVoice (FunASR) — альтернативный STT движок, поддерживает 50+ языков
    # (вкл. RU) и эмоцию (happy/sad/angry/neutral/fearful/disgusted/surprised).
    # Opt-in: по умолчанию выключено — существующий whisper chain не меняется.
    # При SENSEVOICE_ENABLED=True адаптер добавляется в fallback chain МЕЖДУ balanced
    # и max (то есть пробуется до whisper-large-v3). Требует `pip install funasr`.
    # Если funasr не установлен — адаптер мягко возвращает ошибку и fallback
    # продолжает по whisper-кандидатам.
    SENSEVOICE_ENABLED: bool = False
    SENSEVOICE_MODEL: str = "iic/SenseVoiceSmall"
    # При True результат эмоции (happy/neutral/angry/...) пробрасывается в
    # HistoryItem.emotion и далее в NDJSON. Для аналитики настроений.
    SENSEVOICE_EMOTION_TO_HISTORY: bool = True

    # --- Parakeet-TDT-1.1B adapter (Phase 4.2, NVIDIA NeMo) ---
    # NVIDIA Parakeet-TDT-1.1B — топ OpenASR leaderboard (English). Превосходит
    # whisper-large-v3 по WER на EN. Opt-in, по умолчанию выключено.
    # Позиция в chain: МЕЖДУ balanced (whisper-turbo) и SenseVoice.
    # Требует `pip install nemo-toolkit[asr]`. На Apple Silicon (M-серия) работает
    # через PyTorch MPS (CPU fallback). CUDA не обязателен.
    # Если nemo не установлен — адаптер мягко возвращает ошибку и chain продолжается.
    PARAKEET_ENABLED: bool = False
    PARAKEET_MODEL: str = "nvidia/parakeet-tdt-1.1b"

    # --- WhisperX adapter (Phase 4.3) ---
    # whisperx — Community wrapper над whisper-large-v3 от m-bain.
    # Добавляет word-level timestamps (точная атрибуция слов по времени)
    # и native diarization через pyannote (заменяет наш прямой pyannote путь).
    # Opt-in: по умолчанию выключено — существующий whisper chain не меняется.
    # При WHISPERX_ENABLED=True адаптер добавляется в fallback chain ПОСЛЕ
    # SenseVoice и ПЕРЕД max-candidates whisper-large-v3.
    # Requires: pip install whisperx
    # Device: "mps" на Apple Silicon (автодетект), "cpu" как fallback.
    # Примечание: whisperx на MPS запускает torch (не MLX), поэтому потребляет
    # ~3-4 GB RAM; word alignment требует отдельную модель (~200 MB).
    WHISPERX_ENABLED: bool = False
    WHISPERX_MODEL: str = "large-v3"
    # Device для инференса: "mps" | "cpu" | "cuda".
    # "mps" — использует Apple Neural Engine / GPU через torch MPS backend.
    # Если MPS недоступен (CI/Linux) — адаптер автоматически падает на "cpu".
    WHISPERX_DEVICE: str = "mps"
    # Включить diarization через pyannote внутри whisperx.
    # Требует HF_TOKEN (pyannote/speaker-diarization-3.1 — gated model).
    WHISPERX_DIARIZATION: bool = True
    # Включить word-level timestamps (phoneme alignment).
    # При True: результат содержит word_timestamps в HistoryItem.
    WHISPERX_WORD_TIMESTAMPS: bool = True

    # --- Voxtral Mini 4B Realtime adapter (Phase 4.4) ---
    # Mistral Voxtral-Mini-4B-Realtime-2602 — мультиязычная (RU/ES/EN + 10 других) STT-модель
    # с встроенным семантическим reasoning (Q&A, summarisation, function calling).
    # Архитектура: audio encoder (970M) + Mistral Small 3.1 LM decoder (3.4B) = ~4B params.
    # Размер: BF16 ~8.9 GB, 4-bit quant ~2–3 GB (MLX community port).
    # M4 Max 36 GB: любой вариант влезает. Рекомендуем 4-bit для экономии памяти.
    # Лицензия: Apache 2.0 — коммерческое использование ОК.
    # Opt-in: по умолчанию выключено. При VOXTRAL_ENABLED=True адаптер добавляется
    # в fallback chain ПОСЛЕ WhisperX и ПЕРЕД max-candidates whisper-large-v3.
    # Требует: pip install mistral-inference (или mlx-lm + mlx-audio для MLX-варианта).
    # Если библиотека не установлена — адаптер мягко пропускается, chain продолжается.
    # Latency: 480ms recommended (офлайн-качество); диапазон 80ms–2.4s конфигурируемый.
    VOXTRAL_ENABLED: bool = False
    VOXTRAL_MODEL: str = "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit"
    # При VOXTRAL_REASONING_ENABLED=True модель дополнительно возвращает reasoning-поле
    # (summary или Q&A) в HistoryItem.reasoning. Требует ~10–20% дополнительного времени.
    # При False Voxtral работает только как STT (без reasoning overhead).
    VOXTRAL_REASONING_ENABLED: bool = False

    # --- GigaAM-RNNT v2 adapter (RU-специализированная модель от Sber) ---
    # GigaAM — Conformer-based модель (244M параметров), дообученная на 50 000 часах
    # русскоязычной речи. WER на Common Voice RU:
    #   GigaAM-RNNT v2: ~3.8%  vs  whisper-large-v3: ~9.8% (≈2.5× улучшение).
    # Лицензия: MIT — коммерческое использование разрешено.
    # PyPI: pip install gigaam  (официальный пакет от salute-developers)
    # HuggingFace: salute-developers/GigaAM
    # Opt-in: по умолчанию выключено до проверки установки gigaam.
    # Когда STT_GIGAAM_ENABLED=True И STT_LANGUAGE_ROUTING_ENABLED=True:
    #   detected_lang == "ru" → GigaAM → fallback whisper-large-v3.
    # Использует PyTorch + MPS (не MLX) → mlx_lock НЕ нужен.
    # Потребление памяти: ~1 GB (244M float32 params) + ~200 MB torch runtime.
    STT_GIGAAM_ENABLED: bool = False
    # Режим модели: "rnnt" (выше качество, RNNT decoder) или "ctc" (быстрее, CTC decoder).
    # Полные имена тоже поддерживаются: "v2_rnnt", "v2_ctc", "v1_rnnt", "v1_ctc".
    STT_GIGAAM_MODE: str = "rnnt"
    # Устройство для инференса: "cpu" (default, рекомендуется) или "mps" (Apple Silicon GPU).
    # Bench 2026-04-26 на M4 Max: CPU 0.62s vs MPS 4.36s на 15-сек fragment (RTF
    # 0.041 vs 0.291). MPS медленнее из-за warmup + tensor transfer overhead на
    # коротких inference; для длинных audio (>60s) может быть другая story —
    # требуется отдельный bench когда longform доступен (HF_TOKEN setup).
    # См. memory/reference_gigaam_bench_2026-04-26.md.
    STT_GIGAAM_DEVICE: str = "cpu"
    # Транспорт для запуска инференса:
    #   "in_process" — `import gigaam` в текущем Python. Работает только если gigaam
    #                  установлен в активном venv. В main Krab Ear venv (Python 3.14
    #                  + torch 2.11) gigaam несовместим (pin torch<=2.5.1 / onnxruntime<=1.23.x).
    #   "subprocess" — запускает gigaam_worker.py из изолированного venv
    #                  (по умолчанию ~/.venv_krab_ear_gigaam, см.
    #                  scripts/install_gigaam_venv.command). Worker держит модель в
    #                  памяти, общается через stdin/stdout JSON.
    #   "auto" (default) — пробует in_process; при ImportError → subprocess.
    STT_GIGAAM_TRANSPORT: str = "auto"
    # Путь к Python интерпретатору изолированного venv с установленным gigaam.
    # Используется только при transport in {"subprocess", "auto"}.
    # Пустая строка = дефолт ~/.venv_krab_ear_gigaam/bin/python.
    STT_GIGAAM_VENV_PYTHON: str = ""
    # HuggingFace API token (read access). Нужен для transcribe_longform() в gigaam,
    # которая использует pyannote VAD для long audio (>30s). Если пустой —
    # fallback на ~/.cache/huggingface/token. См. reference_gigaam_bench_2026-04-26.
    STT_GIGAAM_HF_TOKEN: str = ""

    # --- Parakeet MLX adapter (EN-only, Apple Silicon) ---
    # NVIDIA Parakeet TDT 0.6B via parakeet-mlx (MLX port).
    # Install: pip install parakeet-mlx
    # Opt-in: выключено по умолчанию (lib не ubiquitous, EN-only).
    # Когда STT_PARAKEET_ENABLED=True И is_available() → подключается в STTRouter
    # как EN-специализированный адаптер (приоритет после GigaAM при lang=="en").
    STT_PARAKEET_ENABLED: bool = False
    # HuggingFace repo ID для MLX Parakeet модели.
    STT_PARAKEET_MODEL: str = "mlx-community/parakeet-tdt-0.6b-v2"

    # --- SenseVoice adapter (East Asian multilingual — zh/yue/ja/ko/en) ---
    # FunAudioLLM/SenseVoiceSmall via funasr package. PyTorch + MPS (NOT MLX).
    # Install: pip install funasr
    # HuggingFace: FunAudioLLM/SenseVoiceSmall (~250 MB)
    # Opt-in: выключено по умолчанию. Включить для East Asian language transcription.
    # Когда STT_SENSEVOICE_ENABLED=True И is_available() → добавляется в STTRouter
    # ПЕРЕД Whisper (более высокий приоритет для zh/yue/ja/ko, acceptable для en).
    # mlx_lock НЕ нужен — PyTorch runtime, не MLX.
    STT_SENSEVOICE_ENABLED: bool = False
    # HuggingFace repo ID или локальный путь к модели.
    STT_SENSEVOICE_MODEL: str = "FunAudioLLM/SenseVoiceSmall"
    # Устройство для инференса: "mps" (Apple Silicon GPU), "cpu", или "auto".
    # "auto" выбирает MPS при наличии torch.backends.mps.is_available().
    STT_SENSEVOICE_DEVICE: str = "auto"

    # --- Voice fingerprint matching ---
    # Включить сопоставление голосовых отпечатков между записями через pyannote/embedding.
    # По умолчанию выключено (opt-in); требует pyannote.audio.
    VOICE_FINGERPRINT_ENABLED: bool = False
    VOICE_FINGERPRINT_MATCH_THRESHOLD: float = 0.75
    # --- Quick Edit before paste ---
    # Показывать мини-оверлей для правки текста перед вставкой.
    QUICK_EDIT_BEFORE_PASTE_ENABLED: bool = False
    QUICK_EDIT_TIMEOUT_SEC: float = 5.0

    # --- Telegram Bridge (Krab Ear → main Krab userbot) ---
    # Мост для отправки транскрипций напрямую в Telegram через main Krab web-панель.
    # False = функция "Отправить в Telegram" в UI скрыта / недоступна.
    TELEGRAM_BRIDGE_ENABLED: bool = True
    # URL web-панели main Krab. Default = WEB_PORT 8080 (см. src/bootstrap/runtime.py).
    # Переопределяется через KRAB_EAR_TELEGRAM_BRIDGE_URL.
    TELEGRAM_BRIDGE_URL: str = "http://localhost:8080"
    # Таймаут HTTP-запроса к main Krab (секунды).
    TELEGRAM_BRIDGE_TIMEOUT_SEC: float = 5.0
    # Circuit breaker: сколько ошибок подряд до размыкания.
    TELEGRAM_BRIDGE_CB_FAIL_THRESHOLD: int = 3
    # Circuit breaker: сколько секунд держать разомкнутым.
    TELEGRAM_BRIDGE_CB_RESET_SEC: float = 60.0

    # --- Sentry / GlitchTip crash telemetry ---
    # Пустой DSN = интеграция полностью отключена (no-op).
    # Совместимо с self-hosted GlitchTip (Sentry-compatible protocol).
    # Задайте через KRAB_EAR_SENTRY_DSN или ~/Library/Application Support/KrabEar/.secrets
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = "production"

    # --- STT punctuation-only LLM pass (opt-in) ---
    # При True: после STT + cleanup и ПЕРЕД полным LLM rewrite, текст прогоняется
    # через минимальный pass с строгим prompt'ом "только пунктуация".
    # Word-set и word-count guards гарантируют, что слова не меняются.
    # Opt-in: False по умолчанию до завершения burn-in периода.
    STT_PUNCTUATION_LLM_PASS_ENABLED: bool = False

    # --- STT quality: speaker-aware initial_prompt hint ---
    STT_SPEAKER_AWARE_PROMPT_ENABLED: bool = True
    STT_DIALOGUE_HINT_THRESHOLD: int = 2

    # --- Russian Whisper fine-tune ---
    STT_USE_RU_FINETUNE: bool = False
    STT_RU_FINETUNE_MODEL: str = "antony66/whisper-large-v3-russian"
    # --- Push-to-talk (hold) режим ---
    # "toggle": одно нажатие — старт, следующее — стоп (классический режим).
    # "hold":   зажал клавишу — запись; отпустил — стоп + транскрибация.
    HOTKEY_MODE: str = "toggle"
    # Минимальная длительность удержания в hold-режиме (мс).
    # Нажатия короче этого порога игнорируются (случайные касания).
    HOLD_MIN_DURATION_MS: int = 200

    # --- Dual-mode TTS (Silero RU + Kokoro EN) ---
    # Opt-in: по умолчанию отключено — существующий macOS `say` workflow не меняется.
    # При TTS_ENABLED=True включается Silero (RU primary) + Kokoro (EN fallback) цепочка.
    # Требует: pip install silero (или torch + torchaudio) для RU; pip install kokoro для EN.
    # Если нужные библиотеки не установлены — TTSService мягко падает на macOS `say`.
    TTS_ENABLED: bool = False
    # Silero TTS: модель русского языка. Доступные голоса: aidar, baya, kseniya, xenia, eugene.
    TTS_SILERO_MODEL: str = "v4_ru"
    TTS_SILERO_VOICE: str = "baya"
    # Kokoro-82M: EN fallback. Apache 2.0, ~350 MB, 54 голоса, 8 языков (без RU).
    TTS_KOKORO_MODEL: str = "hexgrad/Kokoro-82M"
    # При True: если Silero/Kokoro недоступны — fallback на macOS `say` (последний резерв).
    TTS_FALLBACK_SAY: bool = True

    # --- Call provider abstraction (Phase 3) ---
    # Выбор активного провайдера телефонии.
    # Допустимые значения: "telnyx" | "twilio" | "none"
    CALL_PROVIDER: str = "telnyx"

    # --- Telnyx SIP/Call Control adapter (Phase 3 step 3/4) ---
    # Прямой REST-fallback к Telnyx Call Control API (без FreeSWITCH).
    # Пустой TELNYX_API_KEY = stub-режим (все методы возвращают ошибку
    # "telnyx_not_configured", никаких реальных запросов не делается).
    # TELNYX_FROM_NUMBER — Telnyx-номер в формате E.164 (например "+15551234567").
    TELNYX_API_KEY: str = ""
    TELNYX_CONNECTION_ID: str = ""
    TELNYX_FROM_NUMBER: str = ""

    # --- Twilio adapter (Phase 3, trial credit MVP) ---
    # Использует Twilio REST API v2010 с Basic Auth (без пакета twilio).
    # TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN = обязательны для работы.
    # TWILIO_FROM_NUMBER — купленный Twilio номер в формате E.164.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    # --- VAD pre-filter перед STT ---
    # При STT_VAD_PREFILTER_ENABLED=True аудио пропускается через VoiceActivityDetector
    # ДО передачи в Whisper. Это устраняет галлюцинации на тишине
    # ("спасибо за просмотр", "subscribe to the channel" и т.п.).
    # Алгоритм:
    #   1. VAD делит аудио на речь/тишину (30ms фреймы, адаптивный порог).
    #   2. Паузы длиннее STT_VAD_SILENCE_TRIM_THRESHOLD_SEC обрезаются до 0.5s.
    #   3. Если суммарная речь < 0.3s → возврат пустого результата без вызова STT.
    STT_VAD_PREFILTER_ENABLED: bool = True
    # Пороговая длина тишины для обрезки (секунды). Паузы длиннее этого значения
    # сжимаются до 0.5s padding. По умолчанию 2.0s.
    STT_VAD_SILENCE_TRIM_THRESHOLD_SEC: float = 2.0

    # --- Streaming chunked transcription (long-form recordings) ---
    # При включённом режиме аудио длиннее stt_streaming_min_audio_sec сек
    # разбивается на чанки с перекрытием и транскрибируется последовательно.
    # Позволяет снизить задержку получения первых результатов и повысить
    # качество на длинных записях (Whisper теряет контекст >30 с).
    # False = выключено по умолчанию (single-pass быстрее для коротких записей).
    STT_STREAMING_ENABLED: bool = False
    # Порог длительности аудио (в секундах), при превышении которого включается
    # chunked режим (если STT_STREAMING_ENABLED=True).
    STT_STREAMING_MIN_AUDIO_SEC: float = 30.0
    # Длина одного чанка в секундах.
    STT_STREAMING_CHUNK_SEC: float = 15.0
    # Перекрытие между соседними чанками в секундах.
    # Используется для детектирования и удаления дублей на швах.
    STT_STREAMING_OVERLAP_SEC: float = 2.0

    # --- STT quality: user hotwords for initial_prompt boost ---
    # Управляется через IPC: add_stt_hotword / remove_stt_hotword / list_stt_hotwords.
    STT_HOTWORDS: List[str] = []

    # --- Language-aware STT router (scaffold, Phase 5 future) ---
    # Маршрутизация на языково-специализированные STT модели.
    # По умолчанию выключено — используется единый whisper-large-v3 для всех языков.
    # При STT_LANGUAGE_ROUTING_ENABLED=True router выбирает модель по определённому языку:
    # RU → STT_RU_PRIMARY_MODEL, EN → STT_EN_PRIMARY_MODEL, ES → STT_ES_PRIMARY_MODEL,
    # другие → STT_OTHER_PRIMARY_MODEL. Интеграция в engine.py — в follow-up PR.
    STT_LANGUAGE_ROUTING_ENABLED: bool = False
    # --- Scored adapter selection (D.2.3) ---
    # "auto_scored" = использовать score function (language match + speed + quality + duration penalty)
    # "legacy"      = сохранить прежний порядок adapter chain из AudioEngine
    STT_ROUTING: str = "auto_scored"
    # Модель по умолчанию для каждого языка. Текущий дефолт = whisper-large-v3 (generalist).
    STT_RU_PRIMARY_MODEL: str = "mlx-community/whisper-large-v3-mlx"
    STT_EN_PRIMARY_MODEL: str = "mlx-community/whisper-large-v3-mlx"
    STT_ES_PRIMARY_MODEL: str = "mlx-community/whisper-large-v3-mlx"
    STT_OTHER_PRIMARY_MODEL: str = "mlx-community/whisper-large-v3-mlx"

    # --- Audio-level Language ID (AudioLanguageID, core/audio_lang_id.py) ---
    # Быстрый encoder-only forward pass через mlx-whisper для определения языка аудио.
    # Берёт первые STT_AUDIO_LANG_ID_PREVIEW_SEC секунд → log-mel → detect_language().
    # Оборачивается в mlx_lock() (thread-safety, CLAUDE.md).
    # При STT_AUDIO_LANG_ID_ENABLED=False или любой ошибке → graceful None → placeholder.
    STT_AUDIO_LANG_ID_ENABLED: bool = True
    # Длина audio preview (секунды) для detect_language inference.
    # Больше = точнее детекция (меньше шанс ошибки на коротких utterances).
    # Меньше = быстрее (~50ms @ 5s vs ~100ms @ 30s).
    STT_AUDIO_LANG_ID_PREVIEW_SEC: float = 5.0

    # --- Авто-сид дефолтных STT hotwords при первом запуске ---
    # При True: backend при старте вызывает seed_hotwords(only_if_empty=True) —
    # заполняет список брендов/терминов только если он пуст (не перезаписывает).
    # Отключить: KRAB_EAR_STT_AUTO_SEED_HOTWORDS=false
    STT_AUTO_SEED_HOTWORDS: bool = True

    # --- Ежедневный дайджест на email (opt-in) ---
    # При RECAP_EMAIL_ENABLED=True: каждый день в RECAP_TIME_HOUR (локальное время)
    # автоматически генерируется DailyDigest и отправляется на RECAP_EMAIL_TO.
    # RECAP_BACKEND: "smtp" (smtplib) или "mail_app" (macOS Mail.app через osascript).
    # SMTP-пароль считывается из macOS Keychain (ключ "KrabEar SMTP password").
    RECAP_EMAIL_ENABLED: bool = False
    RECAP_EMAIL_TO: str = ""
    RECAP_TIME_HOUR: int = 20
    RECAP_BACKEND: str = "smtp"   # "smtp" | "mail_app"
    # SMTP-конфигурация (используется при RECAP_BACKEND="smtp")
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""       # Лучше хранить в Keychain, не здесь
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    # --- Per-app paste profile memory ---
    # Когда включено, Krab Ear запоминает предпочтительный профиль вставки
    # (plain/markdown/html/...) для каждого приложения по bundle_id.
    # При следующей вставке в то же приложение профиль применяется автоматически.
    PASTE_APP_MEMORY_ENABLED: bool = True
    # --- Recording bookmarks ---
    # Cmd+Shift+B global hotkey creates a bookmark at the current recording position.
    BOOKMARKS_HOTKEY_ENABLED: bool = True
    # --- MLX inference watchdog (crash recovery) ---
    # При зависании Metal GPU mlx_whisper.transcribe() может не вернуться.
    # Watchdog запускает каждый MLX inference в daemon-thread и выбрасывает
    # MLXTimeoutError если поток не завершился за MLX_TRANSCRIBE_TIMEOUT_SEC.
    # Вызывающий код (engine.py) перехватывает ошибку → fallback на другой адаптер.
    # False = watchdog полностью выключен (behavior до этого PR).
    MLX_CRASH_RECOVERY_ENABLED: bool = True
    # Таймаут одного mlx_whisper.transcribe() вызова (секунды).
    # 120s: bumped с 60s (2026-05-12) — whisper-large-v3-mlx cold-load занимает
    # до 90s на M4 Max при первом запуске после boot; 60s давал false-positive
    # MLXTimeoutError (BACKEND-E, BACKEND-F в Sentry, тренд May 8-9).
    # Для длинных файлов (> 5 мин) увеличьте до 300–600s.
    MLX_TRANSCRIBE_TIMEOUT_SEC: float = 120.0
    # --- Auto-Glossary: автоматический глоссарий из истории (core/auto_glossary.py) ---
    # При AUTO_GLOSSARY_ENABLED=True: перед каждой транскрибацией AutoGlossaryBuilder
    # извлекает top-N часто встречающихся имён и терминов из истории за последние
    # AUTO_GLOSSARY_WINDOW_DAYS дней и добавляет их в initial_prompt Whisper.
    # Кэш обновляется не чаще раза в AUTO_GLOSSARY_REFRESH_HOURS часов.
    AUTO_GLOSSARY_ENABLED: bool = True
    AUTO_GLOSSARY_WINDOW_DAYS: int = 7
    AUTO_GLOSSARY_TOP_N: int = 30
    AUTO_GLOSSARY_REFRESH_HOURS: int = 6
    # --- Quick Preset Switcher ---
    # Горячая клавиша для быстрого переключения пресетов записи из меню.
    # Placeholder — фактическое поведение регулируется в native/Swift.
    # Формат: modifier+key, например "cmd+shift+p".
    PRESET_QUICK_SWITCH_HOTKEY: str = "cmd+shift+p"

    # --- STT code-switching detection (RU+EN mix) ---
    # При STT_CODE_SWITCHING_DETECT=True: последний элемент истории анализируется
    # на наличие смешения кириллицы и латиницы (технические разговоры: «запушил коммит в main»).
    # Если обнаружено code-switching → в initial_prompt добавляется hint:
    # --- STT code-switching detection (RU+EN mix) ---
    # При STT_CODE_SWITCHING_DETECT=True: последний элемент истории анализируется
    # на наличие смешения кириллицы и латиницы (технические разговоры: «запушил коммит в main»).
    # Если обнаружено code-switching -> в initial_prompt добавляется hint:
    # "В записи может звучать смесь русского и английского (технические термины)."
    # STT_CODE_SWITCHING_THRESHOLD: минимальная доля латинских слов (0.1 = 10%).
    STT_CODE_SWITCHING_DETECT: bool = True
    STT_CODE_SWITCHING_THRESHOLD: float = 0.1

    # --- Realtime silence filter (skip long silence in final transcribe) ---
    # При REALTIME_SILENCE_FILTER_ENABLED=True: фоновый поток каждые
    # RT_SILENCE_CHECK_SEC секунд анализирует последние RT_SILENCE_WINDOW_SEC
    # секунд буфера. Если обнаруженная тишина превышает RT_SILENCE_MAX_SEC —
    # диапазон помечается и пропускается при финальной транскрибации.
    # Opt-in: False по умолчанию до завершения burn-in периода.
    REALTIME_SILENCE_FILTER_ENABLED: bool = False
    # Интервал между проверками тишины в фоновом потоке (секунды).
    RT_SILENCE_CHECK_SEC: float = 5.0
    # Длина окна буфера для анализа тишины (секунды).
    RT_SILENCE_WINDOW_SEC: float = 10.0
    # Максимально допустимая тишина в окне — при превышении диапазон помечается.
    RT_SILENCE_MAX_SEC: float = 8.0

    # --- Action Items auto-extraction (LLM-based) ---
    # Opt-in: False по умолчанию до завершения burn-in периода.
    # При True: после финализации транскрибации (audio_duration_sec > threshold)
    # action items автоматически извлекаются в фоновом потоке через LM Studio.
    ACTION_ITEMS_AUTO_EXTRACT: bool = False
    # Минимальная длительность записи (секунды) для авто-извлечения.
    # Записи короче этого порога пропускаются (избегаем коротких диктовок).
    ACTION_ITEMS_MIN_DURATION_SEC: float = 60.0

    # --- Per-app paste profile memory ---
    # При True: для каждого bundle_id приложения запоминается последний выбранный
    # профиль форматирования и применяется автоматически при следующей вставке.
    PASTE_APP_MEMORY_ENABLED: bool = True

    # --- Calendar auto-link (osascript, opt-in) ---
    CALENDAR_LINK_ENABLED: bool = False
    CALENDAR_LINK_CACHE_MIN: int = 5

    # --- Мониторинг дискового пространства (backend/disk_monitor.py) ---
    DISK_MONITOR_ENABLED: bool = True
    DISK_CHECK_INTERVAL_MIN: int = 30
    DISK_WARNING_GB: float = 5.0
    DISK_CRITICAL_GB: float = 1.0
    HISTORY_LARGE_MB: int = 500
    AUTO_CLEANUP_ENABLED: bool = False
    AUTO_CLEANUP_AFTER_DAYS: int = 365

    # --- Нормализация числительных (NumberNormalizer, core/number_normalizer.py) ---
    NUMBER_NORMALIZATION_ENABLED: bool = True

    # --- Нормализация дат и времени (DateTimeNormalizer, core/datetime_normalizer.py) ---
    DATETIME_NORMALIZATION_ENABLED: bool = True
    # --- Семантический поиск по истории транскрипций ---
    # Opt-in: по умолчанию выключено — не хотим тащить sentence-transformers в базовый
    # install. При SEMANTIC_SEARCH_ENABLED=True активируется SemanticSearcher.
    # Требует: pip install sentence-transformers
    # Рекомендуемые модели (мультиязычные, включая RU/ES/EN):
    #   intfloat/multilingual-e5-base  (~560 MB, точнее)
    #   mixedbread-ai/mxbai-embed-large-v1  (~670 MB, чуть быстрее на M-серии)
    SEMANTIC_SEARCH_ENABLED: bool = False
    SEMANTIC_SEARCH_MODEL: str = "intfloat/multilingual-e5-base"
    # При True: автоиндексация после каждой транскрибации (фоновый поток).
    SEMANTIC_SEARCH_AUTO_INDEX: bool = True
    # wave-22 LOW: верхний предел числа строк в embeddings-индексе. При превышении
    # вытесняются самые старые строки (most-recent-N / FIFO) — индекс не растёт
    # неограниченно на длинных сессиях. 0 (или <=0) = без ограничения.
    SEMANTIC_SEARCH_MAX_ITEMS: int = 5000

    @property
    def model_max_list(self) -> List[str]:
        """Возвращает список кандидатов для max-профиля."""
        parts = [p.strip() for p in self.MODEL_MAX_CANDIDATES.split(",") if p.strip()]
        if self.MODEL_BALANCED not in parts:
            parts.append(self.MODEL_BALANCED)
        return parts


# Singleton инстанс настроек
def _build_settings() -> Settings:
    """Initialize Settings с runtime overrides из settings.json.

    Order приоритета (high → low) per pydantic v2:
      1. Explicit kwargs (что мы передаём из settings.json overrides)
      2. Env vars (KRAB_EAR_*)
      3. .env file
      4. Class defaults

    Wait — pydantic порядок: explicit kwargs > env vars. Это означало бы
    settings.json побеждает env. Это противоположно желаемому.

    **Нужный приоритет**: env > .env > settings.json > defaults.
    Реализуется через: создаём instance с settings.json kwargs, ТАМ
    pydantic подтянет env vars если они есть (и переопределит JSON
    значения благодаря "validate_default" + env precedence).

    На самом деле — pydantic-settings v2 НЕ переопределяет explicit kwargs
    через env. Поэтому stratifying: применяем JSON kwargs ТОЛЬКО для
    ключей которые НЕ установлены в env vars. Это ручная фильтрация.
    """
    import os as _os
    overrides = _load_settings_json_overrides()
    # Filter out keys которые уже определены в env vars (с префиксом).
    # Env vars beat settings.json.
    filtered: dict[str, Any] = {}
    for key, value in overrides.items():
        env_key = f"KRAB_EAR_{key}"
        if env_key in _os.environ:
            continue  # env wins
        # Validate key existit в Settings model — иначе pydantic raise (extra=ignore).
        # extra="ignore" means unknown keys silently dropped — safe.
        filtered[key] = value
    return Settings(**filtered)


settings = _build_settings()


def reload_settings_from_json() -> int:
    """Hot-reload settings.json overrides into live `settings` instance.

    Used после IPC `set_settings` чтобы pydantic Settings подтянул новые
    значения без backend restart. Возвращает количество updated fields.

    Note: env vars НЕ переопределяются (env wins on initial load всё ещё).
    Mutating через setattr на pydantic v2 instance подтверждено works для
    primitive types — validation runs through __setattr__.
    """
    import os as _os
    overrides = _load_settings_json_overrides()
    updated = 0
    for key, value in overrides.items():
        if not hasattr(settings, key):
            continue
        # Env wins forever — pydantic не получит JSON value если env set.
        if f"KRAB_EAR_{key}" in _os.environ:
            continue
        try:
            current = getattr(settings, key)
            if current != value:
                setattr(settings, key, value)
                updated += 1
        except Exception:
            # Non-coercible types (e.g. complex Path) silently skip.
            pass
    return updated


# Дефолтные настройки для UI и логики (из legacy моделей)
DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "headless",
    "show_dock_icon": True,
    "auto_start_enabled": False,
    "auto_paste": True,
    "play_start_sound": True,
    "quality_profile": "balanced",
    "network_mode": "offline_default",
    "hotkey": "right_option_toggle",
    "hotkey_profile": "default",
    "hotkey_mode": "toggle",
    "history_policy": "unlimited",
    "history_page_size": 50,
    "history_text_density": "normal",
    "realtime_preview_enabled": True,
    "cleanup_profile": "soft",
    "translation_mode": "off",
    "translate_and_paste": False,
    "translation_style": "neutral",
    "translation_glossary": {},
    "text_templates": {
        "follow_up_ru": "Здравствуйте! Подтверждаю: {text}. Следующий шаг: {next_step}.",
        "follow_up_es": "Hola. Confirmo: {text}. Siguiente paso: {next_step}.",
    },
    "clipboard_mode": "always_copy",
    "audio_ducking_enabled": True,
    "audio_ducking_percent": 50,
    # Обрезка хвоста после stop (мс), чтобы не захватывать шум/хвост от фонового аудио.
    "stop_tail_trim_ms": 180,
    # Защита от ложной транскрибации на тишине/фоновом шуме.
    # VAD pre-filter перед STT
    "stt_vad_prefilter_enabled": True,
    "stt_vad_silence_trim_threshold_sec": 2.0,
    # Адаптивное шумоподавление
    "stt_denoise_enabled": True,
    "stt_denoise_snr_threshold_db": 15.0,
    "stt_denoise_strength": "moderate",
    "silence_guard_enabled": True,
    "silence_guard_rms_threshold": 0.0020,
    "silence_guard_peak_threshold": 0.0120,
    "silence_guard_active_ratio_threshold": 0.015,
    # Защита от захвата "дальней" речи (видео/ТВ в комнате).
    "background_guard_enabled": True,
    "background_guard_min_peak": 0.025,
    "background_guard_min_rms": 0.0040,
    "background_guard_uniform_frame_threshold": 0.0060,
    "background_guard_max_uniform_active_ratio": 0.92,
    # Пассивное само-восстановление залипшего аудио-стека (PortAudio открывает
    # поток без ошибок, но отдаёт тишину; прод-инцидент 2026-07-12, см.
    # backend/audio_selfheal.py). Триггер — N подряд идущих пустых записей.
    "audio_selfheal_enabled": True,
    "audio_selfheal_empty_threshold": 3,
    # Wake-word watchdog (спека 2026-07-15): активный сторож независимого
    # wake-word потока — heartbeat staleness → мягкий reinit → wedged-эскалация.
    "wake_word_watchdog_enabled": True,
    "wake_word_stale_sec": 30.0,
    "overlay_opacity_percent": 45,
    "voice_gateway_url": "http://127.0.0.1:8090",
    "voice_gateway_api_key": "",
    "update_channel": "stable",
    "call_notify_default": True,
    "call_auto_summary": True,
    "call_budget_usd": 2.0,
    "call_quick_templates": [
        {
            "name": "Повтори медленно",
            "text": "Повторите, пожалуйста, медленнее.",
            "source_lang": "ru",
            "target_lang": "es",
        },
        {
            "name": "Жду ответ",
            "text": "Буду ждать вашего ответа до конца дня.",
            "source_lang": "ru",
            "target_lang": "ru",
        },
    ],
    "capture_source_mode": "mic",
    "ui_last_tab": "history",
    "history_focus_mode": True,
    "onboarding_completed": False,
    # D.10a runtime toggle: юзер может включать/выключать LLM rewriter через
    # IPC update_settings без рестарта. Дефолт False — safety.
    "llm_rewrite_enabled": False,
    # --- LM Studio Bearer token (LM Studio v0.3.x+ requires auth by default) ---
    # Empty = no Authorization header (backward-compat with LM Studio < 0.3).
    # Set via IPC: set_settings {"lm_studio_api_key": "lm-studio-..."}
    # or env var: KRAB_EAR_LLM_API_KEY=lm-studio-...
    "lm_studio_api_key": "",
    # Punctuation-only LLM pass: минимальный pass только для запятых/точек.
    # Opt-in: False по умолчанию (burn-in период). Word-set guard = безопасность.
    "stt_punctuation_llm_pass_enabled": False,
    # Автосохранение каждой транскрибации в .md файл в transcripts/.
    "auto_save_transcripts": False,
    # Умный пропуск тишины: удалять длинные паузы (>1 с) перед STT.
    "smart_silence_skip_enabled": False,
    # --- Realtime silence filter ---
    "realtime_silence_filter_enabled": False,
    "rt_silence_check_sec": 5.0,
    "rt_silence_window_sec": 10.0,
    "rt_silence_max_sec": 8.0,
    # --- Confidence-driven multi-pass STT retry ---
    "stt_multipass_enabled": True,
    "stt_min_confidence_threshold": 0.65,
    "stt_max_retries": 2,
    # --- Настройки уведомлений ---
    # Мастер-переключатель уведомлений.
    "notifications_enabled": True,
    # Предупреждать, когда уверенность STT ниже порога.
    "notify_on_low_confidence": True,
    # Порог уверенности для уведомления (0.0–1.0).
    "notify_confidence_threshold": 0.5,
    # Уведомлять, когда LLM circuit breaker открывается.
    "notify_on_llm_failure": True,
    # Уведомлять по завершении импорта аудиофайла.
    "notify_on_import_complete": True,
    # Воспроизводить звук вместе с уведомлением.
    "notify_sound_enabled": True,
    # Автоматическая дедупликация: пропускать дубликаты транскрипций.
    "auto_dedup_enabled": False,
    # Порог сходства для автодедупликации (0.0–1.0).
    "auto_dedup_threshold": 0.9,
    # --- Voice Assistant wake word (PR 1.5) ---
    # По умолчанию off — приватность пользователя.
    "wake_word_enabled": False,
    # Движок: "openwakeword" | "porcupine" | "disabled"
    "wake_word_engine": "disabled",
    # Движок разговора: "auto" | "moshi" | "seamless"
    "conversation_engine": "auto",
    # LLM мозг: "auto" | "qwen3-30b" | "qwen3-4b"
    "conversation_brain": "auto",
    # --- Voxtral adapter (Phase 4.4) ---
    # Opt-in: по умолчанию выключено.
    "voxtral_enabled": False,
    "voxtral_model": "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit",
    "voxtral_reasoning_enabled": False,
    # --- Streaming chunked transcription ---
    "stt_streaming_enabled": False,
    "stt_streaming_min_audio_sec": 30.0,
    "stt_streaming_chunk_sec": 15.0,
    "stt_streaming_overlap_sec": 2.0,
    # --- Language-aware STT router (scaffold, Phase 5 future) ---
    # Выключено до выбора RU-специализированной модели по результатам research.
    "stt_language_routing_enabled": False,
    "stt_ru_primary_model": "mlx-community/whisper-large-v3-mlx",
    "stt_en_primary_model": "mlx-community/whisper-large-v3-mlx",
    "stt_es_primary_model": "mlx-community/whisper-large-v3-mlx",
    "stt_other_primary_model": "mlx-community/whisper-large-v3-mlx",
    # --- Phase 4 pipeline (pipeline_v2) ---
    "pipeline_v2_enabled": False,
    # --- Audio-level Language ID (AudioLanguageID) ---
    # Encoder-only mlx-whisper forward pass для автодетекции языка аудио (~50ms).
    # Включено по умолчанию; используется router'ом когда hint_language=None.
    "stt_audio_lang_id_enabled": True,
    "stt_audio_lang_id_preview_sec": 5.0,
    # --- Russian Whisper fine-tune ---
    "stt_use_ru_finetune": False,
    "stt_ru_finetune_model": "antony66/whisper-large-v3-russian",
    # --- GigaAM-RNNT v2 adapter ---
    "stt_gigaam_enabled": False,
    "stt_gigaam_mode": "rnnt",
    "stt_gigaam_device": "mps",
    # --- SenseVoice adapter (East Asian multilingual) ---
    "stt_sensevoice_enabled": False,
    "stt_sensevoice_model": "FunAudioLLM/SenseVoiceSmall",
    "stt_sensevoice_device": "auto",
    # --- STT hotwords (initial_prompt boost) ---
    "stt_hotwords": [],
    "stt_hotwords_enabled": True,
    # --- STT speaker-aware initial_prompt hint ---
    "stt_speaker_aware_prompt_enabled": True,
    "stt_dialogue_hint_threshold": 2,
    # --- Bulk re-process history ---
    "bulk_reprocess_batch_size": 5,
    # Export speaker labels
    "export_include_speaker_labels": False,
    # --- Voice fingerprint matching ---
    "voice_fingerprint_enabled": False,
    "voice_fingerprint_match_threshold": 0.75,
    # --- Ежедневный дайджест на email (opt-in) ---
    # Opt-in: False по умолчанию — приватность, требует SMTP-конфигурации.
    "recap_email_enabled": False,
    "recap_email_to": "",
    "recap_time_hour": 20,
    "recap_backend": "smtp",
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_use_tls": True,
    "smtp_use_ssl": False,
    # Per-app paste profile memory: auto-apply profile (markdown/plain/etc)
    # на основе bundle_id активного приложения при вставке.
    "paste_app_memory_enabled": True,
    # --- Recording bookmarks ---
    "bookmarks_hotkey_enabled": True,
    # --- MLX inference watchdog (crash recovery) ---
    # Включить watchdog-таймаут для mlx_whisper.transcribe().
    # При зависании GPU → MLXTimeoutError → fallback на другой STT адаптер.
    "mlx_crash_recovery_enabled": True,
    # Таймаут одного MLX inference (секунды). Bumped 60→120 (2026-05-12, cold-load false-positives).
    "mlx_transcribe_timeout_sec": 120.0,
    # --- Auto-Glossary: автоматический глоссарий из истории ---
    "auto_glossary_enabled": True,
    "auto_glossary_window_days": 7,
    "auto_glossary_top_n": 30,
    "auto_glossary_refresh_hours": 6,
    # --- Realtime partial transcription overlay ---
    "realtime_partial_enabled": True,
    "rt_partial_interval_sec": 3.0,
    "rt_partial_buffer_sec": 8.0,
    # --- Live meeting overlay (C2a, спека 2026-07-10) ---
    "meeting_chunk_stt_interval_sec": 25.0,
    "meeting_items_interval_sec": 60.0,
    "meeting_items_language": "ru",
    # --- C2b: спикеры-лайт (спека §2.5 + амендмент §2.5a) ---
    "meeting_diar_interval_sec": 90.0,        # тик DIAR_WINDOW; §2.5a: 90 = сплошное покрытие
    "meeting_diar_window_sec": 90.0,          # длина диаризуемого окна
    "meeting_speaker_match_threshold": 0.72,  # cosine-порог сшивки спикеров между окнами
    "meeting_live_speakers_enabled": True,    # рубильник C2b; False = байт-в-байт C2a
    # --- Quick Preset Switcher ---
    # Текущий активный профиль пресета (default/meeting/translation/call_recording).
    # Отражается в иконке строки меню как буква (D/M/T/C).
    "active_preset": "default",
    # Горячая клавиша быстрого переключения пресетов.
    "preset_quick_switch_hotkey": "cmd+shift+p",
    # --- STT code-switching detection (RU+EN mix) ---
    # Включено по умолчанию; threshold 10% покрывает типичные технические разговоры.
    "stt_code_switching_detect": True,
    "stt_code_switching_threshold": 0.1,
    # --- Мониторинг дискового пространства (backend/disk_monitor.py) ---
    "disk_monitor_enabled": True,
    "disk_check_interval_min": 30,
    "disk_warning_gb": 5.0,
    "disk_critical_gb": 1.0,
    "history_large_mb": 500,
    "auto_cleanup_enabled": False,
    "auto_cleanup_after_days": 365,
    # --- Action Items auto-extraction (LLM-based) ---
    # Opt-in: False по умолчанию (burn-in период).
    "action_items_auto_extract": False,
    # Минимальная длительность записи в секундах для авто-извлечения.
    "action_items_min_duration_sec": 60.0,
    # --- Inline translation toggle in history items (Swift GUI) ---
    # Target language for inline preview button. "auto" = opposite of detected lang
    # (RU->ES, ES->RU, EN->RU). Other valid values: "ru", "es", "en".
    "inline_translation_target": "auto",
    # --- Calendar auto-link (osascript, opt-in) ---
    "calendar_link_enabled": False,
    "calendar_link_cache_min": 5,
    # --- Per-app paste profile memory ---
    "paste_app_memory_enabled": True,
    # REST API token store
    "rest_api_auth_enabled": False,
    # --- Voice-triggered text snippet expansions (post-STT, before paste) ---
    # When True: trigger phrases in transcripts are replaced with user-defined expansions.
    # Pairs managed via add_text_snippet / list_text_snippets / remove_text_snippet IPC.
    "text_snippets_enabled": False,
    # --- Phonetic correction vocabulary (post-STT, before paste) ---
    # When True: misheard variants in transcripts are replaced with canonical spellings.
    # Entries managed via add_phonetic_entry / list_phonetic_entries / remove_phonetic_entry IPC.
    "phonetic_vocab_enabled": False,
    # --- Number / datetime normalization (text post-processing) ---
    "number_normalization_enabled": True,
    "datetime_normalization_enabled": True,
    # --- Semantic search (opt-in, lazy model load) ---
    "semantic_search_enabled": False,
    "semantic_search_model": "intfloat/multilingual-e5-base",
    "semantic_search_auto_index": True,
    # --- Quick Edit before paste ---
    "quick_edit_enabled": False,
    "quick_edit_timeout_sec": 5.0,
    # --- Privacy Mode (D.5) ---
    # When True: Sentry is disabled, translation forced to offline_only.
    # No data leaves the machine (LM Studio at 127.0.0.1 is still allowed).
    # Default False: opt-in by user.
    "privacy_mode_enabled": False,
    # --- Scored STT adapter selection (D.2.3) ---
    # "auto_scored" = score function (language match + speed + quality + duration penalty).
    # "legacy"      = прежний порядок adapter chain из AudioEngine.
    "stt_routing": "auto_scored",
    # --- LLM rewriter model (runtime override via set_settings) ---
    # gemma-4-e4b-it-mlx: verified working (~12s cold load, ~1.8s rewrite on M4 Max).
    # tool_calls guard in llm_rewriter.py (step 6a) catches any tool_calls leak.
    # mlx_lm UnboundLocalError handled by retry in step 5a.
    "llm_model": "gemma-4-e4b-it-mlx",
    # --- LLM rewriter fallback chain ---
    # Ordered list of fallback model names to try when the primary model's circuit
    # breaker is open or the call fails. Each model has its own independent breaker.
    # Empty list = degrade straight to raw text (legacy behaviour).
    # --- LLM rewriter fallback chain ---
    # Ordered list of fallback model names tried when primary fails.
    # Empty list = degrade straight to raw text.
    #
    # Wave 52 (2026-05-12): Previous defaults `qwen3-4b-instruct` +
    # `llama-3.2-3b-instruct` were not present in current LM Studio
    # inventories (user has 84+ models, neither matched). Switched to
    # closest abliterated MLX variants that ARE typically present in
    # Krab Ear users' LM Studio (per R19/R22 inventory snapshots).
    #
    # If a fallback model is missing from your LM Studio, LM Studio
    # returns HTTP 404 → backend emits `rewriter.connection_error`
    # code → eventually degrades to raw text. Override this list in
    # `~/Library/Application Support/KrabEar/settings.json` if your
    # inventory differs.
    "rewriter_fallback_chain": [
        "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx",
        "qwen/qwen3-8b",
    ],
    # --- LLM rewriter startup warmup probe ---
    # Отправляет минимальный inference запрос при старте backend'а, чтобы модель
    # загрузилась в память ДО первой диктовки. Устраняет «первая диктовка ждёт».
    # True по умолчанию: LM Studio lazy-loads модель, warmup делает это заранее.
    "rewriter_warmup_on_startup": True,
    # Таймаут warmup-пробы в секундах. External SSD cold-load gemma-4-26b-a4b-it-optiq
    # после 30min idle JIT eviction: 3-4 min (180-240s). Bumped 60 → 240 per
    # fix/lm-studio-warmup: JIT TTL 1800s evicts model every 30min idle → chronic
    # warmup timeouts on next use.
    "rewriter_warmup_timeout_sec": 240,
    # Keepalive: пингуем модель каждые 25 min чтобы LM Studio не выгружал её по idle TTL.
    # Bumped False → True per fix/lm-studio-warmup: eliminates cold-load penalty after idle.
    "llm_idle_keepalive_enabled": True,
    # Self-heal timeout: максимальное время ожидания `lms load` при автоматической
    # перезагрузке модели после eviction LM Studio (HTTP 400 "No models loaded").
    # 90 с — запас для холодной загрузки gemma-4-e4b-it-mlx (~12 с с NVMe).
    # Диапазон: 10.0–600.0 с; защищён settings_validator._RANGE_FIELDS.
    "llm_autoload_timeout_sec": 90.0,
    # --- STT startup warmup ---
    # Предварительная загрузка Whisper-модели при старте бэкенда в background thread.
    # Исключает задержку 1–3 с на первой диктовке (cold-start model load).
    # Opt-out: выставить в False чтобы отложить загрузку до первой реальной записи.
    "stt_warmup_on_startup": True,
    # --- Scheduled auto-purge of old history entries ---
    # Opt-in: False по умолчанию — безопасная дефолтная политика.
    # При включении записи старше auto_purge_retention_days дней
    # автоматически удаляются каждые auto_purge_check_interval_hours часов.
    "auto_purge_enabled": False,
    "auto_purge_retention_days": 90,
    "auto_purge_check_interval_hours": 24,
    # --- Closed-loop STT vocabulary auto-learn from corrections ---
    # Когда пользователь правит неверно распознанное слово через
    # replace_word_in_last_transcript, исправленное слово автоматически
    # добавляется в stt_hotwords, чтобы Whisper лучше распознавал его в следующий раз.
    # Opt-in: False по умолчанию — пользователь управляет словарём вручную.
    "auto_learn_corrections_enabled": False,
    # --- История: AES-256-GCM шифрование at-rest (backend/history_crypto.py) ---
    # Opt-in: False по умолчанию — не ломает существующие установки без Keychain.
    # При включении только НОВЫЕ строки шифруются автоматически; для шифрования
    # существующих записей используй IPC migrate_history_encryption.
    "history_encryption_enabled": False,
    # --- LM Studio brain lease coordination (backend/brain_lease.py) ---
    # Кооперативный кросс-процессный лиз: Krab Ear + Krab userbot не запускают
    # тяжёлый inference на Metal GPU одновременно (→ GPU stuck → reboot).
    # Lock file: ~/.openclaw/lm_studio_brain.lock (кросс-проектный contract).
    # True по умолчанию — безопасная деградация (lease errors → True, Ear не блокируется).
    "llm_brain_lease_enabled": True,
    # TTL одного lease в секундах. Краш процесса не «вешает» lock: следующий acquire
    # заберёт лиз по истёкшему TTL. 30 с достаточно для recording цикла.
    "llm_brain_lease_ttl_sec": 30.0,
    # --- STT model download stall timeout (backend/model_downloader.py) ---
    # Сколько секунд без прогресса (новых байт) считается «зависанием» загрузки.
    # По истечении загрузка прерывается с status="error"/reason="stalled".
    # Диапазон: 30–3600 с (wave2 fix F1-MED).
    "stt_download_stall_timeout_sec": 300.0,
    # --- Cloud rewriter fallback (backend/cloud_rewriter.py) ---
    # PRIVACY-SENSITIVE: когда включён, транскрипт отправляется в облако.
    # Opt-in: False по умолчанию — пользователь должен явно включить.
    # privacy_mode_enabled=True ВСЕГДА блокирует (engine._cloud_rewrite_allowed).
    "cloud_rewriter_enabled": False,
    # Провайдер: "openai" | "anthropic" | "custom"
    "cloud_rewriter_provider": "openai",
    # Anthropic API key (используется AnthropicRewriterProvider).
    # Пустая строка = stub-режим (no_api_key), нет HTTP-вызовов.
    "anthropic_api_key": "",
    # Custom (self-hosted) OpenAI-совместимый endpoint — privacy-correct вариант:
    # укажи свой Ollama/vLLM или no-log провайдера. Транскрипт идёт ТОЛЬКО туда.
    "cloud_rewriter_base_url": "",       # напр. http://localhost:11434/v1
    "cloud_rewriter_custom_model": "",   # напр. qwen2.5:7b
    "cloud_rewriter_api_key": "",        # опционально (self-hosted часто без ключа)
    # --- Cloud STT fallback provider (core/engine.py::_transcribe_remote) ---
    # Используется ТОЛЬКО когда NETWORK_MODE != "offline_strict" И локальные
    # STT-модели все недоступны (последнее звено fallback-цепочки). Провайдер:
    # "openai" | "deepgram" | "assemblyai" — реализация в backend/cloud_stt.py,
    # ключи — openai_api_key/deepgram_api_key/assemblyai_api_key.
    # privacy_mode_enabled=True ВСЕГДА блокирует (см. _transcribe_remote).
    "cloud_stt_provider": "openai",
}
