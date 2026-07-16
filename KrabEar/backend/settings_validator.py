"""SettingsValidator — валидация и миграция настроек Krab Ear.

Проверяет:
- типы полей (bool, float, int, str)
- допустимые диапазоны значений
- допустимые значения enum-полей
- автоисправление: clamping, coerce типов
- миграция схемы между версиями
"""

from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


CURRENT_SCHEMA_VERSION = "2.0"

# Определения enum-полей: ключ → допустимые значения
_ENUM_FIELDS: dict[str, tuple[str, ...]] = {
    "mode": ("headless", "menubar"),
    "quality_profile": ("balanced", "max"),
    "cleanup_profile": ("soft", "strict"),
    "translation_mode": ("off", "ru_to_es", "es_to_ru", "en_to_ru", "auto", "auto_to_ru", "bilingual_ru_es"),
    "translation_style": ("neutral", "chat", "formal"),
    "clipboard_mode": ("always_copy", "copy_on_fail", "never_copy"),
    "network_mode": ("offline_default", "offline_strict", "online_opt_in"),
    "hotkey_profile": ("default", "meeting", "translation"),
    "history_policy": ("unlimited",),
    "history_text_density": ("normal", "compact"),
    "capture_source_mode": ("mic", "system_audio", "mic_plus_system"),
    # Must mirror every PanelTab rawValue the Swift agent can persist
    # (native/KrabEarAgent/Sources/KrabEarAgent/HistoryPanelController.swift
    # enum PanelTab, written back in HistoryPanelController+LiveTranslation.swift
    # tabView(_:didSelect:) via tab.rawValue). A tab id missing here is silently
    # rewritten to 'dictation' on every set_settings call for that tab — spamming
    # a WARNING and breaking tab-restore-on-relaunch (e.g. "conversation" on every
    # "Разговор с AI" start via wake word / double Right Option).
    "ui_last_tab": (
        "dictation",
        "live_translation",
        "history",
        "conversation",
        "call_automation",
        "diagnostics",
        "archive",
    ),
    "update_channel": ("stable", "beta"),
    # cloud_rewriter_provider: valid providers for cloud transcript cleanup.
    "cloud_rewriter_provider": ("openai", "anthropic", "custom"),
    # cloud_stt_provider: valid providers for cloud STT fallback (engine.py::_transcribe_remote).
    "cloud_stt_provider": ("openai", "deepgram", "assemblyai"),
}

