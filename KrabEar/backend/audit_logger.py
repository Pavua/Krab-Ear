"""Audit logger для IPC-методов Krab Ear.

Каждый вызов IPC-метода записывается в append-only NDJSON файл.
Файлы ротируются ежедневно, хранятся последние 7 дней.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.AuditLogger")

# Методы, параметры которых полностью redact-ятся в audit log (чувствительные данные).
# Категории:
#   A — API-ключи / токены / credentials (set_settings содержит voice_gateway_api_key,
#       hf_token, lm_studio_api_key, rest_api_key, telnyx/twilio credentials и т.д.)
#   B — полный текст транскрипций (translate_*, send_to_telegram, send_imessage, …)
#   C — файловые пути (transcribe_paths, export_timeline_*, configure_obsidian_sync, …)
#   D — персональные данные / calendar / messaging
#   E — пресеты/импорт настроек (могут содержать credentials из файла)
_SENSITIVE_METHODS = frozenset({
    # --- A: настройки содержат credentials ---
    "set_settings",                      # voice_gateway_api_key, hf_token, api_keys, …
    "set_notification_preferences",      # может содержать webhook URLs с токенами
    "apply_profile_preset",              # применяет набор settings (credentials possible)
    "import_settings",                   # импортирует settings.json — может содержать секреты
    "restore_settings_backup",           # восстанавливает settings с credentials
    "apply_config_preset",               # возвращает settings_patch с credentials
    "create_config_preset",              # сохраняет произвольный settings_patch
    # --- B: полный текст транскрипций ---
    "translate_text",                    # params: text (полный текст)
    "translate_selection",               # params: text (выделенный текст)
    "send_to_telegram",                  # params: text (транскрипция), chat_id
    "send_imessage",                     # params: text (сообщение), recipient
    "summarize_text",                    # params: text
    "summarize_item",                    # params: item_id → text leak через result
    "post_process_text",                 # params: text
    "score_readability",                 # params: text
    "compare_texts",                     # params: text_a, text_b
    "detect_emotion",                    # params: text
    "expand_abbreviations",              # params: text
    "extract_terms",                     # params: text
    "format_for_paste",                  # params: text + target_app
    "generate_auto_title",              # params: text
    "score_transcription",               # params: text
    "check_duplicate",                   # params: text
    "replace_word_in_last_transcript",   # params: old_word, new_word (transcript mutation)
    "synthesize_speech",                 # params: text (синтез речи)
    "live_subs_ingest",                  # params: audio_b64 (base64 PCM chunks — большой объём)
    "semantic_search",                   # params: query (текст поиска)
    "check_hotwords",                    # params: text (транскрипция)
    # --- C: файловые пути ---
    "transcribe_paths",                  # params: paths (список файловых путей)
    "transcribe_paths_async",            # params: paths
    "preview_transcribe_paths",          # params: paths
    "enqueue_transcription",             # params: path
    "export_timeline_svg",               # params: output_path
    "export_timeline_json",              # params: output_path
    "export_timeline_ical",              # params: output_path
    "configure_auto_export",             # params: output_dir
    "configure_obsidian_sync",           # params: vault_path
    "export_settings",                   # params: file_path (куда экспортировать settings)
    "analyze_audio_quality",             # params: file_path (полный путь к аудио)
    "analyze_silence",                   # params: file_path
    "get_audio_info",                    # params: file_path
    "profile_noise",                     # params: file_path
    "get_waveform",                      # params: file_path
    "check_audio_duplicate",             # params: file_path
    "analyze_word_timing",               # params: file_path
    # --- D: персональные данные (Calendar, Notes, Reminders, Telegram) ---
    "create_apple_note",                 # params: title, body (personal data)
    "create_apple_reminder",             # params: title, body, due_date
    "create_calendar_event",             # params: title, start_time, end_time, notes
    "call_session_create",               # params: phone_number (phone PII)
    "call_session_add_transcript",       # params: text (call transcript)
    # --- E: webhook URLs (могут содержать API-токены в URL) ---
    "register_webhook",                  # params: url (может быть webhook secret URL)
})

_KEEP_DAYS = 7


class AuditLogger:
    """Логирует IPC-запросы в append-only NDJSON файлы с ежедневной ротацией."""

    # Cleanup runs at most once per this many seconds to avoid per-call glob overhead.
    _CLEANUP_INTERVAL_S: float = 60.0

    def __init__(self, data_dir: str | os.PathLike) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_date: str = ""
        self._file_handle = None
        self._last_cleanup_ts: float = 0.0

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def log_request(
        self,
        method: str,
        params: dict,
        result: dict,
        duration_ms: float,
        client_info: dict | None = None,
    ) -> None:
        """Записывает одну запись аудита."""
        from backend.observability import add_breadcrumb as _add_bc  # lazy — avoid circular
        ts = datetime.now(timezone.utc).isoformat()
        success = bool(result.get("ok", False)) if isinstance(result, dict) else False

        _add_bc(
            category="audit",
            message="ipc_request",
            level="debug",
            data={
                "method": method,
                "duration_ms": round(duration_ms, 2),
                "ok": success,
            },
        )

        if method in _SENSITIVE_METHODS:
            params_info: dict[str, Any] = {"redacted": True, "param_count": len(params) if params else 0}
        else:
            params_info = {"params_keys": sorted(params.keys()) if params else []}

        entry: dict[str, Any] = {
            "ts": ts,
            "method": method,
            **params_info,
            "success": success,
            "duration_ms": round(duration_ms, 2),
        }
        if client_info:
            entry["client_info"] = client_info

        line = json.dumps(entry, ensure_ascii=False)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            self._rotate_if_needed(today)
            try:
                if self._file_handle is None:
                    logger.warning("audit log файл недоступен, запись пропущена: %s", method)
                else:
                    self._file_handle.write(line + "\n")
                    self._file_handle.flush()
            except Exception:
                logger.exception("Ошибка записи в audit log")

            now = time.monotonic()
            if now - self._last_cleanup_ts >= self._CLEANUP_INTERVAL_S:
                self._last_cleanup_ts = now
                self._cleanup_old_files()

    def get_audit_log(
        self,
        limit: int = 100,
        method_filter: str | None = None,
    ) -> list[dict]:
        """Возвращает последние записи из всех доступных файлов аудита."""
        files = sorted(self._data_dir.glob("audit_*.ndjson"), reverse=True)
        entries: list[dict] = []

        for path in files:
            if len(entries) >= limit:
                break
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if method_filter and entry.get("method") != method_filter:
                        continue
                    entries.append(entry)
                    if len(entries) >= limit:
                        break
            except Exception:
                logger.exception("Ошибка чтения audit log файла %s", path)

        return entries

    def close(self) -> None:
        """Закрывает текущий файловый дескриптор."""
        with self._lock:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
                self._file_handle = None

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _audit_path(self, date: str) -> Path:
        return self._data_dir / f"audit_{date}.ndjson"

    def _rotate_if_needed(self, today: str) -> None:
        """Открывает новый файл при смене даты (вызывается под self._lock).

        При PermissionError или любой OSError логирует предупреждение и пропускает
        ротацию — текущий дескриптор остаётся без изменений, запись продолжается
        в старый файл (или будет поймана в log_request).
        """
        if today != self._current_date:
            if self._file_handle is not None:
                try:
                    self._file_handle.close()
                except Exception:
                    pass
                self._file_handle = None
            path = self._audit_path(today)
            try:
                self._file_handle = open(path, "a", encoding="utf-8")
                self._current_date = today
            except (PermissionError, OSError) as exc:
                logger.warning(
                    "Не удалось открыть audit log файл %s: %s — ротация пропущена",
                    path,
                    exc,
                )

    def _cleanup_old_files(self) -> None:
        """Удаляет файлы аудита старше _KEEP_DAYS дней."""
        files = sorted(self._data_dir.glob("audit_*.ndjson"))
        if len(files) <= _KEEP_DAYS:
            return
        for old_file in files[: len(files) - _KEEP_DAYS]:
            try:
                old_file.unlink()
            except Exception:
                logger.warning("Не удалось удалить старый audit log: %s", old_file)
