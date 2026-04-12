"""SettingsService — управление настройками Krab Ear.

Выделен из backend/service.py. Отвечает за:
- get_settings / set_settings (IPC-методы)
- apply_profile_preset / list_profile_presets
- TTL-кэш настроек (5 сек)
- Вспомогательные coerce-хелперы
"""

from __future__ import annotations

import time
from typing import Any

from backend.models import DEFAULT_SETTINGS


class SettingsService:
    """Управляет чтением, записью и кэшированием пользовательских настроек."""

    _PROFILE_PRESETS: dict[str, dict[str, Any]] = {
        "default": {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": "off",
            "realtime_preview_enabled": True,
            "auto_paste": True,
        },
        "meeting": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "translation_mode": "off",
            "realtime_preview_enabled": True,
            "auto_paste": False,
        },
        "translation": {
            "quality_profile": "balanced",
            "cleanup_profile": "soft",
            "translation_mode": "auto",
            "translate_and_paste": True,
            "realtime_preview_enabled": True,
            "auto_paste": True,
        },
        "call_recording": {
            "quality_profile": "max",
            "cleanup_profile": "strict",
            "translation_mode": "off",
            "realtime_preview_enabled": False,
            "auto_paste": False,
        },
    }

    _PROFILE_PRESET_DESCRIPTIONS: dict[str, str] = {
        "default": "Стандартный режим: сбалансированное качество, мягкая очистка, автовставка включена",
        "meeting": "Режим митинга: максимальное качество, строгая очистка, автовставка отключена",
        "translation": "Режим перевода: авто-перевод с автовставкой результата",
        "call_recording": "Режим записи звонка: максимальное качество, без превью и автовставки",
    }

    def __init__(self, store: Any) -> None:
        self.store = store
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0

    # ------------------------------------------------------------------
    # Кэш
    # ------------------------------------------------------------------

    def cached_settings(self) -> dict[str, Any]:
        """Возвращает копию настроек с TTL-кэшем (5 сек). Избегает повторного чтения файла."""
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return dict(self._cache)
        self._cache = self.store.load_settings()
        self._cache_ts = now
        return dict(self._cache)

    def invalidate_cache(self) -> None:
        """Сбрасывает кэш настроек (вызывать после save_settings)."""
        self._cache = None
        self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_get_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.cached_settings()

    def handle_set_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self.cached_settings()
        settings.update(params)

        # Нормализуем критичные поля, чтобы UI и агент не расходились по форматам.
        if settings.get("mode") not in {"headless", "menubar"}:
            settings["mode"] = "headless"

        if settings.get("quality_profile") not in {"balanced", "max"}:
            settings["quality_profile"] = "balanced"
        if settings.get("cleanup_profile") not in {"soft", "strict"}:
            settings["cleanup_profile"] = "soft"
        if settings.get("translation_mode") not in {
            "off",
            "ru_to_es",
            "es_to_ru",
            "en_to_ru",
            "auto",
            "auto_to_ru",
            "bilingual_ru_es",
        }:
            settings["translation_mode"] = "off"
        if settings.get("translation_style") not in {"neutral", "chat", "formal"}:
            settings["translation_style"] = "neutral"
        if settings.get("clipboard_mode") not in {"always_copy", "copy_on_fail", "never_copy"}:
            settings["clipboard_mode"] = "always_copy"
        if settings.get("update_channel") not in {"stable", "beta"}:
            settings["update_channel"] = "stable"
        if not isinstance(settings.get("translation_glossary"), dict):
            settings["translation_glossary"] = {}
        if not isinstance(settings.get("text_templates"), dict):
            settings["text_templates"] = dict(DEFAULT_SETTINGS.get("text_templates", {}))
        else:
            normalized_templates: dict[str, str] = {}
            for key, value in settings.get("text_templates", {}).items():
                clean_key = str(key).strip()
                clean_value = str(value).strip()
                if clean_key and clean_value:
                    normalized_templates[clean_key] = clean_value
            settings["text_templates"] = (
                normalized_templates or dict(DEFAULT_SETTINGS.get("text_templates", {}))
            )

        if settings.get("network_mode") not in {"offline_default", "offline_strict", "online_opt_in"}:
            settings["network_mode"] = "offline_default"
        if settings.get("hotkey_profile") not in {"default", "meeting", "translation"}:
            settings["hotkey_profile"] = "default"

        if settings.get("history_policy") not in {"unlimited"}:
            settings["history_policy"] = "unlimited"
        if settings.get("history_text_density") not in {"normal", "compact"}:
            settings["history_text_density"] = "normal"
        if settings.get("capture_source_mode") not in {"mic", "system_audio", "mic_plus_system"}:
            settings["capture_source_mode"] = "mic"
        if settings.get("ui_last_tab") not in {"dictation", "live_translation", "history"}:
            settings["ui_last_tab"] = "history"

        settings["auto_start_enabled"] = bool(settings.get("auto_start_enabled", False))
        settings["show_dock_icon"] = bool(settings.get("show_dock_icon", True))
        settings["auto_paste"] = bool(settings.get("auto_paste", True))
        settings["play_start_sound"] = bool(settings.get("play_start_sound", True))
        settings["realtime_preview_enabled"] = bool(settings.get("realtime_preview_enabled", True))
        settings["translate_and_paste"] = bool(settings.get("translate_and_paste", False))
        settings["onboarding_completed"] = bool(settings.get("onboarding_completed", False))
        settings["audio_ducking_enabled"] = bool(settings.get("audio_ducking_enabled", True))
        settings["silence_guard_enabled"] = self._coerce_bool(settings.get("silence_guard_enabled", True), default=True)
        settings["background_guard_enabled"] = self._coerce_bool(settings.get("background_guard_enabled", True), default=True)
        settings["call_notify_default"] = self._coerce_bool(settings.get("call_notify_default", True), default=True)
        settings["call_auto_summary"] = self._coerce_bool(settings.get("call_auto_summary", True), default=True)
        settings["history_focus_mode"] = self._coerce_bool(settings.get("history_focus_mode", True), default=True)
        _gw_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        if not (_gw_url.startswith("http://localhost") or _gw_url.startswith("http://127.0.0.1") or _gw_url.startswith("https://")):
            raise ValueError(f"Voice Gateway URL must be localhost or HTTPS: {_gw_url}")
        settings["voice_gateway_url"] = _gw_url
        settings["voice_gateway_api_key"] = str(settings.get("voice_gateway_api_key", "")).strip()

        try:
            page_size = int(settings.get("history_page_size", 50))
        except (TypeError, ValueError):
            page_size = 50
        settings["history_page_size"] = max(10, min(page_size, 500))

        try:
            duck_percent = int(settings.get("audio_ducking_percent", 50))
        except (TypeError, ValueError):
            duck_percent = 50
        settings["audio_ducking_percent"] = max(0, min(duck_percent, 100))

        settings["stop_tail_trim_ms"] = self._coerce_bounded(
            value=settings.get("stop_tail_trim_ms", 180),
            default=180,
            min_value=0,
            max_value=1200,
        )
        settings["silence_guard_rms_threshold"] = self._coerce_bounded(
            value=settings.get("silence_guard_rms_threshold", 0.0020),
            default=0.0020,
            min_value=0.0003,
            max_value=0.05,
        )
        settings["silence_guard_peak_threshold"] = self._coerce_bounded(
            value=settings.get("silence_guard_peak_threshold", 0.0120),
            default=0.0120,
            min_value=0.001,
            max_value=0.2,
        )
        settings["silence_guard_active_ratio_threshold"] = self._coerce_bounded(
            value=settings.get("silence_guard_active_ratio_threshold", 0.015),
            default=0.015,
            min_value=0.001,
            max_value=0.30,
        )
        settings["background_guard_min_peak"] = self._coerce_bounded(
            value=settings.get("background_guard_min_peak", 0.025),
            default=0.025,
            min_value=0.003,
            max_value=0.25,
        )
        settings["background_guard_min_rms"] = self._coerce_bounded(
            value=settings.get("background_guard_min_rms", 0.0040),
            default=0.0040,
            min_value=0.0008,
            max_value=0.08,
        )
        settings["background_guard_uniform_frame_threshold"] = self._coerce_bounded(
            value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
            default=0.0060,
            min_value=0.001,
            max_value=0.20,
        )
        settings["background_guard_max_uniform_active_ratio"] = self._coerce_bounded(
            value=settings.get("background_guard_max_uniform_active_ratio", 0.92),
            default=0.92,
            min_value=0.40,
            max_value=0.99,
        )

        try:
            overlay_percent = int(settings.get("overlay_opacity_percent", 45))
        except (TypeError, ValueError):
            overlay_percent = 45
        settings["overlay_opacity_percent"] = max(15, min(overlay_percent, 90))

        result = self.store.save_settings(settings)
        self.invalidate_cache()
        return result

    def handle_apply_profile_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применяет пресет настроек профиля, сохраняет и сбрасывает кэш."""
        profile = str(params.get("profile", "")).strip()
        preset = self._PROFILE_PRESETS.get(profile)
        if preset is None:
            available = ", ".join(self._PROFILE_PRESETS.keys())
            raise ValueError(f"Неизвестный пресет профиля: '{profile}'. Доступные: {available}")

        settings = self.cached_settings()
        settings.update(preset)
        result = self.store.save_settings(settings)
        self.invalidate_cache()
        return result

    def handle_list_profile_presets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных пресетов профилей с описаниями и значениями."""
        presets = []
        for name, values in self._PROFILE_PRESETS.items():
            presets.append({
                "name": name,
                "description": self._PROFILE_PRESET_DESCRIPTIONS.get(name, ""),
                "settings": dict(values),
            })
        return {"presets": presets}

    # ------------------------------------------------------------------
    # Coerce-хелперы
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        """Нормализует bool-поля из UI/JSON с поддержкой строковых значений."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False
        return default

    @staticmethod
    def _coerce_bounded(
        value: Any,
        default: int | float,
        min_value: int | float,
        max_value: int | float,
    ) -> int | float:
        """Нормализует числовое значение в допустимый диапазон. Тип определяется default."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))
