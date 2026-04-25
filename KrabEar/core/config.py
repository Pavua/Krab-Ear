"""Централизованная конфигурация Krab Ear на базе Pydantic-Settings.

Все параметры могут быть переопределены через переменные окружения (.env
или ~/Library/Application Support/KrabEar/.secrets).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import List, Any

# Абсолютный путь к .secrets — backend загружает его на старте через
# pydantic-settings env_file tuple. Порядок загрузки в env_file:
# сначала repo-local .env, затем .secrets — в pydantic-settings v2
# последний файл в tuple побеждает при конфликте ключей. Env vars из
# launchd plist всё равно имеют более высокий приоритет (env > env_file).
_SECRETS_FILE = Path.home() / "Library" / "Application Support" / "KrabEar" / ".secrets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KRAB_EAR_",
        env_file=(".env", str(_SECRETS_FILE)),
        extra="ignore",
    )

    # Директории
    DATA_DIR: Path = Path.home() / ".krab_ear_data"

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
    # "offline_default" / "online_preferred" — разрешают fallback на Voice Gateway STT.
    # Дефолт strict: Voice Gateway STT endpoint пока не реализован, fallback давал 404.
    NETWORK_MODE: str = "offline_strict"
    GATEWAY_URL: str = "http://127.0.0.1:18789/v1/chat/completions"
    STT_GATEWAY_URL: str = "http://127.0.0.1:18789/v1/audio/transcriptions"
    AI_MODEL: str = "google/gemini-2.0-flash"
    STT_MODEL: str = "whisper-1"

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
    LLM_MODEL: str = "huihui-qwen3-4b-instruct-2507-abliterated-hi-mlx"
    LLM_TIMEOUT_SEC: float = 5.0
    LLM_CIRCUIT_FAIL_THRESHOLD: int = 3
    LLM_CIRCUIT_INITIAL_RESET_SEC: int = 60
    LLM_CIRCUIT_MAX_RESET_SEC: int = 600

    # Авто-резервное копирование
    AUTO_BACKUP_ENABLED: bool = True

    # Авто-экспорт истории по расписанию
    AUTO_EXPORT_ENABLED: bool = False

    # Формат логов: "text" (стандартный) или "json" (структурированный JSON)
    LOG_FORMAT: str = "text"

    # REST API: опциональный ключ аутентификации для защищённых эндпоинтов.
    # Пустая строка = аутентификация отключена (обратная совместимость).
    # Если задан, защищённые эндпоинты требуют заголовок: Authorization: Bearer <key>
    REST_API_KEY: str = ""

    # Rate limiting для REST API (flask-limiter).
    # False = rate limiting полностью отключён (удобно для тестов и локальной разработки).
    RATE_LIMIT_ENABLED: bool = True

    # CORS: список разрешённых Origins через запятую. "*" — разрешить всё (локальная разработка).
    # Пример: "http://localhost:3000,https://app.example.com"
    CORS_ORIGINS: str = "*"

    # Умный пропуск тишины: удалять длинные паузы (>1 с) перед STT.
    SMART_SILENCE_SKIP_ENABLED: bool = False

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

    @property
    def model_max_list(self) -> List[str]:
        """Возвращает список кандидатов для max-профиля."""
        parts = [p.strip() for p in self.MODEL_MAX_CANDIDATES.split(",") if p.strip()]
        if self.MODEL_BALANCED not in parts:
            parts.append(self.MODEL_BALANCED)
        return parts


# Singleton инстанс настроек
settings = Settings()

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
    # Автосохранение каждой транскрибации в .md файл в transcripts/.
    "auto_save_transcripts": False,
    # Умный пропуск тишины: удалять длинные паузы (>1 с) перед STT.
    "smart_silence_skip_enabled": False,
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
}