# Диапазоны числовых полей: ключ → (min, max, default, type)
_RANGE_FIELDS: dict[str, tuple[Any, Any, Any, type]] = {
    "history_page_size": (10, 500, 50, int),
    "audio_ducking_percent": (0, 100, 50, int),
    "overlay_opacity_percent": (15, 90, 45, int),
    "stop_tail_trim_ms": (0, 1200, 180, int),
    "silence_guard_rms_threshold": (0.0003, 0.05, 0.0020, float),
    "silence_guard_peak_threshold": (0.001, 0.2, 0.0120, float),
    "silence_guard_active_ratio_threshold": (0.001, 0.30, 0.015, float),
    "background_guard_min_peak": (0.003, 0.25, 0.025, float),
    "background_guard_min_rms": (0.0008, 0.08, 0.0040, float),
    "background_guard_uniform_frame_threshold": (0.001, 0.20, 0.0060, float),
    "background_guard_max_uniform_active_ratio": (0.40, 0.99, 0.92, float),
    "notify_confidence_threshold": (0.0, 1.0, 0.5, float),
    "call_budget_usd": (0.0, 1000.0, 2.0, float),
    # Realtime silence filter (wave-34 B1/B2/B3 + wave-1770 MED)
    "rt_silence_check_sec": (0.5, 60.0, 5.0, float),
    "rt_silence_window_sec": (1.0, 30.0, 10.0, float),
    "realtime_silence_threshold_db": (-80.0, -10.0, -55.0, float),
    "rt_partial_interval_sec": (0.1, 30.0, 1.0, float),
    # --- Live meeting overlay (C2a, спека 2026-07-10) ---
    "meeting_chunk_stt_interval_sec": (10.0, 120.0, 25.0, float),
    "meeting_items_interval_sec": (30.0, 600.0, 60.0, float),
    # wave-1770 MED: rt_silence_max_sec was missing from _RANGE_FIELDS — NaN/Inf
    # caused silence detection to permanently suppress or behave unpredictably.
    "rt_silence_max_sec": (0.5, 60.0, 8.0, float),
    # wave-1770 MED: llm_probe_interval_sec missing from _RANGE_FIELDS — setting it
    # to 999999 via IPC would delay LM Studio crash detection for days.
    "llm_probe_interval_sec": (1.0, 300.0, 30.0, float),
    # W1771 LOW: llm_timeout_sec uncapped — set_settings({llm_timeout_sec: 86400})
    # held _post_lock for the full duration, blocking all concurrent LLM calls.
    # Belt-and-suspenders: _timeout property also caps at 300.0.
    "llm_timeout_sec": (1.0, 300.0, 45.0, float),
    # Scheduled auto-purge (wave-34 lesson: clamp tunables to prevent CPU-spin).
    # auto_purge_retention_days: 1 day minimum, 10 years maximum.
    "auto_purge_retention_days": (1, 3650, 90, int),
    # auto_purge_check_interval_hours: 1 hour minimum, 1 week maximum.
    "auto_purge_check_interval_hours": (1, 168, 24, int),
    # wave2 F1-MED: stall watchdog for STT model download.
    # Below 30 s risks false-positive abort on slow mirrors; above 3600 s = 1 hour.
    "stt_download_stall_timeout_sec": (30.0, 3600.0, 300.0, float),
    # wave5: self-heal model reload timeout (Fix C).
    # Below 10 s risks false-abort on NVMe cold load; above 600 s = 10 min idle.
    "llm_autoload_timeout_sec": (10.0, 600.0, 90.0, float),
    # 2026-07-12: audio self-heal empty-recording streak threshold (see
    # backend/audio_selfheal.py). Below 2 a single false-positive silence-guard
    # trip would reinit PortAudio; above 10 the self-heal would rarely fire.
    "audio_selfheal_empty_threshold": (2, 10, 3, int),
    # Wake-word watchdog (спека 2026-07-15): heartbeat staleness порог перед
    # мягким reinit. Below 10 s risks false-positive reinit on a busy STT
    # inference chunk; above 120 s the watchdog would rarely fire.
    "wake_word_stale_sec": (10.0, 120.0, 30.0, float),
    # --- C2b: спикеры-лайт (спека §2.5 + амендмент §2.5a) ---
    "meeting_diar_interval_sec": (60.0, 600.0, 90.0, float),
    "meeting_diar_window_sec": (30.0, 180.0, 90.0, float),
    "meeting_speaker_match_threshold": (0.5, 0.95, 0.72, float),
}

# Bool-поля с дефолтными значениями
_BOOL_FIELDS: dict[str, bool] = {
    "auto_start_enabled": False,
    "show_dock_icon": True,
    "auto_paste": True,
    "play_start_sound": True,
    "realtime_preview_enabled": True,
    "translate_and_paste": False,
    "onboarding_completed": False,
    "audio_ducking_enabled": True,
    "silence_guard_enabled": True,
    "background_guard_enabled": True,
    "call_notify_default": True,
    "call_auto_summary": True,
    "history_focus_mode": True,
    "llm_rewrite_enabled": False,
    "auto_save_transcripts": False,
    "notifications_enabled": True,
    "notify_on_low_confidence": True,
    "notify_on_llm_failure": True,
    "notify_on_import_complete": True,
    "notify_sound_enabled": True,
    "privacy_mode_enabled": False,
    "stt_hotwords_enabled": True,
    "audio_selfheal_enabled": True,
    "wake_word_watchdog_enabled": True,
    "meeting_live_speakers_enabled": True,
}

# Миграционные таблицы: (from_version, to_version) → список операций
# Каждая операция: ("rename", old_key, new_key) | ("add_default", key, value) | ("remove", key)
_MIGRATIONS: dict[tuple[str, str], list[tuple]] = {
    ("1.0", "2.0"): [
        ("rename", "history_limit", "history_policy"),
        ("add_default", "overlay_opacity_percent", 45),
        ("add_default", "call_budget_usd", 2.0),
        ("add_default", "call_notify_default", True),
        ("add_default", "call_auto_summary", True),
        ("add_default", "llm_rewrite_enabled", False),
        ("add_default", "auto_save_transcripts", False),
        ("add_default", "notifications_enabled", True),
        ("add_default", "notify_on_low_confidence", True),
        ("add_default", "notify_confidence_threshold", 0.5),
        ("add_default", "notify_on_llm_failure", True),
        ("add_default", "notify_on_import_complete", True),
        ("add_default", "notify_sound_enabled", True),
        ("add_default", "capture_source_mode", "mic"),
        ("add_default", "ui_last_tab", "history"),
        ("add_default", "history_focus_mode", True),
    ],
}


