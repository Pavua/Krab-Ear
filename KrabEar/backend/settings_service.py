"""SettingsService — управление настройками Krab Ear.

Выделен из backend/service.py. Отвечает за:
- get_settings / set_settings (IPC-методы)
- apply_profile_preset / list_profile_presets
- TTL-кэш настроек (5 сек)
- Вспомогательные coerce-хелперы
"""

from __future__ import annotations

import ipaddress as _ipaddress
import json
import logging
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from backend.models import DEFAULT_SETTINGS
from backend.observability import add_breadcrumb
from backend.settings_backup import SENSITIVE_FIELDS as _SENSITIVE_FIELDS_BACKUP, SettingsBackup
from backend.settings_validator import CURRENT_SCHEMA_VERSION, SettingsValidator
from backend.state_store import StateStoreLockTimeout

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path-traversal allowlist for settings export / import (W1736)
# ---------------------------------------------------------------------------
# Settings files may only be read from / written to these root directories.
# The list mirrors history_service._EXPORT_ALLOWED_ROOTS but is intentionally
# kept local so changes here don't silently widen the history allowlist.
_SETTINGS_IO_ALLOWED_ROOTS: tuple[str, ...] = (
    "~/Library/Application Support/KrabEar",
    "~/.krab_ear_data",
    "~/Documents",
    "~/Desktop",
    "~/Downloads",
    "/tmp",
    "/private/tmp",  # macOS: /tmp symlinks to /private/tmp
    tempfile.gettempdir(),  # macOS $TMPDIR = /private/var/folders/.../T/ (used by pytest tmp_path)
)


