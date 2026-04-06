"""IPC backend-сервис Krab Ear.

Сервис слушает Unix socket и обрабатывает JSON-RPC-подобные команды:
- start_recording / stop_recording
- get_history_page / search_history / delete_history_item
- get_settings / set_settings
- compact_history
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time
from typing import Any, Callable
from urllib import parse as urllib_parse
from urllib import error as urllib_error, request as urllib_request
import uuid

import numpy as np

# Обеспечиваем корректный импорт модулей KrabEar при запуске как standalone скрипта.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.event_bus import bus as event_bus
from backend.models import DEFAULT_SETTINGS
from contracts.stt_events import SttFailed, SttFinal
from contracts.registry import EventType
from backend.recorder import AudioRecorder
from backend.state_store import StateStore
from backend.transcriber import Transcriber
from backend.translator import Translator
from core.utils import TextUtils

logger = logging.getLogger("KrabEar.Backend.Service")


class BackendService:
    """Бизнес-логика сервиса: запись, транскрибация, история и настройки."""

    def __init__(
        self,
        store: StateStore,
        recorder: AudioRecorder | None = None,
        transcriber: Transcriber | None = None,
        translator: Translator | None = None,
    ) -> None:
        self.store = store
        self.recorder = recorder or AudioRecorder()
        self.transcriber = transcriber or Transcriber()
        self.translator = translator or Translator()
        self._preview_lock = threading.Lock()
        self._preview_thread: threading.Thread | None = None
        self._preview_stop_event = threading.Event()
        self._preview_text = ""
        self._preview_duration_sec = 0.0
        self._preview_updated_at = 0.0
        self._call_assist_lock = threading.Lock()
        self._call_assist_state: dict[str, Any] = {
            "active": False,
            "status": "idle",
            "session_id": None,
            "gateway_session_id": None,
        }

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Обрабатывает один JSON-запрос и возвращает JSON-ответ."""
        request_id = payload.get("id")
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, "invalid_params", "Параметр params должен быть объектом")

        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ping": self._handle_ping,
            "start_recording": self._handle_start_recording,
            "stop_recording": self._handle_stop_recording,
            "get_recording_state": self._handle_get_recording_state,
            "start_call_assist": self._handle_start_call_assist,
            "stop_call_assist": self._handle_stop_call_assist,
            "get_call_assist_state": self._handle_get_call_assist_state,
            "call_assist_diagnostics": self._handle_call_assist_diagnostics,
            "call_assist_summary": self._handle_call_assist_summary,
            "call_assist_quick_phrase": self._handle_call_assist_quick_phrase,
            "list_call_assist_quick_phrases": self._handle_list_call_assist_quick_phrases,
            "call_assist_cost_estimate": self._handle_call_assist_cost_estimate,
            "call_assist_timeline": self._handle_call_assist_timeline,
            "call_assist_timeline_stats": self._handle_call_assist_timeline_stats,
            "call_assist_timeline_summary": self._handle_call_assist_timeline_summary,
            "call_assist_timeline_export": self._handle_call_assist_timeline_export,
            "call_assist_timeline_clear": self._handle_call_assist_timeline_clear,
            "call_assist_timeline_to_history": self._handle_call_assist_timeline_to_history,
            "list_audio_inputs": self._handle_list_audio_inputs,
            "get_history_page": self._handle_get_history_page,
            "search_history": self._handle_search_history,
            "delete_history_item": self._handle_delete_history_item,
            "set_paste_status": self._handle_set_paste_status,
            "get_settings": self._handle_get_settings,
            "set_settings": self._handle_set_settings,
            "compact_history": self._handle_compact_history,
            "add_history_item": self._handle_add_history_item,
            "transcribe_paths": self._handle_transcribe_paths,
            "preview_transcribe_paths": self._handle_preview_transcribe_paths,
            "translate_text": self._handle_translate_text,
            "get_capabilities": self._handle_get_capabilities,
            "get_readiness": self._handle_get_readiness,
            "set_translation_glossary_item": self._handle_set_translation_glossary_item,
            "remove_translation_glossary_item": self._handle_remove_translation_glossary_item,
            "import_history_ndjson": self._handle_import_history_ndjson,
            "get_history_stats": self._handle_get_history_stats,
            "get_history_overview": self._handle_get_history_overview,
            "summarize_text": self._handle_summarize_text,
        }

        handler = handlers.get(method)
        if handler is None:
            return self._error(request_id, "unknown_method", f"Неизвестный метод: {method}")

        try:
            result = handler(params)
            return {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            logger.exception("Ошибка метода %s", method)
            return self._error(request_id, "internal_error", str(exc))

    def _handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok", "service": "krabear-backend", "version": "1.0.0"}

    def _handle_start_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        started = self.recorder.start()
        if not started:
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            # Идемпотентный контракт: повторный start не считается ошибкой.
            return {
                "status": "already_recording",
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
            }
        self._reset_preview_state()
        settings = self.store.load_settings()
        if bool(settings.get("realtime_preview_enabled", True)):
            quality_profile = str(settings.get("quality_profile", "balanced"))
            self._start_preview_worker(quality_profile=quality_profile)
        return {"status": "recording"}

    def _handle_stop_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        self._stop_preview_worker()
        settings = self.store.load_settings()
        stop_tail_trim_ms = self._coerce_bounded_int(
            value=params.get("stop_tail_trim_ms", settings.get("stop_tail_trim_ms", 180)),
            default=180,
            min_value=0,
            max_value=1200,
        )
        stopped = self._stop_recorder_guarded(stop_tail_trim_ms=stop_tail_trim_ms)
        if stopped is None:
            # Идемпотентный контракт: повторный stop не считается ошибкой.
            with self._preview_lock:
                preview_text = self._preview_text
                preview_duration = self._preview_duration_sec
            return {
                "status": "already_stopped",
                "is_recording": False,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
                "stop_tail_trim_ms": stop_tail_trim_ms,
            }

        audio, duration_sec = stopped
        quality_profile = str(
            params.get("quality_profile") or settings.get("quality_profile", "balanced")
        )
        cleanup_profile = str(
            params.get("cleanup_profile") or settings.get("cleanup_profile", "soft")
        )
        lang_hint: str | None = params.get("lang_hint") or None
        translation_mode = str(
            params.get("translation_mode") or settings.get("translation_mode", "off")
        )
        translation_style = str(
            params.get("translation_style") or settings.get("translation_style", "neutral")
        )
        translation_glossary = settings.get("translation_glossary", {})
        translate_and_paste = bool(
            params.get("translate_and_paste")
            if "translate_and_paste" in params
            else settings.get("translate_and_paste", False)
        )
        network_mode = str(settings.get("network_mode", "offline_default"))
        silence_guard_enabled = self._coerce_bool(settings.get("silence_guard_enabled", True), default=True)
        silence_rms_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_rms_threshold", 0.0020),
            default=0.0020,
            min_value=0.0003,
            max_value=0.05,
        )
        silence_peak_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_peak_threshold", 0.0120),
            default=0.0120,
            min_value=0.001,
            max_value=0.2,
        )
        silence_active_ratio_threshold = self._coerce_bounded_float(
            value=settings.get("silence_guard_active_ratio_threshold", 0.015),
            default=0.015,
            min_value=0.001,
            max_value=0.30,
        )
        background_guard_enabled = self._coerce_bool(settings.get("background_guard_enabled", True), default=True)
        background_guard_min_peak = self._coerce_bounded_float(
            value=settings.get("background_guard_min_peak", 0.025),
            default=0.025,
            min_value=0.003,
            max_value=0.25,
        )
        background_guard_min_rms = self._coerce_bounded_float(
            value=settings.get("background_guard_min_rms", 0.0040),
            default=0.0040,
            min_value=0.0008,
            max_value=0.08,
        )
        background_guard_uniform_frame_threshold = self._coerce_bounded_float(
            value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
            default=0.0060,
            min_value=0.001,
            max_value=0.20,
        )
        background_guard_max_uniform_active_ratio = self._coerce_bounded_float(
            value=settings.get("background_guard_max_uniform_active_ratio", 0.92),
            default=0.92,
            min_value=0.40,
            max_value=0.99,
        )
        sample_rate = self._coerce_bounded_int(
            value=getattr(self.recorder, "sample_rate", 16000),
            default=16000,
            min_value=8000,
            max_value=192000,
        )

        if getattr(audio, "size", 0) == 0:
            return {
                "status": "empty_audio",
                "duration_sec": duration_sec,
                "quality_profile": quality_profile,
                "cleanup_profile": cleanup_profile,
                "translation_mode": translation_mode,
                "translate_and_paste": translate_and_paste,
                "text": "",
                "original_text": "",
                "translated_text": "",
                "translation_status": "not_requested",
                "history_id": None,
                "stop_tail_trim_ms": stop_tail_trim_ms,
                "silence_detected": False,
                "silence_guard_enabled": silence_guard_enabled,
                "background_guard_rejected": False,
            }

        silence_detected = False
        if silence_guard_enabled:
            silence_detected = self._looks_like_silence_audio(
                audio=audio,
                sample_rate=sample_rate,
                rms_threshold=silence_rms_threshold,
                peak_threshold=silence_peak_threshold,
                active_ratio_threshold=silence_active_ratio_threshold,
            )
            if silence_detected:
                logger.info(
                    "Silence guard: stop_recording классифицирован как тишина, STT пропущен",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "rms_threshold": silence_rms_threshold,
                        "peak_threshold": silence_peak_threshold,
                        "active_ratio_threshold": silence_active_ratio_threshold,
                    },
                )
                return {
                    "status": "empty_audio",
                    "duration_sec": duration_sec,
                    "quality_profile": quality_profile,
                    "cleanup_profile": cleanup_profile,
                    "translation_mode": translation_mode,
                    "translate_and_paste": translate_and_paste,
                    "text": "",
                    "original_text": "",
                    "translated_text": "",
                    "translation_status": "not_requested",
                    "history_id": None,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                    "silence_detected": True,
                    "silence_guard_enabled": True,
                    "background_guard_rejected": False,
                }

        background_guard_rejected = False
        if background_guard_enabled:
            background_guard_rejected = self._looks_like_distant_background_speech(
                audio=audio,
                sample_rate=sample_rate,
                min_peak=background_guard_min_peak,
                min_rms=background_guard_min_rms,
                uniform_frame_threshold=background_guard_uniform_frame_threshold,
                max_uniform_active_ratio=background_guard_max_uniform_active_ratio,
            )
            if background_guard_rejected:
                logger.info(
                    "Background guard: stop_recording отклонен как фоновая речь",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "min_peak": background_guard_min_peak,
                        "min_rms": background_guard_min_rms,
                        "uniform_frame_threshold": background_guard_uniform_frame_threshold,
                        "max_uniform_active_ratio": background_guard_max_uniform_active_ratio,
                    },
                )
                return {
                    "status": "empty_audio",
                    "duration_sec": duration_sec,
                    "quality_profile": quality_profile,
                    "cleanup_profile": cleanup_profile,
                    "translation_mode": translation_mode,
                    "translate_and_paste": translate_and_paste,
                    "text": "",
                    "original_text": "",
                    "translated_text": "",
                    "translation_status": "not_requested",
                    "history_id": None,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                    "silence_detected": False,
                    "silence_guard_enabled": silence_guard_enabled,
                    "background_guard_rejected": True,
                }

        transcribe_payload = self.transcriber.transcribe(
            audio,
            quality_profile=quality_profile,
            cleanup_profile=cleanup_profile,
            lang_hint=lang_hint,
        )
        text = self._postprocess_transcribed_text(self._extract_transcribed_text(transcribe_payload))
        transcription_error = self._extract_transcribed_error(transcribe_payload)
        if not text:
            if transcription_error:
                event_bus.emit_typed(EventType.STT_FAILED, SttFailed(reason=transcription_error, duration_sec=duration_sec))
            return {
                "status": "empty_text",
                "duration_sec": duration_sec,
                "quality_profile": quality_profile,
                "cleanup_profile": cleanup_profile,
                "translation_mode": translation_mode,
                "translate_and_paste": translate_and_paste,
                "text": "",
                "original_text": "",
                "translated_text": "",
                "translation_status": "not_requested",
                "history_id": None,
                "transcription_error": transcription_error,
                "stop_tail_trim_ms": stop_tail_trim_ms,
                "silence_detected": silence_detected,
                "silence_guard_enabled": silence_guard_enabled,
                "background_guard_rejected": background_guard_rejected,
            }

        translation = self.translator.translate(
            text=text,
            mode=translation_mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=translation_glossary,
        )
        translated_text = translation.text.strip() if translation.ok else ""
        final_text = translated_text if (translate_and_paste and translated_text) else text
        translation_status = translation.status

        item = self.store.add_history_item(
            text=final_text,
            paste_status="failed",
            source_text=text,
            translated_text=translated_text,
            translation_mode=translation.mode,
            source_lang=translation.source_lang,
            target_lang=translation.target_lang,
            translation_status=translation_status,
            translation_engine=translation.engine,
        )
        result_payload = {
            "status": "ok",
            "duration_sec": duration_sec,
            "quality_profile": quality_profile,
            "cleanup_profile": cleanup_profile,
            "translation_mode": translation.mode,
            "translation_style": translation_style,
            "translate_and_paste": translate_and_paste,
            "translation_status": translation_status,
            "source_lang": translation.source_lang,
            "target_lang": translation.target_lang,
            "translation_engine": translation.engine,
            "text": final_text,
            "original_text": text,
            "translated_text": translated_text,
            "history_id": item.id,
            "ts": item.ts,
            "stop_tail_trim_ms": stop_tail_trim_ms,
            "silence_detected": silence_detected,
            "silence_guard_enabled": silence_guard_enabled,
            "background_guard_rejected": background_guard_rejected,
        }
        tp = transcribe_payload if isinstance(transcribe_payload, dict) else {}
        event_bus.emit_typed(EventType.STT_FINAL, SttFinal(
            history_id=item.id,
            text=final_text,
            duration_sec=duration_sec,
            language=tp.get("language"),
            confidence=tp.get("confidence"),
        ))
        return result_payload

    def _handle_get_recording_state(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._preview_lock:
            preview_text = self._preview_text
            preview_duration = self._preview_duration_sec
        return {
            "is_recording": bool(getattr(self.recorder, "is_recording", False)),
            "duration_sec": preview_duration,
            "preview_text": preview_text,
        }

    def _handle_add_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("Пустой текст нельзя добавить в историю")
        paste_status = str(params.get("paste_status", "failed"))
        item = self.store.add_history_item(
            text=text,
            paste_status=paste_status,
            source_text=str(params.get("source_text", "")).strip(),
            translated_text=str(params.get("translated_text", "")).strip(),
            translation_mode=str(params.get("translation_mode", "off")).strip() or "off",
            source_lang=str(params.get("source_lang", "")).strip(),
            target_lang=str(params.get("target_lang", "")).strip(),
            translation_status=str(params.get("translation_status", "not_requested")).strip() or "not_requested",
            translation_engine=str(params.get("translation_engine", "")).strip(),
        )
        return item.to_dict()

    def _handle_get_history_page(self, params: dict[str, Any]) -> dict[str, Any]:
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.get_history_page_filtered(
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def _handle_search_history(self, params: dict[str, Any]) -> dict[str, Any]:
        query = str(params.get("query", "")).strip()
        cursor = params.get("cursor")
        cursor_str = None if cursor is None else str(cursor)
        limit = int(params.get("limit", 50))
        paste_status = params.get("paste_status")
        paste_status_str = None if paste_status is None else str(paste_status)
        translation_mode = params.get("translation_mode")
        translation_mode_str = None if translation_mode is None else str(translation_mode)
        translation_status = params.get("translation_status")
        translation_status_str = None if translation_status is None else str(translation_status)
        from_ts = params.get("from_ts")
        from_ts_str = None if from_ts is None else str(from_ts)
        to_ts = params.get("to_ts")
        to_ts_str = None if to_ts is None else str(to_ts)
        items, next_cursor = self.store.search_history(
            query=query,
            cursor=cursor_str,
            limit=limit,
            paste_status=paste_status_str,
            translation_mode=translation_mode_str,
            translation_status=translation_status_str,
            from_ts=from_ts_str,
            to_ts=to_ts_str,
        )
        return {"items": items, "next_cursor": next_cursor}

    def _handle_delete_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        item_id = str(params.get("id", "")).strip()
        ok = self.store.delete_history_item(item_id)
        if not ok:
            raise RuntimeError("Не указан id для удаления")
        return {"deleted": True, "id": item_id}

    def _handle_set_paste_status(self, params: dict[str, Any]) -> dict[str, Any]:
        item_id = str(params.get("id", "")).strip()
        paste_status = str(params.get("paste_status", "failed")).strip() or "failed"
        ok = self.store.set_paste_status(item_id=item_id, paste_status=paste_status)
        if not ok:
            raise RuntimeError("Не удалось обновить paste_status")
        return {"updated": True, "id": item_id, "paste_status": paste_status}

    def _handle_get_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.store.load_settings()

    def _handle_set_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self.store.load_settings()
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
        settings["voice_gateway_url"] = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
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

        settings["stop_tail_trim_ms"] = self._coerce_bounded_int(
            value=settings.get("stop_tail_trim_ms", 180),
            default=180,
            min_value=0,
            max_value=1200,
        )
        settings["silence_guard_rms_threshold"] = self._coerce_bounded_float(
            value=settings.get("silence_guard_rms_threshold", 0.0020),
            default=0.0020,
            min_value=0.0003,
            max_value=0.05,
        )
        settings["silence_guard_peak_threshold"] = self._coerce_bounded_float(
            value=settings.get("silence_guard_peak_threshold", 0.0120),
            default=0.0120,
            min_value=0.001,
            max_value=0.2,
        )
        settings["silence_guard_active_ratio_threshold"] = self._coerce_bounded_float(
            value=settings.get("silence_guard_active_ratio_threshold", 0.015),
            default=0.015,
            min_value=0.001,
            max_value=0.30,
        )
        settings["background_guard_min_peak"] = self._coerce_bounded_float(
            value=settings.get("background_guard_min_peak", 0.025),
            default=0.025,
            min_value=0.003,
            max_value=0.25,
        )
        settings["background_guard_min_rms"] = self._coerce_bounded_float(
            value=settings.get("background_guard_min_rms", 0.0040),
            default=0.0040,
            min_value=0.0008,
            max_value=0.08,
        )
        settings["background_guard_uniform_frame_threshold"] = self._coerce_bounded_float(
            value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
            default=0.0060,
            min_value=0.001,
            max_value=0.20,
        )
        settings["background_guard_max_uniform_active_ratio"] = self._coerce_bounded_float(
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

        return self.store.save_settings(settings)

    def _handle_translate_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отдельная IPC-команда перевода текста для UI и будущих workflow."""
        text = str(params.get("text", "")).strip()
        mode = str(params.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style", "neutral"))
        settings = self.store.load_settings()
        network_mode = str(params.get("network_mode") or settings.get("network_mode", "offline_default"))
        glossary = settings.get("translation_glossary", {})
        result = self.translator.translate(
            text=text,
            mode=mode,
            network_mode=network_mode,
            translation_style=translation_style,
            glossary=glossary,
        )
        return {
            "text": result.text,
            "status": result.status,
            "source_lang": result.source_lang,
            "target_lang": result.target_lang,
            "translation_mode": result.mode,
            "translation_style": translation_style,
            "engine": result.engine,
        }

    # ------------------------------------------------------------------
    # Readiness probing — честная проверка доступности компонентов
    # ------------------------------------------------------------------

    @staticmethod
    def _hf_model_cached(hf_repo: str) -> bool:
        """Проверяет наличие модели в локальном кэше HuggingFace Hub."""
        cache_base = Path.home() / ".cache" / "huggingface" / "hub"
        folder = "models--" + hf_repo.replace("/", "--")
        return (cache_base / folder).exists()

    @staticmethod
    def _probe_stt() -> dict[str, Any]:
        """Проверяет доступность STT моделей без их загрузки."""
        from core.config import settings as cfg
        balanced_cached = BackendService._hf_model_cached(cfg.MODEL_BALANCED)
        max_cached = [m for m in cfg.model_max_list if BackendService._hf_model_cached(m)]
        return {
            "balanced_model": cfg.MODEL_BALANCED,
            "balanced_cached": balanced_cached,
            "max_models_cached": max_cached,
            "ready": balanced_cached,
        }

    @staticmethod
    def _probe_diarization() -> dict[str, Any]:
        """Проверяет доступность pyannote diarization без загрузки pipeline."""
        from core.config import settings as cfg
        hf_token = os.environ.get("HF_TOKEN") or cfg.HF_TOKEN
        has_token = bool(hf_token)
        model_cached = BackendService._hf_model_cached(cfg.DIARIZATION_MODEL)
        return {
            "model": cfg.DIARIZATION_MODEL,
            "has_hf_token": has_token,
            "model_cached": model_cached,
            "ready": has_token and model_cached,
        }

    @staticmethod
    def _probe_translation() -> dict[str, Any]:
        """Проверяет наличие моделей перевода Helsinki-NLP в локальном кэше."""
        _TRANSLATION_MODELS = {
            "ru_to_es": "Helsinki-NLP/opus-mt-ru-es",
            "es_to_ru": "Helsinki-NLP/opus-mt-es-ru",
            "en_to_ru": "Helsinki-NLP/opus-mt-en-ru",
        }
        cache_base = Path.home() / ".cache" / "huggingface" / "hub"
        cached: list[str] = []
        missing: list[str] = []
        for mode, repo in _TRANSLATION_MODELS.items():
            folder = "models--" + repo.replace("/", "--")
            if (cache_base / folder).exists():
                cached.append(mode)
            else:
                missing.append(mode)
        return {
            "modes_cached": cached,
            "modes_missing_offline": missing,
            "any_ready": bool(cached),
        }

    @staticmethod
    def _build_readiness_report_static() -> dict[str, Any]:
        """Собирает полный отчёт о готовности всех компонентов.

        Статический метод: вызывается и из IPC-сервиса, и из REST server
        без необходимости создавать полный инстанс BackendService.
        """
        stt = BackendService._probe_stt()
        diarization = BackendService._probe_diarization()
        translation = BackendService._probe_translation()
        return {
            "overall_ready": stt["ready"],
            "stt": stt,
            "diarization": diarization,
            "translation": translation,
        }

    def _handle_get_readiness(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает детальный отчёт о реальной готовности компонентов."""
        return self._build_readiness_report_static()

    def _handle_get_capabilities(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает матрицу доступных возможностей текущей сборки."""
        settings = self.store.load_settings()
        diarization_probe = BackendService._probe_diarization()
        translation_probe = BackendService._probe_translation()
        return {
            "stt": {
                "offline": True,
                "realtime_preview": True,
                "silence_guard_enabled": bool(settings.get("silence_guard_enabled", True)),
                "silence_guard_rms_threshold": float(settings.get("silence_guard_rms_threshold", 0.0020)),
                "silence_guard_peak_threshold": float(settings.get("silence_guard_peak_threshold", 0.0120)),
                "silence_guard_active_ratio_threshold": float(
                    settings.get("silence_guard_active_ratio_threshold", 0.015)
                ),
                "background_guard_enabled": bool(settings.get("background_guard_enabled", True)),
                "background_guard_min_peak": float(settings.get("background_guard_min_peak", 0.025)),
                "background_guard_min_rms": float(settings.get("background_guard_min_rms", 0.0040)),
                "background_guard_uniform_frame_threshold": float(
                    settings.get("background_guard_uniform_frame_threshold", 0.0060)
                ),
                "background_guard_max_uniform_active_ratio": float(
                    settings.get("background_guard_max_uniform_active_ratio", 0.92)
                ),
            },
            "translation": {
                "modes": ["off", "ru_to_es", "es_to_ru", "en_to_ru", "auto", "auto_to_ru", "bilingual_ru_es"],
                "styles": ["neutral", "chat", "formal"],
                "offline_default": True,
                "network_mode": str(settings.get("network_mode", "offline_default")),
                "modes_cached_locally": translation_probe["modes_cached"],
                "modes_missing_offline": translation_probe["modes_missing_offline"],
            },
            "summarization": {
                "available": True,
                "modes": ["summary_short", "summary_detailed"],
            },
            "hotkey": {
                "profiles": ["default", "meeting", "translation"],
                "current_profile": str(settings.get("hotkey_profile", "default")),
                "trigger": str(settings.get("hotkey", "right_option_toggle")),
            },
            "clipboard": {
                "modes": ["always_copy", "copy_on_fail", "never_copy"],
                "current_mode": str(settings.get("clipboard_mode", "always_copy")),
            },
            "audio_ducking": {
                "enabled": bool(settings.get("audio_ducking_enabled", True)),
                "percent": int(settings.get("audio_ducking_percent", 50)),
                "stop_tail_trim_ms": int(settings.get("stop_tail_trim_ms", 180)),
            },
            "overlay": {
                "opacity_percent": int(settings.get("overlay_opacity_percent", 45)),
            },
            "diarization": {
                "import_audio_beta": diarization_probe["ready"],
                "realtime": False,
                "has_hf_token": diarization_probe["has_hf_token"],
                "model_cached": diarization_probe["model_cached"],
            },
            "batch_import": {
                "drag_drop_queue": True,
                "preview_paths": True,
                "cancel_after_current": True,
            },
            "system_audio": {
                "capture_translation": True,
                "modes": ["mic", "system_audio", "mic_plus_system"],
                "current_mode": str(settings.get("capture_source_mode", "mic")),
                "status": "beta",
            },
            "call_assist": {
                "available": True,
                "default_notify": bool(settings.get("call_notify_default", True)),
                "default_auto_summary": bool(settings.get("call_auto_summary", True)),
                "voice_gateway_url": str(settings.get("voice_gateway_url", "")),
                "notify_modes": ["auto_on", "auto_off"],
                "tts_modes": ["local", "cloud", "hybrid"],
                "tools": [
                    "diagnostics",
                    "summary",
                    "quick_phrase",
                    "quick_phrase_library",
                    "cost_estimate",
                    "timeline",
                    "timeline_stats",
                    "timeline_summary",
                    "timeline_export",
                    "timeline_clear",
                    "timeline_to_history",
                ],
            },
            "ops": {
                "update_channel": str(settings.get("update_channel", "stable")),
                "channels": ["stable", "beta"],
            },
            "history": {
                "text_density": str(settings.get("history_text_density", "normal")),
                "density_modes": ["normal", "compact"],
                "overview": True,
            },
        }

    def _handle_set_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Добавляет/обновляет одну пару глоссария перевода."""
        source = str(params.get("source", "")).strip()
        target = str(params.get("target", "")).strip()
        if not source or not target:
            raise RuntimeError("source и target обязательны")
        settings = self.store.load_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary[source] = target
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        return {"updated": True, "count": len(saved.get("translation_glossary", {}))}

    def _handle_remove_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет одну пару из глоссария перевода."""
        source = str(params.get("source", "")).strip()
        if not source:
            raise RuntimeError("source обязателен")
        settings = self.store.load_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary.pop(source, None)
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        return {"removed": True, "count": len(saved.get("translation_glossary", {}))}

    def _handle_compact_history(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = self.store.compact_with_stats()
        return {"compacted": True, **stats}

    def _handle_import_history_ndjson(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует историю из внешнего NDJSON-файла."""
        raw_path = str(params.get("path", "")).strip()
        if not raw_path:
            raise RuntimeError("path обязателен")
        result = self.store.import_history_ndjson(Path(raw_path))
        return {
            "path": raw_path,
            "imported": int(result.get("imported", 0)),
            "skipped": int(result.get("skipped", 0)),
            "errors": int(result.get("errors", 0)),
        }

    def _handle_get_history_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает состояние журналов истории и оценку размера."""
        return self.store.get_history_stats()

    def _handle_get_history_overview(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает обзорный срез истории для панели управления."""
        return self.store.get_history_overview()

    def _handle_summarize_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Локальный lightweight-summary для длинных заметок/транскриптов."""
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("text обязателен")
        mode = str(params.get("mode", "summary_short")).strip() or "summary_short"
        max_points = int(params.get("max_points", 3) or 3)
        max_points = max(1, min(max_points, 12))
        summary = self._summarize_text_locally(text=text, mode=mode, max_points=max_points)
        return {
            "mode": summary["mode"],
            "summary": summary["summary"],
            "bullets": summary["bullets"],
            "source_chars": len(text),
        }

    @staticmethod
    def _summarize_text_locally(text: str, mode: str, max_points: int) -> dict[str, Any]:
        """Простая эвристика summary без внешних зависимостей."""
        normalized = " ".join(text.replace("\r", "\n").split())
        if not normalized:
            return {"mode": mode, "summary": "", "bullets": []}

        chunks = []
        for raw in re.split(r"(?<=[.!?])\s+", normalized):
            sentence = raw.strip()
            if sentence:
                chunks.append(sentence)
        if not chunks:
            chunks = [normalized]

        if mode == "summary_detailed":
            bullets = chunks[:max_points]
            summary = " ".join(chunks[: min(len(chunks), max_points + 1)])
        else:
            # Короткий summary: первая смысловая фраза + маркеры.
            head = chunks[0]
            bullets = chunks[1 : 1 + max_points]
            if not bullets:
                bullets = chunks[:max_points]
            summary = head
        return {"mode": mode, "summary": summary, "bullets": bullets}

    def _call_assist_loop(self, session_id: str, gateway_url: str, api_key: str) -> None:
        """Фоновый цикл: WS-подписка на VG + отправка аудио-снапшотов."""
        import asyncio
        import httpx
        import numpy as np
        from backend.vg_ws_client import VGWebSocketClient

        loop = asyncio.new_event_loop()
        client = VGWebSocketClient(gateway_url, session_id, api_key)

        async def _audio_send_loop() -> None:
            """Отправляет аудио-снапшоты в VG каждые 2 секунды."""
            mic_audio_url = f"{gateway_url.rstrip('/')}/v1/sessions/{session_id}/mic-audio"
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=10.0) as http:
                while not client._stop.is_set():
                    await asyncio.sleep(2.0)
                    if not self.recorder.is_recording:
                        continue
                    try:
                        audio_data, duration_sec = self.recorder.snapshot_audio(max_duration_sec=25.0)
                        current_size = getattr(audio_data, "size", 0)
                        if current_size < 16000:  # < 1 sec
                            continue
                        pcm_bytes = (audio_data * 32767).astype(np.int16).tobytes()
                        await http.post(mic_audio_url, content=pcm_bytes, headers=headers)
                    except Exception:
                        logger.exception("call_assist audio send error")

        async def _run() -> None:
            ws_task = asyncio.create_task(client.run())
            audio_task = asyncio.create_task(_audio_send_loop())
            try:
                while True:
                    await asyncio.sleep(0.5)
                    with self._call_assist_lock:
                        if not self._call_assist_state.get("active"):
                            break
            finally:
                client.stop()
                audio_task.cancel()
                try:
                    await audio_task
                except asyncio.CancelledError:
                    pass
                await ws_task

        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    def _request_voice_gateway_post(self, voice_gateway_url: str, api_key: str, path: str, payload: dict) -> dict:
        """POST helper для событий."""
        try:
            url = f"{voice_gateway_url.rstrip('/')}{path}"
            data = json.dumps(payload).encode("utf-8")
            logger.debug(f"POST {url} payload={json.dumps(payload)}")
            req = urllib_request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(req, timeout=2.0) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.error(f"POST {url} failed: {e}")
            return {"ok": False, "error": str(e)}

    def _handle_start_call_assist(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запускает сессию ассистента звонка с интеграцией Voice Gateway."""
        # 1. Запуск аудиозахвата (если еще не запущен)
        if not self.recorder.is_recording:
            started = self.recorder.start()
            if started:
                # Включаем превью для UI
                self._reset_preview_state()
                self._start_preview_worker(quality_profile="balanced")

        settings = self.store.load_settings()
        capture_source_mode = str(
            params.get("capture_source_mode") or settings.get("capture_source_mode", "mic")
        ).strip()
        if capture_source_mode not in {"mic", "system_audio", "mic_plus_system"}:
            capture_source_mode = "mic"

        translation_mode = str(
            params.get("translation_mode") or settings.get("translation_mode", "auto_to_ru")
        ).strip() or "auto_to_ru"
        tts_mode = str(params.get("tts_mode", "hybrid")).strip().lower() or "hybrid"
        if tts_mode not in {"local", "cloud", "hybrid"}:
            tts_mode = "hybrid"

        raw_notify_mode = str(params.get("notify_mode", "")).strip().lower()
        if raw_notify_mode in {"auto_on", "on", "true", "1"}:
            notify_mode = "auto_on"
        elif raw_notify_mode in {"auto_off", "off", "false", "0"}:
            notify_mode = "auto_off"
        else:
            notify_mode = "auto_on" if bool(settings.get("call_notify_default", True)) else "auto_off"
        raw_auto_summary = params.get("auto_summary")
        if raw_auto_summary is None:
            auto_summary = bool(settings.get("call_auto_summary", True))
        elif isinstance(raw_auto_summary, bool):
            auto_summary = raw_auto_summary
        else:
            auto_summary = str(raw_auto_summary).strip().lower() in {"1", "true", "on", "yes"}

        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        session_id = f"call_{uuid.uuid4().hex[:12]}"
        started_at = datetime.now().isoformat(timespec="seconds")

        gateway_session_id = None
        gateway_status = "disabled"
        gateway_error = ""
        if voice_gateway_url:
            gateway_result = self._request_voice_gateway_start(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                payload={
                    "translation_mode": translation_mode,
                    "notify_mode": notify_mode,
                    "tts_mode": tts_mode,
                    "source": capture_source_mode,
                    "meta": {
                        "started_by": "krabear_backend",
                        "auto_summary": auto_summary,
                        "session_id": session_id,
                    },
                },
            )
            if gateway_result.get("ok"):
                gateway_status = "ok"
                gateway_session_id = gateway_result.get("session_id")
            else:
                gateway_status = "degraded"
                gateway_error = str(gateway_result.get("error", "gateway_unreachable"))

        state = {
            "active": True,
            "status": "running",
            "session_id": session_id,
            "gateway_session_id": gateway_session_id,
            "gateway_status": gateway_status,
            "gateway_error": gateway_error,
            "capture_source_mode": capture_source_mode,
            "translation_mode": translation_mode,
            "notify_mode": notify_mode,
            "tts_mode": tts_mode,
            "auto_summary": auto_summary,
            "started_at": started_at,
        }
        with self._call_assist_lock:
            self._call_assist_state = state

        # Запускаем worker отправки обновлений
        if gateway_session_id and gateway_status == "ok":
            t = threading.Thread(
                target=self._call_assist_loop, 
                args=(gateway_session_id, voice_gateway_url, voice_gateway_api_key),
                daemon=True
            )
            t.start()

        return dict(state)

    def _handle_stop_call_assist(self, params: dict[str, Any]) -> dict[str, Any]:
        """Останавливает текущую сессию ассистента звонка."""
        stopped_at = datetime.now().isoformat(timespec="seconds")
        with self._call_assist_lock:
            active = bool(self._call_assist_state.get("active"))
            state = dict(self._call_assist_state)
            if not active:
                idle_state = {
                    "active": False,
                    "status": "idle",
                    "session_id": None,
                    "gateway_session_id": None,
                    "stopped_at": stopped_at,
                }
                self._call_assist_state = idle_state
                return idle_state

            self._call_assist_state["active"] = False
            self._call_assist_state["status"] = "stopped"
            self._call_assist_state["stopped_at"] = stopped_at
            state = dict(self._call_assist_state)

        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        if "auto_summary" in params:
            raw_auto_summary = params.get("auto_summary")
            if isinstance(raw_auto_summary, bool):
                auto_summary = raw_auto_summary
            else:
                auto_summary = str(raw_auto_summary).strip().lower() in {"1", "true", "on", "yes"}
        else:
            auto_summary = bool(settings.get("call_auto_summary", True))
        summary_max_items = int(params.get("summary_max_items", 40) or 40)
        summary_max_items = max(1, min(summary_max_items, 200))

        gateway_session_id = str(state.get("gateway_session_id") or "").strip()
        state["auto_summary"] = auto_summary
        state["summary_status"] = "skipped"
        if auto_summary and gateway_session_id and voice_gateway_url:
            summary_result = self._request_voice_gateway_post(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                path=f"/v1/sessions/{gateway_session_id}/summary",
                payload={"max_items": summary_max_items},
            )
            if summary_result.get("ok"):
                summary_payload = summary_result.get("payload", {})
                state["summary_status"] = "ok"
                state["summary"] = summary_payload
                summary_text = self._build_call_summary_history_text(
                    summary_payload=summary_payload,
                    session_id=str(state.get("session_id") or ""),
                )
                if summary_text:
                    history_item = self.store.add_history_item(
                        text=summary_text,
                        paste_status="failed",
                        source_text=str(summary_payload.get("summary", "")).strip(),
                        translated_text="",
                        translation_mode="off",
                        source_lang="",
                        target_lang="",
                        translation_status="not_requested",
                        translation_engine="call_assist_summary",
                    )
                    state["summary_history_id"] = history_item.id
            else:
                state["summary_status"] = "degraded"
                state["summary_error"] = str(summary_result.get("error", "unknown"))

        if gateway_session_id and voice_gateway_url:
            gateway_result = self._request_voice_gateway_stop(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                session_id=gateway_session_id,
            )
            state["gateway_stop_status"] = "ok" if gateway_result.get("ok") else "degraded"
            if not gateway_result.get("ok"):
                state["gateway_stop_error"] = str(gateway_result.get("error", "unknown"))
        elif gateway_session_id:
            state["gateway_stop_status"] = "degraded"
            state["gateway_stop_error"] = "voice_gateway_url_empty"
        else:
            state["gateway_stop_status"] = "skipped"

        with self._call_assist_lock:
            self._call_assist_state = dict(state)
        return state

    @staticmethod
    def _build_call_summary_history_text(summary_payload: dict[str, Any], session_id: str) -> str:
        """Строит человекочитаемый текст сводки звонка для сохранения в историю."""
        summary = str(summary_payload.get("summary", "")).strip()
        tasks_payload = summary_payload.get("tasks", [])
        tasks: list[str] = []
        if isinstance(tasks_payload, list):
            for raw_task in tasks_payload:
                if isinstance(raw_task, dict):
                    candidate = (
                        str(raw_task.get("task") or raw_task.get("title") or raw_task.get("text") or "").strip()
                    )
                else:
                    candidate = str(raw_task).strip()
                if candidate:
                    tasks.append(candidate)

        if not summary and not tasks:
            return ""

        lines: list[str] = ["[Call Summary]"]
        if session_id:
            lines.append(f"Сессия: {session_id}")
        if summary:
            lines.append("")
            lines.append("Кратко:")
            lines.append(summary)
        if tasks:
            lines.append("")
            lines.append("Задачи:")
            for idx, task in enumerate(tasks[:12], start=1):
                lines.append(f"{idx}. {task}")
        return "\n".join(lines).strip()

    def _handle_get_call_assist_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает состояние ассистента звонка."""
        with self._call_assist_lock:
            return dict(self._call_assist_state)

    def _handle_call_assist_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает diagnostics и explain-пакет почему перевод не появился."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
            active = bool(self._call_assist_state.get("active"))
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        diag_result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/diagnostics",
        )
        if not diag_result.get("ok"):
            raise RuntimeError(f"Gateway diagnostics error: {diag_result.get('error', 'unknown')}")

        include_why = bool(params.get("include_why", True))
        why_payload: dict[str, Any] = {}
        if include_why:
            why_result = self._request_voice_gateway_get(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                path=f"/v1/sessions/{gateway_session_id}/diagnostics/why",
            )
            if why_result.get("ok"):
                why_payload = why_result.get("payload", {})
            else:
                why_payload = {"ok": False, "error": why_result.get("error", "unknown")}

        return {
            "active": active,
            "gateway_session_id": gateway_session_id,
            "diagnostics": diag_result.get("payload", {}),
            "why": why_payload,
        }

    def _handle_call_assist_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запрашивает summary текущей звонковой сессии в Voice Gateway."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        max_items = int(params.get("max_items", 30) or 30)
        max_items = max(1, min(max_items, 200))
        summary_result = self._request_voice_gateway_post(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/summary",
            payload={"max_items": max_items},
        )
        if not summary_result.get("ok"):
            raise RuntimeError(f"Gateway summary error: {summary_result.get('error', 'unknown')}")
        return {
            "gateway_session_id": gateway_session_id,
            "summary": summary_result.get("payload", {}),
        }

    def _handle_call_assist_quick_phrase(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет быструю фразу на перевод/озвучку в Voice Gateway."""
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("text обязателен")

        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        source_lang = str(params.get("source_lang", "ru")).strip().lower() or "ru"
        target_lang = str(params.get("target_lang", "es")).strip().lower() or "es"
        voice = str(params.get("voice", "default")).strip() or "default"
        style = str(params.get("style", "chat")).strip() or "chat"

        quick_result = self._request_voice_gateway_post(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/quick-phrase",
            payload={
                "text": text,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "voice": voice,
                "style": style,
            },
        )
        if not quick_result.get("ok"):
            raise RuntimeError(f"Gateway quick-phrase error: {quick_result.get('error', 'unknown')}")
        return {
            "gateway_session_id": gateway_session_id,
            "quick_phrase": quick_result.get("payload", {}),
        }

    def _handle_list_call_assist_quick_phrases(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает библиотеку быстрых фраз из Voice Gateway."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()

        source_lang = str(params.get("source_lang", "ru")).strip().lower() or "ru"
        target_lang = str(params.get("target_lang", "es")).strip().lower() or "es"
        category = str(params.get("category", "all")).strip().lower() or "all"
        limit = int(params.get("limit", 30) or 30)
        limit = max(1, min(limit, 200))

        query = (
            f"/v1/quick-phrases?source_lang={source_lang}"
            f"&target_lang={target_lang}&category={category}&limit={limit}"
        )
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=query,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway quick-phrases error: {result.get('error', 'unknown')}")
        return result.get("payload", {})

    def _handle_list_call_assist_templates(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает локальные шаблоны быстрых реплик."""
        settings = self.store.load_settings()
        templates = self._normalize_templates(settings.get("call_quick_templates", []))
        return {"templates": templates}

    def _handle_add_call_assist_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет пользовательский шаблон фразы для повторного использования."""
        name = str(params.get("name", "")).strip()
        text = str(params.get("text", "")).strip()
        source_lang = str(params.get("source_lang", "ru")).strip().lower() or "ru"
        target_lang = str(params.get("target_lang", "ru")).strip().lower() or "ru"
        if not name or not text:
            raise RuntimeError("name и text обязательны для шаблона")
        settings = self.store.load_settings()
        templates = self._normalize_templates(settings.get("call_quick_templates", []))
        if any(t["name"].lower() == name.lower() for t in templates):
            raise RuntimeError("Шаблон с таким именем уже существует")
        templates.append(
            {
                "name": name,
                "text": text,
                "source_lang": source_lang,
                "target_lang": target_lang,
            }
        )
        settings["call_quick_templates"] = templates
        self.store.save_settings(settings)
        return {"templates": templates}

    def _handle_remove_call_assist_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет шаблон по имени."""
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("name обязателен")
        settings = self.store.load_settings()
        templates = self._normalize_templates(settings.get("call_quick_templates", []))
        filtered = [t for t in templates if t["name"].lower() != name.lower()]
        if len(filtered) == len(templates):
            raise RuntimeError("Шаблон не найден")
        settings["call_quick_templates"] = filtered
        self.store.save_settings(settings)
        return {"templates": filtered}

    def _handle_call_assist_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет быстрый шаблон в сессию через Gateway."""
        template_name = str(params.get("name", "")).strip()
        if not template_name:
            raise RuntimeError("name обязателен")
        templates = self._normalize_templates(self.store.load_settings().get("call_quick_templates", []))
        template = next((t for t in templates if t["name"].lower() == template_name.lower()), None)
        if template is None:
            raise RuntimeError("Шаблон не найден")
        payload = {
            "text": template["text"],
            "source_lang": template.get("source_lang", "ru"),
            "target_lang": template.get("target_lang", "ru"),
        }
        return self._handle_call_assist_quick_phrase(payload)

    def _normalize_templates(self, raw: Any) -> list[dict[str, str]]:
        """Отрезает шаблоны до необходимых полей и удаляет пустые."""
        normalized: list[dict[str, str]] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                text = str(item.get("text", "")).strip()
                if not name or not text:
                    continue
                normalized.append(
                    {
                        "name": name,
                        "text": text,
                        "source_lang": str(item.get("source_lang", "ru")).strip().lower() or "ru",
                        "target_lang": str(item.get("target_lang", "ru")).strip().lower() or "ru",
                    }
                )
        return normalized

    def _handle_call_assist_cost_estimate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Считает оценку telephony+AI стоимости через endpoint Voice Gateway."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()

        country = str(params.get("country", "ES")).strip().upper() or "ES"
        if len(country) != 2:
            country = "ES"

        minutes_inbound = max(0.0, float(params.get("minutes_inbound", 200) or 200))
        minutes_out_landline = max(0.0, float(params.get("minutes_outbound_landline", 100) or 100))
        minutes_out_mobile = max(0.0, float(params.get("minutes_outbound_mobile", 100) or 100))
        minutes_media_stream = max(0.0, float(params.get("minutes_media_stream", 400) or 400))
        media_stream_rate = max(0.0, float(params.get("media_stream_rate", 0.004) or 0.004))
        use_live_pricing = self._coerce_bool(params.get("use_live_pricing", True), default=True)

        inbound_rate_override = max(0.0, float(params.get("inbound_rate_override", 0.0) or 0.0))
        outbound_landline_rate_override = max(0.0, float(params.get("outbound_landline_rate_override", 0.0) or 0.0))
        outbound_mobile_rate_override = max(0.0, float(params.get("outbound_mobile_rate_override", 0.0) or 0.0))

        stt_cost_per_minute = max(0.0, float(params.get("stt_cost_per_minute", 0.006) or 0.006))
        translation_cost_per_1k_chars = max(0.0, float(params.get("translation_cost_per_1k_chars", 0.0007) or 0.0007))
        tts_cost_per_1k_chars = max(0.0, float(params.get("tts_cost_per_1k_chars", 0.015) or 0.015))
        chars_per_minute = max(1, int(float(params.get("chars_per_minute", 850) or 850)))
        duplex_factor = max(1.0, float(params.get("duplex_factor", 1.6) or 1.6))
        tts_char_factor = max(0.0, float(params.get("tts_char_factor", 0.9) or 0.9))

        query = (
            f"/v1/telephony/cost/estimate?"
            f"country={urllib_parse.quote(country, safe='')}"
            f"&minutes_inbound={minutes_inbound}"
            f"&minutes_outbound_landline={minutes_out_landline}"
            f"&minutes_outbound_mobile={minutes_out_mobile}"
            f"&minutes_media_stream={minutes_media_stream}"
            f"&media_stream_rate={media_stream_rate}"
            f"&use_live_pricing={'true' if use_live_pricing else 'false'}"
            f"&inbound_rate_override={inbound_rate_override}"
            f"&outbound_landline_rate_override={outbound_landline_rate_override}"
            f"&outbound_mobile_rate_override={outbound_mobile_rate_override}"
            f"&stt_cost_per_minute={stt_cost_per_minute}"
            f"&translation_cost_per_1k_chars={translation_cost_per_1k_chars}"
            f"&tts_cost_per_1k_chars={tts_cost_per_1k_chars}"
            f"&chars_per_minute={chars_per_minute}"
            f"&duplex_factor={duplex_factor}"
            f"&tts_char_factor={tts_char_factor}"
        )
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=query,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway cost estimate error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "country": country}

    def _handle_call_assist_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает timeline текущей звонковой сессии из Voice Gateway."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для timeline")

        limit = int(params.get("limit", 80) or 80)
        limit = max(1, min(limit, 500))
        kind = str(params.get("kind", "")).strip()
        contains = str(params.get("contains", "")).strip()
        query_parts = [f"limit={limit}"]
        if kind:
            query_parts.append(f"kind={urllib_parse.quote(kind, safe='')}")
        if contains:
            query_parts.append(f"contains={urllib_parse.quote(contains, safe='')}")
        path = f"/v1/sessions/{gateway_session_id}/timeline?{'&'.join(query_parts)}"
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "items": [], "count": 0}

    def _handle_call_assist_cost_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Считает usage-показатели и вызывает Gateway cost estimate."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id")

        country = str(params.get("country", "ES")).strip().upper()
        if len(country) != 2:
            country = "ES"
        use_live_pricing = self._coerce_bool(params.get("use_live_pricing", True), default=True)
        budget_limit = float(params.get("budget_limit") or settings.get("call_budget_usd", 2.0))
        limit = int(params.get("stats_limit", 400) or 400)
        limit = max(100, min(limit, 2000))

        stats_result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/timeline/stats?limit={limit}",
        )
        if not stats_result.get("ok"):
            raise RuntimeError(f"Gateway timeline stats error: {stats_result.get('error', 'unknown')}")
        stats_payload = stats_result.get("payload", {})
        stats = stats_payload.get("stats", {}) if isinstance(stats_payload.get("stats"), dict) else stats_payload
        text_chars = int(stats.get("text_chars", 0))
        minutes = max(0.5, text_chars / 850.0)

        query_parts = [
            f"country={urllib_parse.quote(country)}",
            f"minutes_inbound={minutes:.3f}",
            "minutes_outbound_landline=0",
            "minutes_outbound_mobile=0",
            f"minutes_media_stream={minutes:.3f}",
            f"use_live_pricing={'true' if use_live_pricing else 'false'}",
        ]
        cost_result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/telephony/cost/estimate?{'&'.join(query_parts)}",
        )
        if not cost_result.get("ok"):
            raise RuntimeError(f"Gateway cost estimate error: {cost_result.get('error', 'unknown')}")

        payload = cost_result.get("payload", {}) if isinstance(cost_result.get("payload"), dict) else {}
        total = float(payload.get("total_usd", 0.0))
        over_budget = total > budget_limit
        return {
            "minutes_estimate": round(minutes, 3),
            "text_chars": text_chars,
            "telephony_usd": payload.get("telephony_usd", {}),
            "ai_usd": payload.get("ai_usd", {}),
            "total_usd": total,
            "budget_limit": budget_limit,
            "over_budget": over_budget,
            "country": payload.get("country", country),
            "rates_source": payload.get("rates_source", "unknown"),
        }

    def _handle_call_assist_timeline_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает агрегаты timeline текущей звонковой сессии."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для timeline stats")

        limit = int(params.get("limit", 1000) or 1000)
        limit = max(1, min(limit, 5000))
        path = f"/v1/sessions/{gateway_session_id}/timeline/stats?limit={limit}"
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline stats error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "stats": {"count": 0}}

    def _handle_call_assist_timeline_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает краткую сводку timeline текущей звонковой сессии."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для timeline summary")

        limit = int(params.get("limit", 400) or 400)
        limit = max(1, min(limit, 5000))
        max_tasks = int(params.get("max_tasks", 8) or 8)
        max_tasks = max(1, min(max_tasks, 20))
        path = f"/v1/sessions/{gateway_session_id}/timeline/summary?limit={limit}&max_tasks={max_tasks}"
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline summary error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "summary": "", "tasks": []}

    def _handle_call_assist_timeline_export(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует timeline текущей звонковой сессии (md/ndjson)."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для timeline export")

        export_format = str(params.get("format", "md")).strip().lower() or "md"
        if export_format not in {"md", "ndjson"}:
            export_format = "md"
        limit = int(params.get("limit", 200) or 200)
        limit = max(1, min(limit, 1000))
        path = f"/v1/sessions/{gateway_session_id}/timeline/export?format={export_format}&limit={limit}"
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline export error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "format": export_format, "content": ""}

    def _handle_call_assist_timeline_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        """Очищает timeline текущей звонковой сессии с опциональным keep_last."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для очистки timeline")

        keep_last = int(params.get("keep_last", 0) or 0)
        keep_last = max(0, min(keep_last, 200))
        path = f"/v1/sessions/{gateway_session_id}/timeline?keep_last={keep_last}"
        result = self._request_voice_gateway_delete(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline clear error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "keep_last": keep_last}

    def _handle_call_assist_timeline_to_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет экспорт timeline в историю Krab Ear как отдельную запись."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._call_assist_lock:
            gateway_session_id = str(self._call_assist_state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway_session_id для сохранения timeline")

        export_format = str(params.get("format", "md")).strip().lower() or "md"
        if export_format not in {"md", "ndjson"}:
            export_format = "md"
        limit = int(params.get("limit", 400) or 400)
        limit = max(1, min(limit, 2000))
        include_summary = self._coerce_bool(params.get("include_summary", True), default=True)
        include_stats = self._coerce_bool(params.get("include_stats", True), default=True)
        max_tasks = int(params.get("max_tasks", 8) or 8)
        max_tasks = max(1, min(max_tasks, 20))

        summary_payload: dict[str, Any] = {}
        stats_payload: dict[str, Any] = {}
        if include_summary:
            summary_result = self._request_voice_gateway_get(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                path=f"/v1/sessions/{gateway_session_id}/timeline/summary?limit={limit}&max_tasks={max_tasks}",
            )
            if summary_result.get("ok"):
                raw_summary = summary_result.get("payload", {})
                if isinstance(raw_summary, dict):
                    summary_payload = raw_summary
        if include_stats:
            stats_result = self._request_voice_gateway_get(
                voice_gateway_url=voice_gateway_url,
                api_key=voice_gateway_api_key,
                path=f"/v1/sessions/{gateway_session_id}/timeline/stats?limit={limit}",
            )
            if stats_result.get("ok"):
                raw_stats = stats_result.get("payload", {})
                if isinstance(raw_stats, dict):
                    stats_payload = raw_stats

        path = f"/v1/sessions/{gateway_session_id}/timeline/export?format={export_format}&limit={limit}"
        result = self._request_voice_gateway_get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=path,
        )
        if not result.get("ok"):
            raise RuntimeError(f"Gateway timeline export error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        content = str(payload.get("content", "")).strip()
        if not content:
            raise RuntimeError("Timeline пуст, нечего сохранять в историю")

        session_tag = str(params.get("session_tag", "")).strip()
        if not session_tag:
            session_tag = gateway_session_id
        sections: list[str] = [f"[Call Timeline Export] session={session_tag} format={export_format}"]

        if summary_payload:
            summary_text = str(summary_payload.get("summary", "")).strip()
            tasks_raw = summary_payload.get("tasks", [])
            tasks: list[str] = []
            if isinstance(tasks_raw, list):
                for raw_task in tasks_raw:
                    task = str(raw_task).strip()
                    if task:
                        tasks.append(task)
            lines = ["## Summary", summary_text or "-"]
            if tasks:
                lines.append("## Tasks")
                for idx, task in enumerate(tasks[: max_tasks], start=1):
                    lines.append(f"{idx}. {task}")
            sections.append("\n".join(lines))

        if stats_payload:
            stats = stats_payload.get("stats", {})
            if isinstance(stats, dict):
                lines = [
                    "## Timeline Stats",
                    f"count: {stats.get('count', 0)}",
                    f"text_chars: {stats.get('text_chars', 0)}",
                    f"first_ts: {stats.get('first_ts', '-')}",
                    f"last_ts: {stats.get('last_ts', '-')}",
                ]
                by_kind = stats.get("by_kind", {})
                if isinstance(by_kind, dict) and by_kind:
                    lines.append("by_kind:")
                    for key in sorted(by_kind.keys()):
                        lines.append(f"- {key}: {by_kind[key]}")
                sections.append("\n".join(lines))

        sections.append(content)
        text = "\n\n".join(sections)
        history_item = self.store.add_history_item(
            text=text,
            paste_status="failed",
            source_text=content[:4000],
            translated_text="",
            translation_mode="off",
            source_lang="",
            target_lang="",
            translation_status="not_requested",
            translation_engine="call_assist_timeline",
        )
        return {
            "ok": True,
            "gateway_session_id": gateway_session_id,
            "format": export_format,
            "history_id": history_item.id,
            "chars": len(content),
            "summary_included": bool(summary_payload),
            "stats_included": bool(stats_payload),
        }

    def _handle_list_audio_inputs(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных входных аудиоустройств."""
        items = self._list_audio_inputs()
        default_input_id = None
        for item in items:
            if item.get("is_default"):
                default_input_id = item.get("id")
                break
        return {
            "items": items,
            "count": len(items),
            "default_input_id": default_input_id,
        }

    def _handle_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        settings = self.store.load_settings()
        quality_profile = str(params.get("quality_profile") or settings.get("quality_profile", "balanced"))
        cleanup_profile = str(params.get("cleanup_profile") or settings.get("cleanup_profile", "soft"))
        lang_hint: str | None = params.get("lang_hint") or None
        translation_mode = str(params.get("translation_mode") or settings.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style") or settings.get("translation_style", "neutral"))
        translation_glossary = settings.get("translation_glossary", {})
        translate_and_paste = bool(
            params.get("translate_and_paste")
            if "translate_and_paste" in params
            else settings.get("translate_and_paste", False)
        )
        network_mode = str(settings.get("network_mode", "offline_default"))

        selected = [str(item).strip() for item in raw_paths if str(item).strip()]
        audio_paths = self._collect_audio_paths(selected)
        if not audio_paths:
            return {"items": [], "processed": 0, "errors": ["Не найдено аудиофайлов для транскрибации"]}

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for audio_path in audio_paths:
            started_at = time.monotonic()
            try:
                transcribe_payload = self.transcriber.transcribe(
                    audio_path,
                    quality_profile=quality_profile,
                    cleanup_profile=cleanup_profile,
                    lang_hint=lang_hint,
                )
                text = self._extract_transcribed_text(transcribe_payload)
                elapsed = round(time.monotonic() - started_at, 3)
                if not text:
                    err = self._extract_transcribed_error(transcribe_payload)
                    if err:
                        errors.append(f"{audio_path}: {err}")
                    else:
                        errors.append(f"{audio_path}: пустой результат")
                    continue
                translation = self.translator.translate(
                    text=text,
                    mode=translation_mode,
                    network_mode=network_mode,
                    translation_style=translation_style,
                    glossary=translation_glossary,
                )
                translated_text = translation.text.strip() if translation.ok else ""
                final_text = translated_text if (translate_and_paste and translated_text) else text

                history_item = self.store.add_history_item(
                    text=final_text,
                    paste_status="failed",
                    source_text=text,
                    translated_text=translated_text,
                    translation_mode=translation.mode,
                    source_lang=translation.source_lang,
                    target_lang=translation.target_lang,
                    translation_status=translation.status,
                    translation_engine=translation.engine,
                )
                items.append(
                    {
                        "path": audio_path,
                        "text": final_text,
                        "original_text": text,
                        "translated_text": translated_text,
                        "translation_mode": translation.mode,
                        "translation_style": translation_style,
                        "translation_status": translation.status,
                        "source_lang": translation.source_lang,
                        "target_lang": translation.target_lang,
                        "history_id": history_item.id,
                        "duration_sec": elapsed,
                    }
                )
            except Exception as exc:
                errors.append(f"{audio_path}: {exc}")

        return {
            "items": items,
            "processed": len(items),
            "errors": errors,
        }

    def _handle_preview_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Быстрый предпросмотр импорта: считает аудиофайлы без транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        selected = [str(item).strip() for item in raw_paths if str(item).strip()]
        audio_paths = self._collect_audio_paths(selected)
        sample_limit = int(params.get("sample_limit", 5) or 5)
        safe_sample_limit = max(1, min(sample_limit, 50))
        by_ext: dict[str, int] = {}
        total_bytes = 0
        for audio_path in audio_paths:
            suffix = Path(audio_path).suffix.lower() or "<none>"
            by_ext[suffix] = by_ext.get(suffix, 0) + 1
            try:
                total_bytes += Path(audio_path).stat().st_size
            except FileNotFoundError:
                continue
        return {
            "input_count": len(selected),
            "audio_count": len(audio_paths),
            "sample": audio_paths[:safe_sample_limit],
            "by_ext": by_ext,
            "total_bytes": total_bytes,
        }

    @staticmethod
    def _collect_audio_paths(paths: list[str]) -> list[str]:
        audio_ext = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".mp4", ".m4b", ".aif", ".aiff"}
        result: list[str] = []

        for raw in paths:
            path = Path(raw).expanduser()
            if not path.exists():
                continue

            if path.is_file():
                if path.suffix.lower() in audio_ext:
                    result.append(str(path.resolve()))
                continue

            if path.is_dir():
                for candidate in path.rglob("*"):
                    if candidate.is_file() and candidate.suffix.lower() in audio_ext:
                        result.append(str(candidate.resolve()))

        # Убираем дубликаты, сохраняем порядок.
        unique: list[str] = []
        seen: set[str] = set()
        for item in result:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    def _start_preview_worker(self, quality_profile: str) -> None:
        self._stop_preview_worker()
        self._preview_stop_event.clear()
        self._preview_thread = threading.Thread(
            target=self._preview_loop,
            args=(quality_profile,),
            daemon=True,
        )
        self._preview_thread.start()

    def _stop_preview_worker(self) -> None:
        self._preview_stop_event.set()
        if self._preview_thread and self._preview_thread.is_alive():
            self._preview_thread.join(timeout=1.5)
        self._preview_thread = None

    def _reset_preview_state(self) -> None:
        with self._preview_lock:
            self._preview_text = ""
            self._preview_duration_sec = 0.0
            self._preview_updated_at = 0.0

    def _preview_loop(self, quality_profile: str) -> None:
        snapshot_audio = getattr(self.recorder, "snapshot_audio", None)
        min_samples = int(getattr(self.recorder, "sample_rate", 16000) * 0.8)
        last_refresh_duration = 0.0

        while not self._preview_stop_event.is_set():
            if not bool(getattr(self.recorder, "is_recording", False)):
                break

            if not callable(snapshot_audio):
                self._preview_stop_event.wait(0.7)
                continue

            try:
                audio_data, duration_sec = snapshot_audio(max_duration_sec=12.0)
            except Exception:
                logger.exception("Realtime preview: ошибка snapshot_audio")
                self._preview_stop_event.wait(0.8)
                continue

            with self._preview_lock:
                self._preview_duration_sec = float(duration_sec)

            current_size = int(getattr(audio_data, "size", 0))
            if current_size < min_samples:
                self._preview_stop_event.wait(0.8)
                continue
            # Важный нюанс: после достижения лимита snapshot-а размер буфера стабилизируется.
            # Поэтому ориентируемся на прогресс времени записи, а не на size.
            if duration_sec - last_refresh_duration < 0.9:
                self._preview_stop_event.wait(0.35)
                continue

            try:
                preview_payload = self.transcriber.transcribe_preview(
                    audio_data,
                    quality_profile=quality_profile,
                )
                preview_text = self._extract_transcribed_text(preview_payload)
                preview_text = self._postprocess_preview_text(preview_text)
            except Exception:
                logger.exception("Realtime preview: ошибка transcribe_preview")
                self._preview_stop_event.wait(0.8)
                continue

            if preview_text:
                with self._preview_lock:
                    self._preview_text = preview_text[-900:]
                    self._preview_updated_at = float(duration_sec)
            else:
                # Не держим stale preview: если актуальный срез пустой/артефактный,
                # очищаем текст, чтобы fallback не подхватил мусор.
                with self._preview_lock:
                    self._preview_text = ""
                    self._preview_updated_at = float(duration_sec)
            last_refresh_duration = float(duration_sec)
            self._preview_stop_event.wait(0.8)

    @staticmethod
    def _list_audio_inputs() -> list[dict[str, Any]]:
        """Пытается безопасно получить список входных аудиоустройств."""
        try:
            import sounddevice as sd  # type: ignore
        except Exception:
            return []

        try:
            devices = sd.query_devices()
        except Exception:
            logger.exception("Не удалось получить список аудиоустройств")
            return []

        hostapis: list[str] = []
        try:
            hostapi_payload = sd.query_hostapis()
            hostapis = [str(item.get("name", "")) for item in hostapi_payload]
        except Exception:
            hostapis = []

        default_input_idx = None
        try:
            default_device = sd.default.device
            if isinstance(default_device, (list, tuple)) and default_device:
                default_input_idx = int(default_device[0])
        except Exception:
            default_input_idx = None

        results: list[dict[str, Any]] = []
        for index, device in enumerate(devices):
            try:
                max_input_channels = int(device.get("max_input_channels", 0))
            except Exception:
                max_input_channels = 0
            if max_input_channels <= 0:
                continue
            hostapi_index = int(device.get("hostapi", -1))
            hostapi_name = hostapis[hostapi_index] if 0 <= hostapi_index < len(hostapis) else ""
            name = str(device.get("name", f"Input {index}")).strip()
            lowered = name.lower()
            tags: list[str] = []
            if "blackhole" in lowered:
                tags.append("loopback")
            if "shure" in lowered or "mic" in lowered or "microphone" in lowered:
                tags.append("mic")
            if "loopback" in lowered and "loopback" not in tags:
                tags.append("loopback")
            results.append(
                {
                    "id": index,
                    "name": name,
                    "hostapi": hostapi_name,
                    "max_input_channels": max_input_channels,
                    "default_samplerate": int(float(device.get("default_samplerate", 0) or 0)),
                    "is_default": bool(default_input_idx == index),
                    "tags": tags,
                }
            )
        return results

    @staticmethod
    def _request_voice_gateway_start(
        voice_gateway_url: str,
        api_key: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Создаёт сессию в Voice Gateway и возвращает идентификатор."""
        try:
            url = f"{voice_gateway_url.rstrip('/')}/v1/sessions"
            body = json.dumps(payload).encode("utf-8")
            request = urllib_request.Request(url=url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(request, timeout=3.5) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                session_id = str(payload.get("id", "")).strip()
                return {"ok": bool(session_id), "session_id": session_id, "payload": payload}
        except urllib_error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)
            return {"ok": False, "error": f"http_{exc.code}:{details}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _request_voice_gateway_get(
        voice_gateway_url: str,
        api_key: str,
        path: str,
    ) -> dict[str, Any]:
        """GET к Voice Gateway с безопасным JSON-парсингом."""
        try:
            if path.startswith("http://") or path.startswith("https://"):
                url = path
            else:
                if not path.startswith("/"):
                    path = f"/{path}"
                url = f"{voice_gateway_url.rstrip('/')}{path}"
            request = urllib_request.Request(url=url, method="GET")
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(request, timeout=4.0) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return {"ok": True, "payload": payload}
        except urllib_error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)
            return {"ok": False, "error": f"http_{exc.code}:{details}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _request_voice_gateway_post(
        voice_gateway_url: str,
        api_key: str,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """POST к Voice Gateway с безопасным JSON-парсингом."""
        try:
            if not path.startswith("/"):
                path = f"/{path}"
            url = f"{voice_gateway_url.rstrip('/')}{path}"
            body = json.dumps(payload).encode("utf-8")
            request = urllib_request.Request(url=url, data=body, method="POST")
            request.add_header("Content-Type", "application/json")
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(request, timeout=4.0) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return {"ok": True, "payload": payload}
        except urllib_error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)
            return {"ok": False, "error": f"http_{exc.code}:{details}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _request_voice_gateway_delete(
        voice_gateway_url: str,
        api_key: str,
        path: str,
    ) -> dict[str, Any]:
        """DELETE к Voice Gateway с безопасным JSON-парсингом."""
        try:
            if not path.startswith("/"):
                path = f"/{path}"
            url = f"{voice_gateway_url.rstrip('/')}{path}"
            request = urllib_request.Request(url=url, method="DELETE")
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(request, timeout=4.0) as response:
                raw = response.read().decode("utf-8")
                payload = json.loads(raw) if raw else {}
                return {"ok": True, "payload": payload}
        except urllib_error.HTTPError as exc:
            try:
                details = exc.read().decode("utf-8")
            except Exception:
                details = str(exc)
            return {"ok": False, "error": f"http_{exc.code}:{details}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _request_voice_gateway_stop(
        voice_gateway_url: str,
        api_key: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Удаляет сессию в Voice Gateway."""
        try:
            url = f"{voice_gateway_url.rstrip('/')}/v1/sessions/{session_id}"
            request = urllib_request.Request(url=url, method="DELETE")
            if api_key:
                request.add_header("Authorization", f"Bearer {api_key}")
            with urllib_request.urlopen(request, timeout=3.5):
                return {"ok": True}
        except urllib_error.HTTPError as exc:
            return {"ok": False, "error": f"http_{exc.code}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _error(request_id: Any, code: str, message: str) -> dict[str, Any]:
        return {
            "id": request_id,
            "ok": False,
            "error": {"code": code, "message": message},
        }

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
    def _coerce_bounded_int(value: Any, default: int, min_value: int, max_value: int) -> int:
        """Нормализует целое значение в допустимый диапазон."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = int(default)
        return max(min_value, min(parsed, max_value))

    @staticmethod
    def _coerce_bounded_float(value: Any, default: float, min_value: float, max_value: float) -> float:
        """Нормализует float-значение в допустимый диапазон."""
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float(default)
        return max(min_value, min(parsed, max_value))

    def _stop_recorder_guarded(self, stop_tail_trim_ms: int) -> tuple[Any, float] | None:
        """
        Останавливает рекордер с поддержкой старых сигнатур stop().

        Нужен для совместимости фейков/старых реализаций, где метод `stop`
        ещё не принимает `trim_tail_ms`.
        """
        stop_callable = getattr(self.recorder, "stop", None)
        if not callable(stop_callable):
            raise RuntimeError("Рекордер не поддерживает stop()")
        try:
            return stop_callable(trim_tail_ms=stop_tail_trim_ms)
        except TypeError:
            return stop_callable()

    @staticmethod
    def _looks_like_silence_audio(
        audio: Any,
        sample_rate: int,
        rms_threshold: float,
        peak_threshold: float,
        active_ratio_threshold: float,
    ) -> bool:
        """
        Эвристически определяет, есть ли в буфере реальная речь.

        Логика:
        - очень низкие peak/rms -> считаем тишиной;
        - иначе считаем долю «активных» 20мс фреймов и отсекаем фоновой шум.
        """
        try:
            data = np.asarray(audio, dtype=np.float32).reshape(-1)
        except Exception:
            return False
        if data.size == 0:
            return True

        abs_data = np.abs(data)
        peak = float(abs_data.max(initial=0.0))
        rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64)))
        if peak <= peak_threshold and rms <= rms_threshold:
            return True

        frame_size = max(1, int(sample_rate * 0.02))  # 20мс
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return peak <= (peak_threshold * 1.2) and rms <= (rms_threshold * 1.4)

        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        activity_threshold = max(rms_threshold * 2.0, 0.0035)
        active_ratio = float(np.mean(frame_rms >= activity_threshold))

        return active_ratio < active_ratio_threshold and peak <= (peak_threshold * 1.5)

    @staticmethod
    def _looks_like_distant_background_speech(
        audio: Any,
        sample_rate: int,
        min_peak: float,
        min_rms: float,
        uniform_frame_threshold: float,
        max_uniform_active_ratio: float,
    ) -> bool:
        """
        Эвристика "дальняя фоновая речь", чтобы не коммитить ТВ/видео вместо диктовки.

        Идея:
        - если уровень слишком низкий (нет близкой речи);
        - и при этом энергия распределена почти равномерно без естественных пауз,
          что характерно для далёкого источника/фона.
        """
        try:
            data = np.asarray(audio, dtype=np.float32).reshape(-1)
        except Exception:
            return False
        if data.size == 0:
            return False

        abs_data = np.abs(data)
        peak = float(abs_data.max(initial=0.0))
        rms = float(np.sqrt(np.mean(np.square(data), dtype=np.float64)))
        low_level = peak < min_peak and rms < min_rms

        frame_size = max(1, int(sample_rate * 0.02))  # 20мс
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return low_level

        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        mean_rms = float(np.mean(frame_rms))
        std_rms = float(np.std(frame_rms))
        variation_coeff = std_rms / max(mean_rms, 1e-8)
        duration_sec = float(data.size) / max(float(sample_rate), 1.0)

        # Для тихих сигналов опускаем порог активности, иначе равномерный фон
        # может казаться "неактивным" и проскальзывать мимо фильтра.
        dynamic_uniform_threshold = max(0.0012, min(uniform_frame_threshold, max(min_rms * 0.35, 0.0012)))
        active_ratio = float(np.mean(frame_rms >= dynamic_uniform_threshold))

        # Равномерный плотный поток без естественных пауз считаем фоном даже при чуть
        # более высоком уровне: это типичный паттерн "ролик на фоне".
        background_pattern = active_ratio >= max_uniform_active_ratio and variation_coeff < 0.35
        very_uniform = active_ratio >= 0.96 and variation_coeff < 0.18
        return background_pattern and (low_level or (very_uniform and duration_sec >= 4.0))

    @staticmethod
    def _is_known_prompt_echo(normalized_text: str) -> bool:
        """
        Отлавливает типовые фразы-артефакты, которые не должны попадать в финальный текст.

        Проверяем как точные совпадения, так и вхождения фрагментов: в реальности
        артефакт часто приходит с обрывами или повтором одной и той же инструкции.
        """
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return True

        blocked_fragments = (
            "продолжение следует",
            "to be continued",
            "сохраняй смысл ставь корректную пунктуац",
            "сохраняй смысл ставь корректную пункту",
            "ставь корректную пунктуац",
            "ставь корректную пункту",
        )
        if any(fragment in normalized for fragment in blocked_fragments):
            return True

        words = normalized.split()
        compact = " ".join(words)
        if (
            "сохраняй" in words
            and "смысл" in words
            and any(token.startswith("корр") for token in words)
            and any(token.startswith("пункт") for token in words)
        ):
            return True

        return bool(re.search(r"сохраняй\s+смысл.*корр\w*.*пункт\w*", compact))

    @staticmethod
    def _contains_repeated_chunk(words: list[str], min_repeats: int = 3) -> bool:
        """
        Ищет подряд повторяющиеся куски фразы (типичный зацикленный артефакт модели).
        """
        total = len(words)
        if total < 6:
            return False

        max_chunk = min(7, total // min_repeats)
        for chunk_size in range(2, max_chunk + 1):
            start = 0
            while start + (chunk_size * min_repeats) <= total:
                chunk = words[start : start + chunk_size]
                repeats = 1
                while start + (chunk_size * (repeats + 1)) <= total:
                    next_chunk = words[
                        start + (chunk_size * repeats) : start + (chunk_size * (repeats + 1))
                    ]
                    if next_chunk != chunk:
                        break
                    repeats += 1
                if repeats >= min_repeats:
                    return True
                start += 1
        return False

    @staticmethod
    def _looks_like_looping_artifact(words: list[str], min_words: int, min_bigram_hits: int) -> bool:
        """
        Детектирует «петли» и низкоинформативные повторы в транскрибе.
        """
        if len(words) < min_words:
            return False

        counts: dict[str, int] = {}
        for token in words:
            counts[token] = counts.get(token, 0) + 1

        unique_ratio = len(counts) / max(1, len(words))
        max_freq = max(counts.values()) if counts else 0
        if unique_ratio <= 0.42 and max_freq >= max(3, int(len(words) * 0.34)):
            return True

        if len(counts) <= 2 and len(words) >= 5 and max_freq >= 4:
            return True

        bigram_counts: dict[tuple[str, str], int] = {}
        for idx in range(len(words) - 1):
            key = (words[idx], words[idx + 1])
            bigram_counts[key] = bigram_counts.get(key, 0) + 1
        top_bigram_freq = max(bigram_counts.values()) if bigram_counts else 0
        if top_bigram_freq >= max(min_bigram_hits, len(words) // 5):
            return True

        return BackendService._contains_repeated_chunk(words)

    @staticmethod
    def _postprocess_transcribed_text(text: str) -> str:
        """
        Дополнительная фильтрация и базовая нормализация пунктуации.

        Цель: уменьшить артефакты на пустом/шумовом вводе и чуть улучшить читаемость.
        """
        clean = str(text or "").strip()
        if not clean:
            return ""

        lowered = clean.lower()
        # Явные тех-артефакты инструментального вывода.
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            return ""

        normalized = TextUtils.normalize_phrase(clean)
        if BackendService._is_known_prompt_echo(normalized):
            return ""

        collapsed_duplicate = BackendService._collapse_immediate_duplicate_phrase(normalized)
        if collapsed_duplicate:
            clean = collapsed_duplicate
            normalized = TextUtils.normalize_phrase(clean)

        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if BackendService._looks_like_looping_artifact(words, min_words=8, min_bigram_hits=4):
            return ""

        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()

        first_alpha_idx = next((idx for idx, char in enumerate(clean) if char.isalpha()), -1)
        if first_alpha_idx >= 0:
            clean = clean[:first_alpha_idx] + clean[first_alpha_idx].upper() + clean[first_alpha_idx + 1 :]

        if not re.search(r"[.!?…]$", clean):
            if len(words) >= 4:
                clean = f"{clean}."

        return clean.strip()

    @staticmethod
    def _collapse_immediate_duplicate_phrase(normalized_text: str) -> str:
        """
        Схлопывает паттерн «одна и та же фраза подряд два раза».

        Пример:
        «ну он просто два раза теперь пишет ну он просто два раза теперь пишет»
        -> «Ну он просто два раза теперь пишет.»
        """
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return ""

        words = normalized.split()
        total = len(words)
        if total < 8:
            return ""

        # Базовый сценарий: точное дублирование 1-в-1.
        if total % 2 == 0:
            half = total // 2
            if words[:half] == words[half:]:
                collapsed = " ".join(words[:half]).strip()
                if not collapsed:
                    return ""
                return f"{collapsed[0].upper()}{collapsed[1:]}."

        # Допуск ±1 токен на хвосте (из-за пунктуации/обрезки).
        for shift in (-1, 1):
            left = total // 2
            right = total - left
            if abs(left - right) != 1:
                continue
            if shift < 0 and left > right:
                if words[:right] == words[left:]:
                    collapsed = " ".join(words[:right]).strip()
                    if collapsed:
                        return f"{collapsed[0].upper()}{collapsed[1:]}."
            if shift > 0 and right > left:
                if words[:left] == words[right:]:
                    collapsed = " ".join(words[:left]).strip()
                    if collapsed:
                        return f"{collapsed[0].upper()}{collapsed[1:]}."

        return ""

    @staticmethod
    def _postprocess_preview_text(text: str) -> str:
        """
        Лёгкая фильтрация realtime-preview без агрессивной пунктуации.

        Нужна, чтобы в live-subtitles не проскакивали тех-артефакты/промпт-эхо.
        """
        clean = str(text or "").strip()
        if not clean:
            return ""

        lowered = clean.lower()
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            return ""

        normalized = TextUtils.normalize_phrase(clean)
        if BackendService._is_known_prompt_echo(normalized):
            return ""

        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if BackendService._looks_like_looping_artifact(words, min_words=6, min_bigram_hits=3):
            return ""

        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    @staticmethod
    def _extract_transcribed_text(payload: Any) -> str:
        """
        Нормализует результат транскрибации в строку.

        Исторически backend получал `str`, но текущий Transcriber отдает `dict`.
        Метод поддерживает оба контракта, чтобы не ломать stop/preview pipelines.
        """
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            direct_text = payload.get("text")
            if direct_text is not None:
                return str(direct_text).strip()
            nested = payload.get("result")
            if isinstance(nested, dict):
                nested_text = nested.get("text")
                if nested_text is not None:
                    return str(nested_text).strip()
            return ""
        return str(payload).strip()

    @staticmethod
    def _extract_transcribed_error(payload: Any) -> str:
        """Извлекает текст ошибки из payload транскрибации, если он присутствует."""
        if isinstance(payload, dict):
            error = payload.get("error")
            if error is not None:
                return str(error).strip()
        return ""


class IPCServer:
    """Unix socket сервер, который проксирует запросы в BackendService."""

    def __init__(self, socket_path: Path, service: BackendService) -> None:
        self.socket_path = socket_path
        self.service = service
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Останавливает accept loop."""
        self._stop_event.set()

    def serve_forever(self) -> None:
        """Основной цикл обработки входящих подключений."""
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(32)
        server.settimeout(0.8)

        logger.info("IPC сервер запущен на %s", self.socket_path)
        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        break
                    raise

                with conn:
                    self._handle_connection(conn)
        finally:
            server.close()
            if self.socket_path.exists():
                self.socket_path.unlink()
            logger.info("IPC сервер остановлен")

    def _handle_connection(self, conn: socket.socket) -> None:
        """Чтение одной JSON-команды и возврат JSON-ответа."""
        try:
            raw = conn.recv(1024 * 1024)
            if not raw:
                return
            text = raw.decode("utf-8").strip()
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("payload должен быть JSON-объектом")
        except Exception as exc:
            response = {
                "id": None,
                "ok": False,
                "error": {"code": "invalid_json", "message": str(exc)},
            }
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            return

        response = self.service.handle_request(payload)
        conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


def default_data_dir() -> Path:
    """Каталог состояния приложения в профиле пользователя."""
    return Path.home() / "Library" / "Application Support" / "KrabEar"


def default_socket_path(data_dir: Path) -> Path:
    """Путь Unix socket внутри того же каталога состояния."""
    return data_dir / "krabear.sock"


def configure_logging(data_dir: Path) -> None:
    """Настраивает логирование backend в файл и stdout."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "backend.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def build_service(data_dir: Path) -> BackendService:
    """Фабрика backend-сервиса с запуском проверок на старте."""
    store = StateStore(data_dir=data_dir)
    # Гарантируем наличие полного набора дефолтных настроек.
    store.save_settings(store.load_settings() or dict(DEFAULT_SETTINGS))
    store.maybe_compact()
    return BackendService(store=store)


def main() -> None:
    """CLI entrypoint backend-сервиса."""
    parser = argparse.ArgumentParser(description="Krab Ear backend service")
    parser.add_argument("--data-dir", default=None, help="Каталог для settings/history/socket")
    parser.add_argument("--socket-path", default=None, help="Путь Unix socket")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser() if args.data_dir else default_data_dir()
    socket_path = (
        Path(args.socket_path).expanduser()
        if args.socket_path
        else default_socket_path(data_dir)
    )

    configure_logging(data_dir)
    service = build_service(data_dir)
    server = IPCServer(socket_path=socket_path, service=service)

    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Получен сигнал %s, завершаем backend", signum)
        server.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    server.serve_forever()


if __name__ == "__main__":
    main()