def _is_allowed_gateway_url(url: str) -> bool:
    """Проверяет, допустим ли URL Voice Gateway.

    Разрешено:
    - HTTP или HTTPS на любом loopback-адресе (127.0.0.0/8, ::1, и все формы
      IPv6-mapped loopback типа ::ffff:127.0.0.1).
    - Имена «localhost» и «localhost.<tld>» (браузерное соглашение).
    - Внешний HTTPS (scheme=https, нелокальный хост).

    Блокировано:
    - HTTP на нелокальный хост (SSRF-вектор).
    - Любые другие схемы (ws://, ftp:// …).

    W850: заменяет старую проверку через startswith(), которая не покрывала
    http://[::1], http://[::ffff:7f00:1] и другие IPv6-loopback формы.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return False

    host = (parsed.hostname or "").lower()

    # Имя «localhost» (RFC 6761) — всегда loopback
    if host == "localhost":
        return True

    # Попытка разобрать как IP-литерал (urlparse убирает скобки для IPv6)
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return True  # 127.x.x.x, ::1, ::ffff:127.0.0.1 и т.п.
    except ValueError:
        pass  # не IP-литерал

    # Внешний HTTPS разрешён
    if scheme == "https":
        return True

    return False


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fixed: dict[str, Any] = field(default_factory=dict)


class SettingsValidator:
    """Валидатор и мигратор настроек Krab Ear."""

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def validate(self, settings: dict[str, Any]) -> ValidationResult:
        """Валидирует settings, автоисправляет где возможно.

        Returns:
            ValidationResult с флагом valid=True если нет неисправимых ошибок.
            Поле `fixed` содержит исправленную копию словаря настроек.
        """
        fixed = dict(settings)
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Enum-поля
        for key, allowed in _ENUM_FIELDS.items():
            if key not in fixed:
                continue
            val = fixed[key]
            if val not in allowed:
                warnings.append(
                    f"'{key}': недопустимое значение {val!r}, исправлено на '{allowed[0]}'"
                )
                fixed[key] = allowed[0]

        # 2. Диапазоны числовых полей
        for key, (min_v, max_v, default, coerce) in _RANGE_FIELDS.items():
            if key not in fixed:
                continue
            val = fixed[key]
            try:
                parsed = coerce(val)
            except (TypeError, ValueError):
                warnings.append(
                    f"'{key}': не удалось преобразовать {val!r} в {coerce.__name__}, "
                    f"исправлено на {default}"
                )
                fixed[key] = default
                continue
            # Reject non-finite floats (NaN / ±Inf) before range comparison — NaN bypasses
            # `< min_v or > max_v` silently (both comparisons return False for NaN) and
            # would persist as an invalid JSON value that breaks strict parsers / Swift JSONDecoder.
            if isinstance(parsed, float) and not math.isfinite(parsed):
                warnings.append(
                    f"'{key}': значение {parsed} не является конечным числом, "
                    f"исправлено на {default}"
                )
                fixed[key] = default
                continue
            if parsed < min_v or parsed > max_v:
                clamped = max(min_v, min(parsed, max_v))
                warnings.append(
                    f"'{key}': значение {parsed} вне диапазона [{min_v}, {max_v}], "
                    f"исправлено на {clamped}"
                )
                fixed[key] = clamped
            else:
                fixed[key] = parsed  # нормализуем тип

        # 3. Bool-поля
        for key, default_val in _BOOL_FIELDS.items():
            if key not in fixed:
                continue
            val = fixed[key]
            coerced = self._coerce_bool(val)
            if coerced is None:
                warnings.append(
                    f"'{key}': не удалось преобразовать {val!r} в bool, "
                    f"исправлено на {default_val}"
                )
                fixed[key] = default_val
            else:
                fixed[key] = coerced

        # 4. Специальные поля
        # translation_glossary должен быть dict
        if "translation_glossary" in fixed and not isinstance(fixed["translation_glossary"], dict):
            warnings.append(
                f"'translation_glossary': ожидается dict, получен {type(fixed['translation_glossary']).__name__}, "
                f"исправлено на {{}}"
            )
            fixed["translation_glossary"] = {}

        # text_templates должен быть dict[str, str]
        if "text_templates" in fixed:
            tt = fixed["text_templates"]
            if not isinstance(tt, dict):
                warnings.append(
                    f"'text_templates': ожидается dict, получен {type(tt).__name__}, исправлено на {{}}"
                )
                fixed["text_templates"] = {}
            else:
                cleaned: dict[str, str] = {}
                for k, v in tt.items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        warnings.append(
                            f"'text_templates': ключ {k!r} или значение {v!r} не строка, пропущено"
                        )
                        continue
                    if k.strip() and v.strip():
                        cleaned[k.strip()] = v.strip()
                fixed["text_templates"] = cleaned

        # stt_hotwords должен быть list[str] непустых строк.
        # W1768: import_settings/restore_settings_backup вызывают только validate(),
        # минуя нормализацию из set_settings.  Без этой проверки крафтнутый
        # stt_hotwords=[null, 123, {"k": "v"}] из /tmp/payload.json проходил насквозь,
        # а затем recording_core_service.py (`_w.strip()`) и transcript_context.py
        # (`w.strip()`) падали с AttributeError на не-строковых элементах →
        # тихий крах потока транскрипции.  Приводим к list, отбрасываем не-строки
        # (со структурированным warning), стрипаем и оставляем только непустые.
        if "stt_hotwords" in fixed:
            hw = fixed["stt_hotwords"]
            if not isinstance(hw, list):
                warnings.append(
                    f"'stt_hotwords': ожидается list, получен {type(hw).__name__}, исправлено на []"
                )
                fixed["stt_hotwords"] = []
            else:
                cleaned_hw: list[str] = []
                for item in hw:
                    if not isinstance(item, str):
                        warnings.append(
                            f"'stt_hotwords': элемент {item!r} не строка, пропущено"
                        )
                        continue
                    stripped = item.strip()
                    if stripped:
                        cleaned_hw.append(stripped)
                fixed["stt_hotwords"] = cleaned_hw

        # voice_gateway_url: должен быть localhost/loopback (HTTP или HTTPS) или внешний HTTPS.
        # W850: старая проверка по строковому префиксу пропускала IPv6-loopback формы
        # (http://[::1], http://[::ffff:127.0.0.1] и т.д.).  Теперь используем urlparse +
        # ipaddress.is_loopback для корректного охвата всех loopback-адресов.
        if "voice_gateway_url" in fixed:
            gw_url = str(fixed["voice_gateway_url"]).strip()
            if not _is_allowed_gateway_url(gw_url):
                errors.append(
                    f"'voice_gateway_url': должен быть loopback (любой HTTP/HTTPS) "
                    f"или внешний HTTPS, получен {gw_url!r}"
                )

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            fixed=fixed,
        )

    def migrate(self, settings: dict[str, Any], from_version: str, to_version: str) -> dict[str, Any]:
        """Мигрирует settings из from_version в to_version.

        Поддерживает только последовательные переходы через известные версии.
        """
        if from_version == to_version:
            return dict(settings)

        result = dict(settings)
        version_chain = self._build_migration_chain(from_version, to_version)

        for step_from, step_to in version_chain:
            ops = _MIGRATIONS.get((step_from, step_to))
            if ops is None:
                raise ValueError(
                    f"Нет пути миграции из версии {step_from!r} в {step_to!r}"
                )
            result = self._apply_migration_ops(result, ops)

        return result

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_bool(value: Any) -> bool | None:
        """Пробует преобразовать value в bool. Возвращает None если не удалось."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "on", "yes"}:
                return True
            if normalized in {"0", "false", "off", "no"}:
                return False
        return None

    @staticmethod
    def _build_migration_chain(from_version: str, to_version: str) -> list[tuple[str, str]]:
        """Строит цепочку шагов миграции."""
        # Упрощённая версия: только прямые переходы
        known = list(_MIGRATIONS.keys())
        # Строим граф переходов
        from_v = from_version
        chain: list[tuple[str, str]] = []
        visited: set[str] = {from_v}
        while from_v != to_version:
            step = next(
                ((f, t) for (f, t) in known if f == from_v),
                None,
            )
            if step is None:
                raise ValueError(f"Нет пути миграции из {from_v!r} в {to_version!r}")
            chain.append(step)
            from_v = step[1]
            if from_v in visited:
                raise ValueError(f"Цикл в цепочке миграций: {from_v!r}")
            visited.add(from_v)
        return chain

    @staticmethod
    def _apply_migration_ops(settings: dict[str, Any], ops: list[tuple]) -> dict[str, Any]:
        """Применяет список операций миграции к копии словаря настроек."""
        result = dict(settings)
        for op in ops:
            kind = op[0]
            if kind == "rename":
                _, old_key, new_key = op
                if old_key in result and new_key not in result:
                    result[new_key] = result.pop(old_key)
            elif kind == "add_default":
                _, key, default_value = op
                if key not in result:
                    result[key] = default_value
            elif kind == "remove":
                _, key = op
                result.pop(key, None)
        return result