def _validate_settings_path(p: Path, *, operation: str) -> None:
    """Raise RuntimeError if *p* is outside the settings I/O allowlist.

    Args:
        p:         Already-resolved (expanduser + resolve) Path to validate.
        operation: Human-readable label used in the error message ("export" / "import").
    """
    for root_str in _SETTINGS_IO_ALLOWED_ROOTS:
        allowed = Path(root_str).expanduser().resolve()
        try:
            p.relative_to(allowed)
            return  # inside this root → allowed
        except ValueError:
            continue
    raise RuntimeError(
        f"settings {operation}: путь {p!s} находится за пределами разрешённых директорий. "
        f"Разрешённые корни: {list(_SETTINGS_IO_ALLOWED_ROOTS)}"
    )


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
        # W1437: RLock serialises all 5 save paths to prevent concurrent race conditions.
        self._save_lock = threading.RLock()
        # Hooks called with (old_settings, new_settings) after a successful save.
        # BackendService registers a hook to propagate hot-reloaded values to
        # live collaborators (e.g. LLMRewriter.set_api_key).
        self._after_save_hooks: list[Any] = []
        # Спека 2026-08-12 settings-read-nonblocking: гасит лог-шторм при
        # затяжной контенции — WARNING пишется один раз на эпизод (пока не
        # придёт УСПЕШНОЕ чтение), а не на каждый промах TTL-кэша.
        self._read_lock_timeout_warned: bool = False

    def register_after_save_hook(self, hook: Any) -> None:
        """Register a callable(old_settings, new_settings) fired after each set_settings save."""
        self._after_save_hooks.append(hook)

    # ------------------------------------------------------------------
    # Кэш
    # ------------------------------------------------------------------

    def cached_settings(self) -> dict[str, Any]:
        """Возвращает копию настроек с TTL-кэшем (5 сек). Избегает повторного чтения файла.

        На промахе TTL раньше уходил в ``StateStore.load_settings()`` без
        ограничения ожидания — тот берёт эксклюзивный flock, общий со всей
        историей (спека 2026-08-12 settings-read-nonblocking). Долгая
        операция с историей в другом потоке подвешивала privacy-гейт КАЖДОГО
        IPC-хендлера на десятки секунд. Теперь чтение ограничено коротким
        read-path бюджетом (``settings_read_lock_timeout_sec``); не уложились
        — ``_on_read_lock_timeout()`` отдаёт fail-closed фоллбэк вместо
        блокировки.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_ts) < self._cache_ttl:
            return dict(self._cache)

        read_timeout = self._read_lock_timeout_budget()
        try:
            if read_timeout > 0:
                raw = self.store.load_settings(lock_timeout_sec=read_timeout)
            else:
                # 0 = прежнее поведение (без read-path override — просто
                # обычный call site, ждёт сколько нужно инстансу StateStore).
                raw = self.store.load_settings()
        except StateStoreLockTimeout:
            return self._on_read_lock_timeout(read_timeout)

        # Успешное чтение закрывает эпизод контенции (если он был) — следующий
        # промах TTL снова получит ровно одну WARNING, а не тишину навсегда.
        self._read_lock_timeout_warned = False
        # Validate and auto-fix on load — warnings only, no hard errors
        result_v = self._validator.validate(raw)
        if result_v.warnings:
            for w in result_v.warnings:
                _log.debug("settings load: %s", w)
        self._cache = result_v.fixed
        self._cache_ts = now
        return dict(self._cache)

    def _read_lock_timeout_budget(self) -> float:
        """Бюджет ожидания flock для текущего чтения (settings_read_lock_timeout_sec).

        Источник: последнее известное (пусть протухшее по TTL) значение
        кэша, если оно уже было хоть раз успешно прочитано; иначе — дефолт
        модуля ``DEFAULT_SETTINGS`` (холодный старт). Само значение уже
        приходит клампленным диапазоном ``_RANGE_FIELDS`` из предыдущего
        ``validate()`` — здесь только защита от испорченного/нечислового
        значения, которое могло попасть сюда до первой валидации.
        """
        source = self._cache if self._cache is not None else DEFAULT_SETTINGS
        try:
            budget = float(source.get("settings_read_lock_timeout_sec", 0.5))
        except (TypeError, ValueError):
            budget = 0.5
        if not math.isfinite(budget) or budget < 0:
            budget = 0.5
        return budget

    def _on_read_lock_timeout(self, read_timeout: float) -> dict[str, Any]:
        """Fail-closed фоллбэк на StateStoreLockTimeout из read-path бюджета.

        Направление отказа — ВСЕГДА в сторону приватности (спека §"Направление
        отказа — fail-closed по приватности"): неизвестность настроек под
        контенцией трактуется как «приватность включена», никогда как
        privacy_mode_enabled=False по умолчанию.

        1. Есть последнее известное значение кэша (даже протухшее по TTL) —
           отдаём его как есть: оно уже прошло валидацию и несёт РЕАЛЬНОЕ
           значение privacy_mode_enabled, которое владелец выставил в прошлый
           успешный раз.
        2. Известного значения нет вовсе (холодный старт backend-а + контенция
           settings.json сразу же) — отдаём дефолты, но с принудительным
           privacy_mode_enabled=True: неизвестность ВСЕГДА трактуется как
           «приватность включена», иначе транскрипты потекут в
           аналитику/экспорт/Sentry, пока не разрешится контенция.
        """
        if not self._read_lock_timeout_warned:
            # read_timeout<=0 значит read-path бюджет выключен (0 = прежнее
            # поведение) — реально сработал ОБЩИЙ инстанс-таймаут StateStore,
            # а не этот бюджет, поэтому не печатаем вводящие в заблуждение
            # "0.00с" — это отдельная формулировка.
            budget_desc = f"{read_timeout:.2f}с" if read_timeout > 0 else "выключенным read-path бюджетом"
            _log.warning(
                "SettingsService.cached_settings(): не удалось прочитать настройки за "
                "%s (StateStore._lock() занят другим держателем) — отдаём %s вместо "
                "блокировки хендлера",
                budget_desc,
                "последнее известное значение" if self._cache is not None else
                "fail-closed дефолты (privacy_mode_enabled=True)",
                extra={"read_timeout_sec": read_timeout, "has_cache": self._cache is not None},
            )
            self._read_lock_timeout_warned = True
        if self._cache is not None:
            return dict(self._cache)
        fallback = dict(DEFAULT_SETTINGS)
        fallback["privacy_mode_enabled"] = True
        return fallback

    def invalidate_cache(self) -> None:
        """Сбрасывает кэш настроек (вызывать после save_settings)."""
        self._cache = None
        self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # W1308: after-save hooks helper
    # ------------------------------------------------------------------

    def _fire_after_save_hooks(self, old_settings: dict[str, Any], new_settings: dict[str, Any]) -> None:
        """Call all registered after-save hooks with (old, new). Exceptions are swallowed."""
        for hook in self._after_save_hooks:
            try:
                hook(old_settings, new_settings)
            except Exception as exc:  # noqa: BLE001
                _log.warning("after_save_hook failed: %s", exc)

    def _reload_and_fire_hooks(self, old_settings: dict[str, Any], new_settings: dict[str, Any]) -> None:
        """W1341/W1436: Hot-reload pydantic settings, then fire hooks.

        Single point of truth called on ALL 5 save paths so pydantic Settings
        never stays stale and hooks always fire after reload.
        """
        try:
            from core.config import reload_settings_from_json  # noqa: PLC0415
            updated = reload_settings_from_json()
            if updated:
                _log.info("settings: hot-reloaded %d pydantic fields", updated)
        except Exception as exc:  # noqa: BLE001
            _log.warning("settings: hot-reload failed: %s", exc)
        self._fire_after_save_hooks(old_settings, new_settings)

    def _maybe_disable_sentry_for_privacy(
        self, old_settings: dict[str, Any], new_settings: dict[str, Any]
    ) -> None:
        """W1763 MED 2: централизованный kill-switch Sentry при включении privacy-режима.

        Вызывается из ВСЕХ путей изменения настроек, которые могут изменить
        privacy_mode_enabled (set_settings, import_settings, restore_settings_backup).
        Если privacy_mode_enabled переключается True→True (уже было True — ничего не делаем).
        Если False→True — сбрасываем Sentry SDK, чтобы телеметрия замолчала немедленно.

        Метод идемпотентен: повторный вызов с теми же значениями — no-op.
        """
        old_privacy = bool(old_settings.get("privacy_mode_enabled", False))
        new_privacy = bool(new_settings.get("privacy_mode_enabled", False))
        if new_privacy and not old_privacy:
            # Privacy mode только что включился — отключаем Sentry.
            try:
                import backend.observability as _obs  # noqa: PLC0415
                if _obs._sentry_initialized:
                    try:
                        import sentry_sdk as _sdk  # noqa: PLC0415
                        _sdk.flush(timeout=2)
                        _sdk.init(dsn=None)  # type: ignore[call-overload]
                    except Exception:  # noqa: BLE001
                        pass
                    _obs._sentry_initialized = False
                    _log.info(
                        "_maybe_disable_sentry_for_privacy: Sentry отключён — privacy_mode_enabled=True",
                        extra={"trigger": "privacy_mode_flip"},
                    )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "_maybe_disable_sentry_for_privacy: ошибка отключения Sentry: %s", exc
                )

    # ------------------------------------------------------------------
    # W1457: migration helper
    # ------------------------------------------------------------------

    def _maybe_migrate(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Migrate settings to current schema version if needed.

        W1457: After write-back to store, invalidates cache so subsequent
        cached_settings() calls always return the latest schema version.

        This method does NOT call store.save_settings itself — callers are
        responsible for the final save after validation. The write-back (and
        invalidate_cache) only happens when settings were loaded from store
        (i.e. when this is called from the regular load path, not restore).
        For restore path: migrate only, caller validates+saves.
        """
        schema_ver = settings.get("schema_version", "1.0")
        if schema_ver == CURRENT_SCHEMA_VERSION:
            return settings
        try:
            migrated = self._validator.migrate(settings, from_version=schema_ver, to_version=CURRENT_SCHEMA_VERSION)
            _log.info("_maybe_migrate: migrated settings %s→%s", schema_ver, CURRENT_SCHEMA_VERSION)
            return migrated
        except Exception as exc:  # noqa: BLE001
            _log.warning("_maybe_migrate: migration %s→%s failed: %s", schema_ver, CURRENT_SCHEMA_VERSION, exc)
            return settings

    def _maybe_migrate_and_save(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Migrate settings to current schema and write-back to store if needed.

        W1457: Calls store.save_settings(migrated) then invalidate_cache() after
        schema write-back so cached_settings() always returns the migrated version.
        Used when loading live settings from store (not from backup restore path).
        """
        schema_ver = settings.get("schema_version", "1.0")
        if schema_ver == CURRENT_SCHEMA_VERSION:
            return settings
        migrated = self._maybe_migrate(settings)
        if migrated is not settings:
            try:
                self.store.save_settings(migrated)
                self.invalidate_cache()  # W1457: invalidate after schema write-back
            except Exception as exc:  # noqa: BLE001
                _log.warning("_maybe_migrate_and_save: write-back failed: %s", exc)
        return migrated

    # ------------------------------------------------------------------
    # IPC handlers
    # ------------------------------------------------------------------

    def handle_get_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        # wave-35 CRIT: redact secrets before sending over unauthenticated IPC socket.
        # Non-empty credential fields are replaced with 'REDACTED'; empty/absent fields
        # are left as-is so the UI can distinguish "not configured" from "set".
        settings = self.cached_settings()
        for k in self._SENSITIVE_FIELDS:
            if settings.get(k):
                settings[k] = "REDACTED"
        return settings

    def handle_get_voice_gateway_credential(self, params: dict[str, Any]) -> dict[str, Any]:
        """W1892: узкоскоуповый НЕредактированный источник VG-креденшела.

        Swift открывает WS к Voice Gateway напрямую (минуя Python-бэкенд), поэтому
        ему нужно реальное значение ``voice_gateway_api_key`` для заголовка
        ``Authorization: Bearer`` — общий ``get_settings`` его редактирует
        (wave-35 CRIT, верно для всех остальных клиентов/полей). Возвращает ТОЛЬКО
        эти два поля, ничего сверх — не расширяет отображение секрета ни в UI, ни в
        бэкапах (``settings_backup.py`` продолжает редактировать поле на диске).
        """
        settings = self.cached_settings()
        return {
            "ok": True,
            "voice_gateway_url": settings.get("voice_gateway_url", ""),
            "voice_gateway_api_key": settings.get("voice_gateway_api_key", ""),
        }

    def handle_set_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._save_lock:  # W1437
            return self._handle_set_settings_locked(params)

    # ---------------------------------------------------------------------------
    # ENV-PIN guard: these settings may be locked by the operator via env vars.
    # If the env var is set the IPC caller must NOT be allowed to overwrite it.
    # ---------------------------------------------------------------------------
    _ENV_PINNED_SETTINGS: dict[str, str] = {
        "ipc_signing_secret": "KRAB_EAR_IPC_SIGNING_SECRET",
        "ipc_signing_enabled": "KRAB_EAR_IPC_SIGNING_ENABLED",
    }

    def _check_env_pinned(self, params: dict[str, Any]) -> None:
        """Raise ValueError for any key in *params* that is pinned by an env var.

        The error message names the env var the operator must unset to allow
        the update, so callers get a clear action item.
        """
        for key, env_var in self._ENV_PINNED_SETTINGS.items():
            if key in params and os.environ.get(env_var) is not None:
                raise ValueError(
                    f"{key} is pinned by env; "
                    f"remove {env_var} to allow updates"
                )

    def _handle_set_settings_locked(self, params: dict[str, Any]) -> dict[str, Any]:
        old_settings = self.cached_settings()

        # A2: refuse to overwrite env-pinned security settings via IPC.
        self._check_env_pinned(params)

        # wave-36 HIGH: a client that previously received 'REDACTED' must not write
        # the string 'REDACTED' back to disk, silently overwriting the real credential.
        # Strip any sensitive-field entry whose value is exactly 'REDACTED' before merge.
        params = {
            k: v for k, v in params.items()
            if not (k in self._SENSITIVE_FIELDS and v == "REDACTED")
        }

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
        if settings.get("ui_last_tab") not in {"dictation", "live_translation", "history", "conversation", "call_automation", "diagnostics", "archive"}:
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
        # W1763 MED 1: точная проверка хоста через urlparse предотвращает sibling-prefix bypass.
        # Старый startswith("http://localhost") пропускал http://localhost.evil.com/ и
        # http://127.0.0.1.evil.com/ — они проходили проверку, но резолвились на атакующий сервер.
        # Теперь проверяем только схему и точный hostname после парсинга.
        _gw_parsed = urlparse(_gw_url)
        _gw_scheme = _gw_parsed.scheme
        _gw_hostname = (_gw_parsed.hostname or "").lower()
        _localhost_hostnames = {"localhost", "127.0.0.1", "::1"}
        if not (
            (_gw_scheme == "http" and _gw_hostname in _localhost_hostnames)
            or _gw_scheme == "https"
        ):
            raise ValueError(f"Voice Gateway URL must be localhost or HTTPS: {_gw_url}")
        settings["voice_gateway_url"] = _gw_url
        settings["voice_gateway_api_key"] = str(settings.get("voice_gateway_api_key", "")).strip()

        # wave-1770 HIGH: validate smtp_host to prevent SSRF to cloud metadata endpoints.
        # An attacker with IPC access can set smtp_host=169.254.169.254 to trigger TCP
        # connections to AWS/GCP instance-metadata services when recap email fires.
        # Allow: empty (SMTP disabled), hostnames, loopback/localhost (local relay),
        # RFC 1918 (internal corporate mail servers).
        # Block: link-local 169.254.0.0/16 (cloud metadata), multicast, ::.
        _smtp_host_raw = str(settings.get("smtp_host", "")).strip()
        if _smtp_host_raw:
            try:
                _smtp_addr = _ipaddress.ip_address(_smtp_host_raw)
                if _smtp_addr.is_link_local:
                    raise ValueError(
                        f"smtp_host {_smtp_host_raw!r} отклонён: link-local адрес "
                        "(169.254.0.0/16 / fe80::/10) — cloud metadata endpoint запрещён"
                    )
                if _smtp_addr.is_multicast:
                    raise ValueError(
                        f"smtp_host {_smtp_host_raw!r} отклонён: multicast адрес запрещён"
                    )
            except ValueError as _ve:
                if "отклонён" in str(_ve):
                    raise
                # Not an IP address (hostname) — allow
        settings["smtp_host"] = _smtp_host_raw

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
        vr = self._validator.validate(settings)
        if not vr.valid:
            raise ValueError(f"Настройки содержат ошибки: {'; '.join(vr.errors)}")
        if vr.warnings:
            for w in vr.warnings:
                _log.warning("settings save: %s", w)
        settings = vr.fixed

        result = self.store.save_settings(settings)
        self.invalidate_cache()
        add_breadcrumb(
            category="settings",
            message="set_settings",
            data={
                "keys": sorted(params.keys()),
                "key_count": len(params),
            },
        )
        # W1763 MED 2: централизованный kill-switch Sentry (вынесен в _maybe_disable_sentry_for_privacy).
        # Покрывает set_settings, import_settings, restore_settings_backup одним методом.
        self._maybe_disable_sentry_for_privacy(old_settings, settings)
        # W1341/W1436: hot-reload pydantic settings then fire hooks (single point of truth).
        self._reload_and_fire_hooks(old_settings, settings)
        # wave-36 HIGH: redact secrets before sending result over IPC socket.
        return self._redact_secrets(result)

    def handle_apply_profile_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применяет пресет настроек профиля, сохраняет и сбрасывает кэш.

        После успешного применения эмитирует preset.changed через EventBus.
        """
        profile = str(params.get("profile", "")).strip()
        preset = self._PROFILE_PRESETS.get(profile)
        if preset is None:
            available = ", ".join(self._PROFILE_PRESETS.keys())
            raise ValueError(f"Неизвестный пресет профиля: '{profile}'. Доступные: {available}")

        with self._save_lock:  # W1437
            old_settings = self.cached_settings()
            settings = dict(old_settings)
            settings.update(preset)
            settings["active_preset"] = profile
            result = self.store.save_settings(settings)
            self.invalidate_cache()
            add_breadcrumb(
                category="settings",
                message="apply_profile_preset",
                data={
                    "profile": profile,
                    "keys_changed": sorted(preset.keys()),
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
            # W1308/W1341/W1436: reload pydantic settings and fire hooks
            self._reload_and_fire_hooks(old_settings, settings)
            # wave-36 HIGH: redact secrets before sending result over IPC socket.
            return self._redact_secrets(result)

    # ------------------------------------------------------------------
    # A1 — Рекомендованная настройка в один тап
    # (spec docs/superpowers/specs/2026-07-07-recommended-setup-design.md)
    # ------------------------------------------------------------------

    # 10 безусловных («ДА» черновика §4) — включаются всегда, кроме privacy-скипа
    # для трёх transcript-читающих ключей из этого набора.
    _RECOMMENDED_UNCONDITIONAL: tuple[str, ...] = (
        "smart_silence_skip_enabled",
        "realtime_silence_filter_enabled",
        "auto_dedup_enabled",
        "auto_save_transcripts",
        "phonetic_vocab_enabled",
        "text_snippets_enabled",
        "auto_learn_corrections_enabled",
        "quick_edit_enabled",
        "paste_undo_enabled",
        "calendar_link_enabled",
    )

    # Транскрипт-читающие ключи из безусловного набора — skip при privacy_mode_enabled=True
    # (финальная спека §4; см. Задача №0 плана A1 для подтверждения гейтов в местах исполнения).
    _RECOMMENDED_PRIVACY_SENSITIVE: frozenset[str] = frozenset({
        "auto_dedup_enabled", "auto_learn_corrections_enabled",
    })

    # 3 условных («УСЛОВНО-ДА») — probe-гейт применяется в _apply_conditional_candidates.
    _RECOMMENDED_CONDITIONAL: tuple[str, ...] = (
        "llm_rewrite_enabled",
        "action_items_auto_extract",
        "stt_sensevoice_enabled",
    )

    # GigaAM-пара — решение 9.7: ВСЕГДА skipped, без probe-логики вообще.
    _RECOMMENDED_GIGAAM_PAIR: tuple[str, ...] = (
        "stt_gigaam_enabled",
        "stt_language_routing_enabled",
    )
    _RECOMMENDED_GIGAAM_SKIP_REASON: str = "настройте GigaAM вручную в Настройках"

    def handle_apply_recommended_setup(
        self,
        params: dict[str, Any],
        *,
        probe_llm_fn: Any,
        sensevoice_cached_fn: Any,
    ) -> dict[str, Any]:
        """Применяет (или показывает превью) рекомендованный безопасный набор настроек.

        Скелет идентичен handle_apply_profile_preset (см. выше в этом файле):
        old_settings = cached_settings() -> merge -> save_settings -> invalidate_cache
        -> EventBus emit -> _reload_and_fire_hooks.

        Args:
            params: {"dry_run": bool = True, "keys": list[str] | None}.
            probe_llm_fn: callable() -> {"reachable": bool, ...} — обычно
                HealthCheckService.handle_probe_llm_http, инжектируется вызывающей
                стороной (service.py) чтобы SettingsService не зависел напрямую от
                HealthCheckService (избегаем циклических конструкторских зависимостей).
            sensevoice_cached_fn: callable() -> bool — обычно
                ModelDownloader.get_status("FunAudioLLM/SenseVoiceSmall")["cached"].

        Returns:
            Контракт финальной спеки §2: {ok, dry_run, tier, applied, skipped,
            rationale, snapshot_id, restart_required}.
        """
        dry_run = bool(params.get("dry_run", True))
        requested_keys = params.get("keys")
        requested_keys_set = set(requested_keys) if requested_keys else None

        with self._save_lock:  # W1437 — тот же lock, что и все остальные save-пути
            old_settings = self.cached_settings()
            privacy_on = bool(old_settings.get("privacy_mode_enabled", False))

            applied: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []

            def _wants(key: str) -> bool:
                return requested_keys_set is None or key in requested_keys_set

            # 1) Безусловные «ДА»
            for key in self._RECOMMENDED_UNCONDITIONAL:
                if not _wants(key):
                    continue
                if key in self._RECOMMENDED_PRIVACY_SENSITIVE and privacy_on:
                    skipped.append({"key": key, "reason": "privacy_mode_enabled"})
                    continue
                old_value = old_settings.get(key, False)
                applied.append({
                    "key": key, "old_value": old_value, "new_value": True,
                    "restart_required": False,
                })

            # 2) Условные «УСЛОВНО-ДА» — probe-гейт
            self._apply_conditional_candidates(
                old_settings=old_settings, privacy_on=privacy_on, wants=_wants,
                probe_llm_fn=probe_llm_fn, sensevoice_cached_fn=sensevoice_cached_fn,
                applied=applied, skipped=skipped,
            )

            # 3) GigaAM-пара — решение 9.7, ВСЕГДА skipped, никакого probe
            for key in self._RECOMMENDED_GIGAAM_PAIR:
                if not _wants(key):
                    continue
                skipped.append({"key": key, "reason": self._RECOMMENDED_GIGAAM_SKIP_REASON})

            tier = self._detect_tier_for_recommended_setup()
            rationale = self._build_recommended_setup_rationale(tier, applied, skipped)
            restart_required = any(a["restart_required"] for a in applied)

            if dry_run:
                return {
                    "ok": True, "dry_run": True, "tier": tier,
                    "applied": applied, "skipped": skipped,
                    "rationale": rationale, "snapshot_id": None,
                    "restart_required": restart_required,
                }

            # dry_run=False — реально применяем
            snapshot_id = self._backup.create_backup(old_settings, reason="before_recommended_setup")
            merged = dict(old_settings)
            for item in applied:
                merged[item["key"]] = item["new_value"]
            self.store.save_settings(merged)
            self.invalidate_cache()
            try:
                import backend.event_bus as _ebus  # noqa: PLC0415
                _ebus.bus.emit("recommended_setup.applied", {
                    "tier": tier,
                    "applied_keys": sorted(a["key"] for a in applied),
                    "skipped_keys": sorted(s["key"] for s in skipped),
                })
            except Exception as exc:  # noqa: BLE001
                _log.warning("handle_apply_recommended_setup: emit failed: %s", exc)
            self._reload_and_fire_hooks(old_settings, merged)

            return {
                "ok": True, "dry_run": False, "tier": tier,
                "applied": applied, "skipped": skipped,
                "rationale": rationale, "snapshot_id": snapshot_id,
                "restart_required": restart_required,
            }

    def _apply_conditional_candidates(
        self, *, old_settings, privacy_on, wants, probe_llm_fn, sensevoice_cached_fn,
        applied, skipped,
    ) -> None:
        """Probe-гейт для 3 условных кандидатов."""
        if wants("llm_rewrite_enabled"):
            self._apply_llm_probe_gated_key(
                "llm_rewrite_enabled", old_settings, probe_llm_fn, applied, skipped,
            )
        if wants("action_items_auto_extract"):
            if privacy_on:
                skipped.append({"key": "action_items_auto_extract", "reason": "privacy_mode_enabled"})
            else:
                self._apply_llm_probe_gated_key(
                    "action_items_auto_extract", old_settings, probe_llm_fn, applied, skipped,
                )
        if wants("stt_sensevoice_enabled"):
            try:
                cached = bool(sensevoice_cached_fn())
            except Exception:  # noqa: BLE001
                cached = False
            if cached:
                applied.append({
                    "key": "stt_sensevoice_enabled",
                    "old_value": old_settings.get("stt_sensevoice_enabled", False),
                    "new_value": True, "restart_required": False,
                })
            else:
                skipped.append({
                    "key": "stt_sensevoice_enabled",
                    "reason": "модель SenseVoice не найдена в HF-кэше",
                })

    @staticmethod
    def _apply_llm_probe_gated_key(key, old_settings, probe_llm_fn, applied, skipped) -> None:
        try:
            probe = probe_llm_fn() or {}
        except Exception:  # noqa: BLE001
            probe = {}
        if probe.get("reachable"):
            applied.append({
                "key": key, "old_value": old_settings.get(key, False),
                "new_value": True, "restart_required": False,
            })
        else:
            skipped.append({"key": key, "reason": "требует LM Studio, probe_llm_http не ответил"})

    @staticmethod
    def _detect_tier_for_recommended_setup() -> str:
        from core.hardware_profile import detect_hardware_profile  # noqa: PLC0415
        return detect_hardware_profile().tier

    @staticmethod
    def _build_recommended_setup_rationale(tier: str, applied: list, skipped: list) -> str:
        return (
            f"Железо: {tier}-класс. Включено безопасных настроек: {len(applied)}, "
            f"пропущено: {len(skipped)}."
        )

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
        with self._save_lock:  # W1437
            old_settings = self.cached_settings()
            settings = dict(old_settings)

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
            # W1308/W1341/W1436: reload pydantic settings and fire hooks
            self._reload_and_fire_hooks(old_settings, settings)
            # wave-36 HIGH: redact secrets before sending result over IPC socket.
            return self._redact_secrets(result)

    # W929 F4: single source of truth — imported from settings_backup.
    # Covers all 9 secret fields; local 4-field set was a subset causing leaks.
    _SENSITIVE_FIELDS: frozenset[str] = _SENSITIVE_FIELDS_BACKUP

    def _redact_secrets(self, d: dict) -> dict:
        """Return a shallow copy of *d* with all non-empty sensitive fields replaced by 'REDACTED'.

        wave-36 HIGH: write-path handlers (set_settings, apply_profile_preset,
        set_notification_preferences) previously returned the raw save_settings() result
        which contained plaintext credentials.  This helper is the single redaction point
        for all three paths.

        Empty / absent values are left as-is so the UI can distinguish
        'not configured' (empty string) from 'configured but hidden' (REDACTED).
        """
        result = dict(d)
        for k in self._SENSITIVE_FIELDS:
            if result.get(k):
                result[k] = "REDACTED"
        return result

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
            # W1736: reject writes to paths outside the settings I/O allowlist.
            _validate_settings_path(out_path, operation="export")
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
        # W1736: reject reads from paths outside the settings I/O allowlist.
        _validate_settings_path(src, operation="import")
        if not src.exists():
            raise FileNotFoundError(f"Файл настроек не найден: {src}")

        try:
            with src.open("r", encoding="utf-8") as fh:
                incoming: dict[str, Any] = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Невалидный JSON в файле настроек: {exc}") from exc

        if not isinstance(incoming, dict):
            raise ValueError("Файл настроек должен содержать JSON-объект")

        with self._save_lock:  # W1437
            errors: list[str] = []
            skipped = 0
            old_settings = self.cached_settings()
            merged = dict(old_settings)

            for key, value in incoming.items():
                if key in self._SENSITIVE_FIELDS:
                    skipped += 1
                    _log.debug("import_settings: пропуск чувствительного поля '%s'", key)
                    continue
                merged[key] = value

            # Validate the merged result
            vr = self._validator.validate(merged)
            if not vr.valid:
                # W1434: raise ValueError on hard validation failure
                raise ValueError(
                    f"Настройки содержат ошибки: {'; '.join(vr.errors)}"
                )
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
            # W1763 MED 2: kill-switch Sentry если импорт включает privacy_mode_enabled=True.
            self._maybe_disable_sentry_for_privacy(old_settings, merged)
            # W1308/W1341/W1436: reload pydantic settings and fire hooks
            self._reload_and_fire_hooks(old_settings, merged)
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

        W1178: pre-restore backup + validate + ValueError on failure + rollback.
        W1337 F2: preserve credential fields missing from backup.
        W1435: migrate old-schema backups before validate.
        W1437: RLock around save path.

        Params:
            backup_id (str): идентификатор бэкапа.

        Returns:
            {"restored_settings": {...}, "backup_id": str}
        """
        backup_id = str(params.get("backup_id", "")).strip()
        if not backup_id:
            raise ValueError("Параметр 'backup_id' обязателен для restore_settings_backup")

        with self._save_lock:  # W1437
            old_settings = self.cached_settings()

            # W1178: take a pre-restore snapshot so restore can be undone on failure
            try:
                self._backup.create_backup(old_settings, reason="before_restore")
            except Exception as exc:  # noqa: BLE001
                _log.warning("handle_restore_settings_backup: pre-restore backup failed: %s", exc)

            restored = self._backup.restore_backup(backup_id)

            # W1337 F2: preserve credential fields missing from backup.
            current = self.cached_settings()
            dropped_fields = sorted(
                field for field in self._SENSITIVE_FIELDS
                if current.get(field) and not restored.get(field)
            )
            if dropped_fields:
                for field in dropped_fields:
                    restored[field] = current[field]
                _log.warning(
                    "handle_restore_settings_backup: backup '%s' missing credentials %s",
                    backup_id, dropped_fields,
                )

            # W1435: migrate old schema before validate
            restored = self._maybe_migrate(restored)

            # W1178/W1435: validate restored settings — rollback and raise on hard errors
            vr = self._validator.validate(restored)
            if not vr.valid:
                _log.warning("handle_restore_settings_backup: corrupt backup %s rejected: %s",
                             backup_id, vr.errors)
                # W1178: rollback to pre-restore state
                try:
                    self.store.save_settings(old_settings)
                    self.invalidate_cache()
                except Exception as rollback_exc:  # noqa: BLE001
                    _log.error(
                        "handle_restore_settings_backup: rollback failed: %s", rollback_exc
                    )
                raise ValueError(
                    f"Восстановление отклонено — бэкап содержит невалидные настройки: "
                    f"{'; '.join(vr.errors)}"
                )
            restored = vr.fixed

            self.store.save_settings(restored)
            self.invalidate_cache()

            _log.info("handle_restore_settings_backup: restored from %s", backup_id)
            # W1763 MED 2: kill-switch Sentry если восстановленный бэкап включает privacy_mode_enabled=True.
            self._maybe_disable_sentry_for_privacy(old_settings, restored)
            # W1308/W1341/W1436: reload pydantic settings and fire hooks
            self._reload_and_fire_hooks(old_settings, restored)
            # wave-35 CRIT: redact secrets in the IPC response (same logic as handle_get_settings).
            restored_safe = dict(restored)
            for k in self._SENSITIVE_FIELDS:
                if restored_safe.get(k):
                    restored_safe[k] = "REDACTED"
            result: dict = {"restored_settings": restored_safe, "backup_id": backup_id}
            if dropped_fields:
                result["warning"] = "credentials_dropped"
                result["dropped_fields"] = dropped_fields
            return result

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
        # Reject non-finite floats (NaN / ±Inf) — max/min propagates NaN unchanged
        # because all comparisons with NaN return False.
        if isinstance(parsed, float) and not math.isfinite(parsed):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))
