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
    NETWORK_MODE: str = "offline_default"
    GATEWAY_URL: str = "http://127.0.0.1:18789/v1/chat/completions"
    STT_GATEWAY_URL: str = "http://127.0.0.1:18789/v1/audio/transcriptions"
    AI_MODEL: str = "google/gemini-2.0-flash"
    STT_MODEL: str = "whisper-1"
    
    # Лимиты
    MAX_AUDIO_MB: int = 50
    MAX_DURATION_SEC: int = 300
    TRANSCRIBE_TIMEOUT_SEC: int = 300
    
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

    # Pipeline v2 feature flag.
    # True = BackendService использует transcribe_v2() (pipeline-based path).
    # False = legacy path через AudioEngine.transcribe() напрямую (по умолчанию).
    PIPELINE_V2: bool = False

    # IPC throttle: защита от злоупотребления тяжёлыми IPC-методами.
    # False = throttling полностью отключён (удобно для тестов и локальной разработки).
    IPC_THROTTLE_ENABLED: bool = True

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
}
