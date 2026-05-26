"""SettingsService — управление настройками Krab Ear.

Выделен из backend/service.py. Отвечает за:
- get_settings / set_settings (IPC-методы)
- apply_profile_preset / list_profile_presets
- TTL-кэш настроек (5 сек)
- Вспомогательные coerce-хелперы
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.models import DEFAULT_SETTINGS
from backend.observability import add_breadcrumb
from backend.settings_backup import SettingsBackup
from backend.settings_validator import SettingsValidator

_log = logging.getLogger(__name__)


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

    def __init__(self, store: Any, backup: SettingsBackup | None = None) -> None:
        self.store = store
        self._cache: dict[str, Any] | None = None
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 5.0
        self._validator = SettingsValidator()
        self._backup = backup if backup is not None else SettingsBackup()
        # Hooks called with (old_settings, new_settings) after a successful save.
        # BackendService registers a hook to propagate hot-reloaded values to
        # live collaborators (e.g. LLMRewriter.set_api_key).
        self._after_save_hooks: list[Any] = []

    def register_after_save_hook(self, hook: Any) -> None:
        """Register a callable(old_settings, new_settings) fired after each set_settings save."""
        self._after_save_hooks.append(hook)

    # ------------------------------------------------------------------
    # Кэш
    # ------------------------------------------------------------------

    def cached_settings(self) -> dict[str, Any]:
        """Возвращает копию настроек с TTL-кэшем (5 сек). Избегает повторного чтения файла."""
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return dict(self._cache)
        raw = self.store.load_settings()
        # Validate and auto-fix on load — warnings only, no hard errors
        result_v = self._validator.validate(raw)
        if result_v.warnings:
            for w in result_v.warnings:
                _log.debug("settings load: %s", w)
        self._cache = result_v.fixed
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
        _t0 = time.monotonic()
        old_settings = self.cached_settings()
        try:
            self._backup.create_backup(old_settings, reason="before_set")
        except Exception as exc:  # noqa: BLE001
            _log.warning("handle_set_settings: auto-backup failed: %s", exc)

        settings = dict(old_settings)
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

        # Нормализация STT hotwords: убираем пустые строки и дублирование.
        raw_hotwords = settings.get("stt_hotwords", [])
        if not isinstance(raw_hotwords, list):
            raw_hotwords = []
        settings["stt_hotwords"] = list(dict.fromkeys(
            w.strip() for w in raw_hotwords if str(w).strip()
        ))
        settings["stt_hotwords_enabled"] = bool(settings.get("stt_hotwords_enabled", True))

        # Final validation pass before persisting — raises on hard errors
        try:
            vr = self._validator.validate(settings)
            if not vr.valid:
                raise ValueError(f"Настройки содержат ошибки: {'; '.join(vr.errors)}")
        except Exception as _exc:
            add_breadcrumb(
                category="settings",
                message="set_settings",
                level="error",
                data={
                    "keys": sorted(params.keys()),
                    "key_count": len(params),
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                    "ok": False,
                    "error_type": type(_exc).__name__,
                },
            )
            raise
        if vr.warnings:
            for w in vr.warnings:
                _log.warning("settings save: %s", w)
        settings = vr.fixed

        result = self.store.save_settings(settings)
        self.invalidate_cache()
        add_breadcrumb(
            category="settings",
            message="set_settings",
            level="info",
            data={
                "keys": sorted(params.keys()),
                "key_count": len(params),
                "duration_ms": round((time.monotonic() - _t0) * 1000),
                "ok": True,
            },
        )
        # Hot-reload pydantic Settings из обновлённого settings.json — без
        # restart engine.py видит новые feature flags (STT_GIGAAM_ENABLED,
        # STT_LANGUAGE_ROUTING_ENABLED, etc).
        try:
            from core.config import reload_settings_from_json
            updated = reload_settings_from_json()
            if updated:
                _log.info("set_settings: hot-reloaded %d pydantic fields", updated)
        except Exception as exc:  # noqa: BLE001
            _log.warning("set_settings: hot-reload failed: %s", exc)
        # Notify registered hooks (e.g. propagate api_key to live LLMRewriter).
        for hook in self._after_save_hooks:
            try:
                hook(old_settings, settings)
            except Exception as exc:  # noqa: BLE001
                _log.warning("set_settings: after_save_hook failed: %s", exc)
        return result

    def handle_apply_profile_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применяет пресет настроек профиля, сохраняет и сбрасывает кэш.

        После успешного применения эмитирует preset.changed через EventBus.
        """
        _t0 = time.monotonic()
        profile = str(params.get("profile", "")).strip()
        preset = self._PROFILE_PRESETS.get(profile)
        if preset is None:
            available = ", ".join(self._PROFILE_PRESETS.keys())
            raise ValueError(f"Неизвестный пресет профиля: '{profile}'. Доступные: {available}")

        settings = self.cached_settings()
        settings.update(preset)
        settings["active_preset"] = profile
        result = self.store.save_settings(settings)
        self.invalidate_cache()
        add_breadcrumb(
            category="settings",
            message="apply_profile_preset",
            level="info",
            data={
                "profile": profile,
                "keys_changed": sorted(preset.keys()),
                "duration_ms": round((time.monotonic() - _t0) * 1000),
                "ok": True,
            },
        )
        try:
            import backend.event_bus as _ebus  # noqa: PLC0415
            _ebus.bus.emit("preset.changed", {
                "profile": profile,
                "description": self._PROFILE_PRESET_DESCRIPTIONS.get(profile, ""),
            })
        except Exception as exc:  # noqa: BLE001
            _log.warning("handle_apply_profile_preset: emit preset.changed failed: %s", exc)
        return result

    def handle_get_notification_preferences(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущие настройки уведомлений из хранилища настроек."""
        settings = self.cached_settings()
        return {
            "notifications_enabled": bool(settings.get("notifications_enabled", True)),
            "notify_on_low_confidence": bool(settings.get("notify_on_low_confidence", True)),
            "notify_confidence_threshold": float(settings.get("notify_confidence_threshold", 0.5)),
            "notify_on_llm_failure": bool(settings.get("notify_on_llm_failure", True)),
            "notify_on_import_complete": bool(settings.get("notify_on_import_complete", True)),
            "notify_sound_enabled": bool(settings.get("notify_sound_enabled", True)),
        }

    def handle_set_notification_preferences(self, params: dict[str, Any]) -> dict[str, Any]:
        """Обновляет настройки уведомлений. Принимает любое подмножество полей."""
        settings = self.cached_settings()

        _BOOL_FIELDS = (
            "notifications_enabled",
            "notify_on_low_confidence",
            "notify_on_llm_failure",
            "notify_on_import_complete",
            "notify_sound_enabled",
        )
        for field in _BOOL_FIELDS:
            if field in params:
                settings[field] = self._coerce_bool(params[field], default=bool(settings.get(field, True)))

        if "notify_confidence_threshold" in params:
            settings["notify_confidence_threshold"] = self._coerce_bounded(
                value=params["notify_confidence_threshold"],
                default=0.5,
                min_value=0.0,
                max_value=1.0,
            )

        result = self.store.save_settings(settings)
        self.invalidate_cache()
        return result

    # Sensitive fields — никогда не экспортируются
    _SENSITIVE_FIELDS: frozenset[str] = frozenset({
        "voice_gateway_api_key",
        "hf_token",
        "rest_api_key",
        "lm_studio_api_key",
    })

    def handle_export_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует текущие настройки в JSON-файл, исключая чувствительные поля.

        Params:
            file (str, optional): путь к файлу. По умолчанию ~/krabear_settings_<ts>.json.

        Returns:
            {"file": path, "settings_count": N}
        """
        settings = self.cached_settings()
        safe = {k: v for k, v in settings.items() if k not in self._SENSITIVE_FIELDS}

        if params.get("file"):
            out_path = Path(str(params["file"])).expanduser().resolve()
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_path = Path.home() / f"krabear_settings_{ts}.json"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            json.dump(safe, fh, ensure_ascii=False, indent=2)

        _log.info("export_settings: %d settings → %s", len(safe), out_path)
        return {"file": str(out_path), "settings_count": len(safe)}

    def handle_import_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует настройки из JSON-файла.

        Params:
            file (str): путь к JSON-файлу с настройками.

        Validates each key via SettingsValidator (run against merged dict).
        Never overwrites sensitive fields — they are silently skipped.
        Returns {"imported": N, "skipped": N, "errors": [...]}
        """
        file_path = params.get("file")
        if not file_path:
            raise ValueError("Параметр 'file' обязателен для import_settings")

        src = Path(str(file_path)).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Файл настроек не найден: {src}")

        try:
            with src.open("r", encoding="utf-8") as fh:
                incoming: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON в файле настроек: {exc}") from exc

        if not isinstance(incoming, dict):
            raise ValueError("Файл настроек должен содержать JSON-объект")

        errors: list[str] = []
        skipped = 0
        merged = self.cached_settings()

        for key, value in incoming.items():
            if key in self._SENSITIVE_FIELDS:
                skipped += 1
                _log.debug("import_settings: пропуск чувствительного поля '%s'", key)
                continue
            merged[key] = value

        # Validate the merged result
        vr = self._validator.validate(merged)
        if not vr.valid:
            errors.extend(vr.errors)
        if vr.warnings:
            for w in vr.warnings:
                _log.warning("import_settings: %s", w)
            errors.extend(vr.warnings)
        merged = vr.fixed

        imported = len(incoming) - skipped
        self.store.save_settings(merged)
        self.invalidate_cache()
        add_breadcrumb(
            category="settings",
            message="import_settings",
            level="info" if not errors else "warning",
            data={
                "imported": imported,
                "skipped": skipped,
                "error_count": len(errors),
            },
        )

        _log.info("import_settings: imported=%d skipped=%d errors=%d from %s",
                  imported, skipped, len(errors), src)
        return {"imported": imported, "skipped": skipped, "errors": errors}

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
    # Backup IPC handlers
    # ------------------------------------------------------------------

    def handle_list_settings_backups(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список бэкапов настроек, от новых к старым.

        Params:
            limit (int, optional): максимальное количество записей (default=10, max=50).

        Returns:
            {"backups": [{backup_id, ts, reason, file_size, settings_count_keys}, ...]}
        """
        try:
            limit = int(params.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 50))

        backups = self._backup.list_backups(limit=limit)
        return {"backups": backups}

    def handle_restore_settings_backup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Восстанавливает настройки из указанного бэкапа и сохраняет их.

        Params:
            backup_id (str): идентификатор бэкапа.

        Returns:
            {"restored_settings": {...}, "backup_id": str}
        """
        _t0 = time.monotonic()
        backup_id = str(params.get("backup_id", "")).strip()
        if not backup_id:
            raise ValueError("Параметр 'backup_id' обязателен для restore_settings_backup")

        restored = self._backup.restore_backup(backup_id)
        self.store.save_settings(restored)
        self.invalidate_cache()

        add_breadcrumb(
            category="settings",
            message="restore_settings_backup",
            level="info",
            data={
                "duration_ms": round((time.monotonic() - _t0) * 1000),
                "ok": True,
            },
        )
        _log.info("handle_restore_settings_backup: restored from %s", backup_id)
        return {"restored_settings": restored, "backup_id": backup_id}

    def handle_create_manual_settings_backup(self, params: dict[str, Any]) -> dict[str, Any]:
        """Создаёт ручной бэкап текущих настроек с произвольной причиной.

        Params:
            reason (str, optional): метка причины (default="manual").

        Returns:
            {"backup_id": str, "settings_count_keys": int}
        """
        reason = str(params.get("reason", "manual")).strip() or "manual"
        current = self.cached_settings()
        backup_id = self._backup.create_backup(current, reason=reason)

        # Count non-sensitive keys
        safe_count = sum(
            1 for k in current
            if k not in SettingsService._SENSITIVE_FIELDS
        )
        _log.info(
            "handle_create_manual_settings_backup: %s (%d keys)",
            backup_id,
            safe_count,
        )
        return {"backup_id": backup_id, "settings_count_keys": safe_count}

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
