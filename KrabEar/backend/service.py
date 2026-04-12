"""IPC backend-сервис Krab Ear.

Сервис слушает Unix socket и обрабатывает JSON-RPC-подобные команды:
- start_recording / stop_recording
- get_history_page / search_history / delete_history_item
- get_settings / set_settings
- compact_history
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import tempfile
import logging
import os
from pathlib import Path
import re
import signal
import socket
import platform
import sys
import threading
import time
from typing import Any, Callable
import uuid

import numpy as np

# Обеспечиваем корректный импорт модулей KrabEar при запуске как standalone скрипта.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.call_assist_service import CallAssistService
from backend.event_bus import bus as event_bus
from backend.models import DEFAULT_SETTINGS
from contracts.stt_events import SttFailed, SttFinal, SttPartial
from contracts.registry import EventType
from contracts.translation_events import TranslationCompleted, TranslationFailed
from backend.recorder import AudioRecorder
from backend.state_store import StateStore
from backend.transcriber import Transcriber
from backend.translator import Translator
from core.config import settings
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

        # D.10a: LLM rewriter initialization (admin flag check via settings)
        self._llm_rewriter = self._init_llm_rewriter()

        if transcriber is None:
            self.transcriber = Transcriber(
                llm_rewriter=self._llm_rewriter,
                settings_get=self._get_runtime_setting,
            )
        else:
            self.transcriber = transcriber
            if self._llm_rewriter is not None:
                if hasattr(transcriber, "engine"):
                    if transcriber.engine._llm_rewriter is None:
                        transcriber.engine._llm_rewriter = self._llm_rewriter
                    transcriber.engine._settings_get = self._get_runtime_setting

        self.translator = translator or Translator()
        self._start_time: float = time.monotonic()
        self._settings_cache: dict[str, Any] | None = None
        self._settings_cache_ts: float = 0.0
        self._settings_cache_ttl: float = 5.0
        self._preview_lock = threading.Lock()
        self._preview_thread: threading.Thread | None = None
        self._preview_stop_event = threading.Event()
        self._preview_text = ""
        self._preview_duration_sec = 0.0
        self._preview_updated_at = 0.0
        self._preview_error_count: int = 0
        self._clipboard_history: list[dict] = []
        self._call_assist = CallAssistService(
            store=self.store,
            recorder=self.recorder,
            transcriber=self.transcriber,
            reset_preview_fn=self._reset_preview_state,
            start_preview_fn=lambda qp: self._start_preview_worker(quality_profile=qp),
        )

    def _init_llm_rewriter(self):
        """Создаёт LLMRewriter если settings.LLM_ENABLED. Возвращает None иначе."""
        if not settings.LLM_ENABLED:
            return None

        try:
            from backend.llm_rewriter import LLMRewriter
            rewriter = LLMRewriter(
                base_url=settings.LLM_BASE_URL,
                api_key=settings.LLM_API_KEY,
                model=settings.LLM_MODEL,
                timeout_sec=settings.LLM_TIMEOUT_SEC,
                circuit_fail_threshold=settings.LLM_CIRCUIT_FAIL_THRESHOLD,
                circuit_initial_reset_sec=settings.LLM_CIRCUIT_INITIAL_RESET_SEC,
                circuit_max_reset_sec=settings.LLM_CIRCUIT_MAX_RESET_SEC,
            )
            if rewriter.ping():
                logger.info(
                    "LLM rewriter инициализирован: %s @ %s",
                    settings.LLM_MODEL,
                    settings.LLM_BASE_URL,
                )
            else:
                logger.warning(
                    "LLM rewriter не отвечает на ping (%s), будет circuit-break'нут при первом rewrite",
                    settings.LLM_BASE_URL,
                )
            return rewriter
        except Exception as exc:
            logger.exception("Не удалось инициализировать LLM rewriter: %s", exc)
            return None

    def _cached_settings(self) -> dict[str, Any]:
        """Возвращает копию настроек с TTL-кэшем (5 сек). Избегает повторного чтения файла."""
        now = time.monotonic()
        if self._settings_cache is not None and (now - self._settings_cache_ts) < self._settings_cache_ttl:
            return dict(self._settings_cache)
        self._settings_cache = self.store.load_settings()
        self._settings_cache_ts = now
        return dict(self._settings_cache)

    def _invalidate_settings_cache(self) -> None:
        """Сбрасывает кэш настроек (вызывать после save_settings)."""
        self._settings_cache = None
        self._settings_cache_ts = 0.0

    def _get_runtime_setting(self, key: str, default: Any) -> Any:
        """Callback для AudioEngine: читает runtime toggle из StateStore.

        Используется для проверки llm_rewrite_enabled на каждой транскрипции.
        """
        try:
            return self._cached_settings().get(key, default)
        except Exception:
            return default

    def handle_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Обрабатывает один JSON-запрос и возвращает JSON-ответ."""
        request_id = payload.get("id")
        method = str(payload.get("method", "")).strip()
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, "invalid_params", "Параметр params должен быть объектом")

        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ping": self._handle_ping,  # VERIFIED: called from Swift (BackendSupervisor)
            "start_recording": self._handle_start_recording,  # VERIFIED: called from Swift (main)
            "stop_recording": self._handle_stop_recording,  # VERIFIED: called from Swift (main)
            "get_recording_state": self._handle_get_recording_state,  # VERIFIED: called from Swift (main, HistoryPanel)
            "start_call_assist": self._call_assist.handle_start,  # VERIFIED: called from Swift (HistoryPanel)
            "stop_call_assist": self._call_assist.handle_stop,  # VERIFIED: called from Swift (HistoryPanel)
            "get_call_assist_state": self._call_assist.handle_get_state,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_diagnostics": self._call_assist.handle_diagnostics,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_summary": self._call_assist.handle_summary,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_quick_phrase": self._call_assist.handle_quick_phrase,  # VERIFIED: called from Swift (HistoryPanel)
            "list_call_assist_quick_phrases": self._call_assist.handle_list_quick_phrases,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_cost_estimate": self._call_assist.handle_cost_estimate,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline": self._call_assist.handle_timeline,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_stats": self._call_assist.handle_timeline_stats,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_summary": self._call_assist.handle_timeline_summary,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_export": self._call_assist.handle_timeline_export,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_clear": self._call_assist.handle_timeline_clear,  # VERIFIED: called from Swift (HistoryPanel)
            "call_assist_timeline_to_history": self._call_assist.handle_timeline_to_history,  # VERIFIED: called from Swift (HistoryPanel)
            "list_audio_inputs": self._handle_list_audio_inputs,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_page": self._handle_get_history_page,  # VERIFIED: called from Swift (HistoryPanel)
            "search_history": self._handle_search_history,  # VERIFIED: called from Swift (HistoryPanel)
            "delete_history_item": self._handle_delete_history_item,  # VERIFIED: called from Swift (HistoryPanel)
            "set_paste_status": self._handle_set_paste_status,  # VERIFIED: called from Swift (main)
            "get_settings": self._handle_get_settings,  # VERIFIED: called from Swift (main)
            "set_settings": self._handle_set_settings,  # VERIFIED: called from Swift (main)
            "compact_history": self._handle_compact_history,  # VERIFIED: called from Swift (main, HistoryPanel)
            "add_history_item": self._handle_add_history_item,  # VERIFIED: called from Swift (main, HistoryPanel)
            "transcribe_paths": self._handle_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
            "preview_transcribe_paths": self._handle_preview_transcribe_paths,  # VERIFIED: called from Swift (HistoryPanel)
            "translate_text": self._handle_translate_text,  # VERIFIED: called from Swift (main, HistoryPanel)
            "get_capabilities": self._handle_get_capabilities,  # UNUSED: consider deprecation (no Swift callers)
            "get_readiness": self._handle_get_readiness,  # UNUSED: consider deprecation (no Swift callers)
            "get_diagnostics": self._handle_get_diagnostics,  # диагностика: system, stt, llm, history, settings_cache
            "set_translation_glossary_item": self._handle_set_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
            "remove_translation_glossary_item": self._handle_remove_translation_glossary_item,  # VERIFIED: called from Swift (HistoryPanel)
            "get_glossary_suggestions": self._handle_get_glossary_suggestions,  # авто-обучение глоссария: предлагает пары source→target из истории
            "import_history_ndjson": self._handle_import_history_ndjson,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_stats": self._handle_get_history_stats,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_overview": self._handle_get_history_overview,  # VERIFIED: called from Swift (HistoryPanel)
            "get_history_item": self._handle_get_history_item,  # полные детали одной записи истории по ID
            "get_recording_stats": self._handle_get_recording_stats,  # recording metadata statistics
            "get_metrics_dashboard": self._handle_get_metrics_dashboard,  # real-time metrics dashboard snapshot
            "summarize_text": self._handle_summarize_text,  # VERIFIED: called from Swift (HistoryPanel)
            "summarize_item": self._handle_summarize_item,  # LLM summary для элемента истории по ID
            "llm_status": self._handle_llm_status,  # UNUSED: consider deprecation (no Swift callers)
            "get_vocabulary_suggestions": self._handle_get_vocabulary_suggestions,
            "export_history": self._handle_export_history,
            "export_history_srt": self._handle_export_history_srt,
            "get_clipboard_history": self._handle_get_clipboard_history,
            "repaste_item": self._handle_repaste_item,
            "cleanup_old_history": self._handle_cleanup_old_history,  # удаляет записи старше N дней
            "get_storage_info": self._handle_get_storage_info,  # размер файлов данных
            "apply_profile_preset": self._handle_apply_profile_preset,  # применяет пресет настроек профиля
            "list_profile_presets": self._handle_list_profile_presets,  # список доступных пресетов профилей
            "get_audio_devices": self._handle_get_audio_devices,  # список доступных аудиовходов для GUI
            "test_microphone": self._handle_test_microphone,  # тест микрофона: RMS/peak уровни
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
        try:
            history_count = self.store.count_active_items()
        except Exception:
            history_count = -1
        return {
            "status": "ok",
            "service": "krabear-backend",
            "version": "1.0.0",
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "is_recording": bool(getattr(self.recorder, "is_recording", False)),
            "history_count": history_count,
        }

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
        settings = self._cached_settings()
        if bool(settings.get("realtime_preview_enabled", True)):
            quality_profile = str(settings.get("quality_profile", "balanced"))
            self._start_preview_worker(quality_profile=quality_profile)
        return {"status": "recording"}

    def _handle_stop_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        self._stop_preview_worker()
        settings = self._cached_settings()
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

        # Загружаем пользовательский vocabulary для подсказок Whisper
        user_vocabulary = self.store.load_vocabulary() or []

        transcribe_payload = self.transcriber.transcribe(
            audio,
            quality_profile=quality_profile,
            cleanup_profile=cleanup_profile,
            lang_hint=lang_hint,
            extra_vocabulary=user_vocabulary if user_vocabulary else None,
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
        if translation.ok and translated_text:
            event_bus.emit_typed(EventType.TRANSLATION_COMPLETED, TranslationCompleted(
                history_id="",  # будет обновлено ниже после сохранения в store
                source_text=text,
                translated_text=translated_text,
                source_lang=translation.source_lang or "",
                target_lang=translation.target_lang or "",
                engine=translation.engine or "",
                mode=translation.mode or "",
            ))
        elif not translation.ok and translation_status not in ("not_requested", "off"):
            event_bus.emit_typed(EventType.TRANSLATION_FAILED, TranslationFailed(
                history_id=None,
                source_text=text,
                reason=translation.status or "unknown",
                source_lang=translation.source_lang,
                target_lang=translation.target_lang,
            ))

        tp = transcribe_payload if isinstance(transcribe_payload, dict) else {}
        confidence = tp.get("confidence", 0.0)
        if confidence < 0.4 and text:
            logger.warning("Низкая уверенность STT: %.2f — возможна ошибка распознавания", confidence)
        diarization_data = tp.get("diarization")

        # Format text with speaker labels if diarization produced multiple speakers
        display_text = self._format_text_with_speakers(final_text, diarization_data)

        item = self.store.add_history_item(
            text=display_text,
            paste_status="failed",
            source_text=text,
            translated_text=translated_text,
            translation_mode=translation.mode,
            source_lang=translation.source_lang,
            target_lang=translation.target_lang,
            translation_status=translation_status,
            translation_engine=translation.engine,
            cleaned_text=tp.get("cleaned_text", ""),
            llm_applied=bool(tp.get("llm_applied", False)),
            llm_latency_ms=int(tp.get("llm_latency_ms", 0) or 0),
            diarization=diarization_data,
        )
        self._clipboard_history.append({
            "text": final_text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "history_id": item.id,
        })
        if len(self._clipboard_history) > 20:
            self._clipboard_history = self._clipboard_history[-20:]

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
            "text": display_text,
            "original_text": text,
            "translated_text": translated_text,
            "history_id": item.id,
            "ts": item.ts,
            "stop_tail_trim_ms": stop_tail_trim_ms,
            "silence_detected": silence_detected,
            "silence_guard_enabled": silence_guard_enabled,
            "background_guard_rejected": background_guard_rejected,
        }
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
        return self._cached_settings()

    def _handle_set_settings(self, params: dict[str, Any]) -> dict[str, Any]:
        settings = self._cached_settings()
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

        result = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return result

    # ---------------------------------------------------------------------------
    # Пресеты профилей настроек
    # ---------------------------------------------------------------------------

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
            "auto_paste": False,  # не вставлять автоматически во время митинга
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

    def _handle_apply_profile_preset(self, params: dict[str, Any]) -> dict[str, Any]:
        """Применяет пресет настроек профиля, сохраняет и сбрасывает кэш."""
        profile = str(params.get("profile", "")).strip()
        preset = self._PROFILE_PRESETS.get(profile)
        if preset is None:
            available = ", ".join(self._PROFILE_PRESETS.keys())
            raise ValueError(f"Неизвестный пресет профиля: '{profile}'. Доступные: {available}")

        settings = self._cached_settings()
        settings.update(preset)
        result = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return result

    def _handle_list_profile_presets(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных пресетов профилей с описаниями и значениями."""
        presets = []
        for name, values in self._PROFILE_PRESETS.items():
            presets.append({
                "name": name,
                "description": self._PROFILE_PRESET_DESCRIPTIONS.get(name, ""),
                "settings": dict(values),
            })
        return {"presets": presets}

    def _handle_translate_text(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отдельная IPC-команда перевода текста для UI и будущих workflow."""
        text = str(params.get("text", "")).strip()
        mode = str(params.get("translation_mode", "off"))
        translation_style = str(params.get("translation_style", "neutral"))
        settings = self._cached_settings()
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
    def _format_text_with_speakers(text: str, diarization: dict | None) -> str:
        """Форматирует текст с метками спикеров из diarization speaker_turns.

        Если diarization неактивен или менее 2 спикеров — возвращает исходный текст.
        Использует speaker_turns (склеенные реплики) для читаемого вывода.
        """
        if not diarization or not isinstance(diarization, dict):
            return text
        if not diarization.get("enabled"):
            return text
        turns = diarization.get("speaker_turns", [])
        if not turns or len(turns) < 2:
            return text
        # Check that there are actually multiple speakers
        speakers = {t.get("speaker") for t in turns if t.get("speaker")}
        if len(speakers) < 2:
            return text
        parts: list[str] = []
        current_speaker = None
        for turn in turns:
            speaker = turn.get("speaker", "?")
            turn_text = str(turn.get("text", "")).strip()
            if not turn_text:
                continue
            if speaker != current_speaker:
                current_speaker = speaker
                parts.append(f"\n[{speaker}]: {turn_text}")
            else:
                parts.append(f" {turn_text}")
        if parts:
            return "".join(parts).strip()
        return text

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

    def _handle_get_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает комплексную диагностику: системная информация, STT, LLM, история и кэш настроек."""
        try:
            diarization_device = str(self.transcriber.engine._resolve_diarization_device())
        except Exception:
            diarization_device = "unknown"

        try:
            history_count = self.store.count_active_items()
        except Exception:
            history_count = -1

        return {
            "system": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "uptime_sec": time.monotonic() - self._start_time,
            },
            "stt": {
                "model_balanced": settings.MODEL_BALANCED,
                "model_max": settings.MODEL_MAX_CANDIDATES,
                "quality_profile": self.transcriber.engine.quality_profile,
                "current_model": self.transcriber.engine.current_model,
                "diarization_enabled": settings.DIARIZATION_ENABLED,
                "diarization_device": diarization_device,
            },
            "llm": self._llm_rewriter.status() if self._llm_rewriter else {"enabled": False},
            "history": {
                "total_items": history_count,
                "data_dir": str(self.store.data_dir),
                "transcripts_dir": str(Path(self.store.data_dir) / "transcripts"),
            },
            "settings_cache": {
                "ttl_sec": self._settings_cache_ttl,
                "cached": self._settings_cache is not None,
            },
        }

    def _handle_get_capabilities(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает матрицу доступных возможностей текущей сборки."""
        settings = self._cached_settings()
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
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary[source] = target
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return {"updated": True, "count": len(saved.get("translation_glossary", {}))}

    def _handle_remove_translation_glossary_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет одну пару из глоссария перевода."""
        source = str(params.get("source", "")).strip()
        if not source:
            raise RuntimeError("source обязателен")
        settings = self._cached_settings()
        glossary = settings.get("translation_glossary", {})
        if not isinstance(glossary, dict):
            glossary = {}
        glossary.pop(source, None)
        settings["translation_glossary"] = glossary
        saved = self.store.save_settings(settings)
        self._invalidate_settings_cache()
        return {"removed": True, "count": len(saved.get("translation_glossary", {}))}

    def _handle_get_glossary_suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует историю переводов и предлагает пары source→target для глоссария.

        Сканирует записи истории с source_text и translated_text, извлекает:
        - заглавные слова/фразы (имена собственные, бренды)
        - термины из BRAND_REPLACEMENTS
        - повторяющиеся слова в парах оригинал→перевод

        Возвращает кандидатов, которых ещё нет в текущем глоссарии.
        """
        from core.utils import _BRAND_REPLACEMENTS_RAW

        scan_limit = max(10, min(int(params.get("scan_limit", 200) or 200), 1000))
        min_count = max(2, min(int(params.get("min_count", 2) or 2), 20))
        top_k = max(5, min(int(params.get("top_k", 30) or 30), 100))

        items, _ = self.store.get_history_page(cursor=None, limit=scan_limit)

        # Загружаем текущий глоссарий — фильтруем уже добавленные пары
        settings = self._cached_settings()
        current_glossary: dict[str, str] = settings.get("translation_glossary", {}) or {}

        # Бренды из utils.py — канонические замены, которые стоит добавить в глоссарий
        brand_canonicals: list[str] = [canonical for _pat, canonical in _BRAND_REPLACEMENTS_RAW]

        # Собираем частоту заглавных слов и пары source→translated из истории
        pair_counts: dict[str, dict[str, int]] = {}  # source_word → {translated_word: count}
        capitalized_freq: dict[str, int] = {}

        for item in items:
            source_text = str(item.get("source_text", "") or "").strip()
            translated_text = str(item.get("translated_text", "") or "").strip()
            if not source_text or not translated_text:
                continue

            cap_words = re.findall(r"\b[A-ZА-Я][A-Za-zА-Яа-я]{2,}\b", source_text)
            for w in cap_words:
                capitalized_freq[w] = capitalized_freq.get(w, 0) + 1

            for src_word in set(cap_words):
                pattern = re.compile(r"\b" + re.escape(src_word) + r"\b", re.IGNORECASE)
                match = pattern.search(translated_text)
                if match:
                    found = match.group(0)
                    if src_word not in pair_counts:
                        pair_counts[src_word] = {}
                    pair_counts[src_word][found] = pair_counts[src_word].get(found, 0) + 1

        suggestions: list[dict] = []

        # 1. Пары из истории (src != target — реальный перевод)
        for src_word, trans_counts in pair_counts.items():
            if capitalized_freq.get(src_word, 0) < min_count:
                continue
            if src_word in current_glossary:
                continue
            best_target = max(trans_counts, key=lambda k: trans_counts[k])
            if src_word.lower() != best_target.lower():
                suggestions.append({
                    "source": src_word,
                    "target": best_target,
                    "count": capitalized_freq[src_word],
                    "origin": "history_pair",
                })

        # 2. Заглавные слова без явного перевода — пользователь уточнит target
        for word, count in capitalized_freq.items():
            if count < min_count:
                continue
            if word in current_glossary:
                continue
            if any(s["source"] == word for s in suggestions):
                continue
            suggestions.append({
                "source": word,
                "target": word,
                "count": count,
                "origin": "capitalized_term",
            })

        # 3. Бренды из BRAND_REPLACEMENTS — предлагаем зафиксировать в глоссарии
        for canonical in brand_canonicals:
            if canonical in current_glossary:
                continue
            if any(s["source"] == canonical for s in suggestions):
                continue
            suggestions.append({
                "source": canonical,
                "target": canonical,
                "count": 0,
                "origin": "brand_replacement",
            })

        # Сначала history_pair/capitalized_term по count desc, бренды в конце
        suggestions.sort(key=lambda s: (s["origin"] == "brand_replacement", -s["count"], s["source"]))
        top = suggestions[:top_k]

        return {
            "suggestions": top,
            "total_candidates": len(suggestions),
            "scanned_items": len(items),
            "current_glossary_size": len(current_glossary),
        }

    def _handle_compact_history(self, params: dict[str, Any]) -> dict[str, Any]:
        stats = self.store.compact_with_stats()
        return {"compacted": True, **stats}

    def _handle_import_history_ndjson(self, params: dict[str, Any]) -> dict[str, Any]:
        """Импортирует историю из внешнего NDJSON-файла."""
        raw_path = str(params.get("path", "")).strip()
        if not raw_path:
            raise RuntimeError("path обязателен")
        resolved = Path(raw_path).expanduser().resolve()
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
            return {"error": {"message": f"Path outside allowed directories: {resolved}"}}
        result = self.store.import_history_ndjson(resolved)
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

    def _handle_get_history_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает полные детали одной записи истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id обязателен")

        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        for item in items:
            if item.id == item_id:
                result = item.to_dict()
                # Вычисляемые поля
                result["text_length"] = len(item.text)
                result["word_count"] = len(item.text.split()) if item.text else 0
                # Проверяем наличие файла транскрипта
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                matching = list(transcripts_dir.glob(f"*{item_id[:8]}*")) if transcripts_dir.exists() else []
                result["transcript_file"] = str(matching[0]) if matching else None
                return result

        raise RuntimeError(f"Запись {item_id} не найдена")

    # ------------------------------------------------------------------
    # Экспорт истории (markdown / SRT)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_duration_human(seconds: float | None) -> str:
        """Форматирует длительность аудио в читаемый вид: '5м 23с'."""
        if seconds is None or seconds <= 0:
            return ""
        total = int(seconds)
        h, remainder = divmod(total, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}ч {m}м {s}с"
        if m > 0:
            return f"{m}м {s}с"
        return f"{s}с"

    @staticmethod
    def _format_ts_human(iso_ts: str) -> str:
        """Преобразует ISO timestamp в читаемый формат: '2026-04-11 22:46'."""
        try:
            dt = datetime.fromisoformat(iso_ts)
            return dt.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return iso_ts

    def _handle_export_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует всю историю в формате Markdown с метаданными и диаризацией.

        Параметры:
            limit (int): максимальное количество записей (по умолчанию 500)
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): markdown-текст
            total_items (int): количество экспортированных записей
            path (str|None): путь к файлу, если save_to_file=True
        """
        limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

        # Загружаем историю через пагинацию (от новых к старым)
        items_dicts, _ = self.store.get_history_page_filtered(
            cursor=None, limit=limit,
            paste_status=None, translation_mode=None,
        )
        if not items_dicts:
            return {"content": "# Krab Ear \u2014 \u042d\u043a\u0441\u043f\u043e\u0440\u0442 \u0438\u0441\u0442\u043e\u0440\u0438\u0438\n\n\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u0443\u0441\u0442\u0430.\n", "total_items": 0, "path": None}

        from backend.models import HistoryItem as _HI
        items = [_HI.from_dict(d) for d in items_dicts]

        # Определяем временной диапазон (items отсортированы newest-first)
        ts_list = [it.ts for it in items if it.ts]
        earliest_ts = self._format_ts_human(ts_list[-1]) if ts_list else "?"
        latest_ts = self._format_ts_human(ts_list[0]) if ts_list else "?"
        export_ts = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Метаданные шапки
        header_lines = [
            "# Krab Ear \u2014 \u042d\u043a\u0441\u043f\u043e\u0440\u0442 \u0438\u0441\u0442\u043e\u0440\u0438\u0438",
            f"- \u0417\u0430\u043f\u0438\u0441\u0435\u0439: {len(items)}",
            f"- \u041f\u0435\u0440\u0438\u043e\u0434: {earliest_ts} \u2014 {latest_ts}",
            f"- \u042d\u043a\u0441\u043f\u043e\u0440\u0442: {export_ts}",
            "",
            "---",
            "",
        ]

        sections: list[str] = []
        for idx, item in enumerate(items, start=1):
            # Заголовок записи
            ts_human = self._format_ts_human(item.ts)
            duration_str = self._format_duration_human(item.audio_duration_sec)
            title_parts = [f"## {idx}. [{ts_human}]"]
            if duration_str:
                title_parts.append(f"({duration_str})")
            sections.append(" ".join(title_parts))

            # Метаданные записи
            meta_parts: list[str] = []
            if item.source_lang:
                meta_parts.append(f"**\u042f\u0437\u044b\u043a:** {item.source_lang}")
            diar = item.diarization
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2:
                    meta_parts.append(f"**\u0421\u043f\u0438\u043a\u0435\u0440\u044b:** {len(speakers)}")
            if meta_parts:
                sections.append(" | ".join(meta_parts))
                sections.append("")

            # Основной текст (с метками спикеров если есть диаризация)
            if diar and isinstance(diar, dict) and diar.get("enabled"):
                turns = diar.get("speaker_turns", [])
                speakers = {t.get("speaker") for t in turns if t.get("speaker")}
                if len(speakers) >= 2 and turns:
                    for turn in turns:
                        speaker = turn.get("speaker", "?")
                        turn_text = str(turn.get("text", "")).strip()
                        if turn_text:
                            sections.append(f"[{speaker}]: {turn_text}")
                else:
                    sections.append(item.text)
            else:
                sections.append(item.text)

            # Перевод (если есть)
            if item.translated_text and item.translation_status == "ok":
                mode_label = item.translation_mode or ""
                sections.append("")
                sections.append(f"**\u041f\u0435\u0440\u0435\u0432\u043e\u0434** ({mode_label}):")
                sections.append(item.translated_text)

            sections.append("")

        content = "\n".join(header_lines) + "\n".join(sections)

        # Сохранение в файл (опционально)
        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
                file_path = transcripts_dir / filename
                file_path.write_text(content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u044d\u043a\u0441\u043f\u043e\u0440\u0442 \u0432 \u0444\u0430\u0439\u043b: %s", exc)

        return {"content": content, "total_items": len(items), "path": save_path}

    def _handle_export_history_srt(self, params: dict[str, Any]) -> dict[str, Any]:
        """Экспортирует запись истории в формате SRT-субтитров (по speaker_turns).

        Параметры:
            id (str): идентификатор записи в истории
            save_to_file (bool): если True, сохраняет файл в transcripts/

        Возвращает:
            content (str): SRT-текст
            item_id (str): ID записи
            speakers (int): количество спикеров
            segments (int): количество сегментов
            path (str|None): путь к файлу, если save_to_file=True
        """
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("id \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u0435\u043d")

        # Ищем запись по ID через пагинацию
        from backend.models import HistoryItem as _HI
        target_item: _HI | None = None
        cursor: str | None = None
        for _ in range(200):  # безопасный лимит итераций
            page_dicts, next_cursor = self.store.get_history_page_filtered(
                cursor=cursor, limit=100,
                paste_status=None, translation_mode=None,
            )
            if not page_dicts:
                break
            for d in page_dicts:
                if d.get("id") == item_id:
                    target_item = _HI.from_dict(d)
                    break
            if target_item is not None:
                break
            if next_cursor is None:
                break
            cursor = next_cursor

        if target_item is None:
            raise RuntimeError(f"\u0417\u0430\u043f\u0438\u0441\u044c \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430: {item_id}")

        diar = target_item.diarization
        if not diar or not isinstance(diar, dict) or not diar.get("enabled"):
            # Без диаризации — один сегмент на весь текст
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        turns = diar.get("speaker_turns", [])
        if not turns:
            duration = target_item.audio_duration_sec or 0.0
            srt_content = self._build_srt_single(target_item.text, duration)
            return self._finalize_srt_export(
                params, srt_content, item_id, speakers=1, segments=1,
            )

        speakers = {t.get("speaker") for t in turns if t.get("speaker")}
        srt_lines: list[str] = []
        for seq, turn in enumerate(turns, start=1):
            speaker = turn.get("speaker", "SPEAKER_00")
            turn_text = str(turn.get("text", "")).strip()
            if not turn_text:
                continue
            start_sec = float(turn.get("start", 0.0) or 0.0)
            end_sec = float(turn.get("end", start_sec + 1.0) or start_sec + 1.0)
            srt_lines.append(str(seq))
            srt_lines.append(f"{self._srt_timestamp(start_sec)} --> {self._srt_timestamp(end_sec)}")
            srt_lines.append(f"[{speaker}]: {turn_text}")
            srt_lines.append("")

        srt_content = "\n".join(srt_lines)
        return self._finalize_srt_export(
            params, srt_content, item_id,
            speakers=len(speakers), segments=len(turns),
        )

    def _finalize_srt_export(
        self,
        params: dict[str, Any],
        srt_content: str,
        item_id: str,
        speakers: int,
        segments: int,
    ) -> dict[str, Any]:
        """Общая финализация SRT-экспорта: опциональное сохранение в файл."""
        save_path: str | None = None
        if self._coerce_bool(params.get("save_to_file", False), default=False):
            try:
                transcripts_dir = Path(self.store.data_dir) / "transcripts"
                transcripts_dir.mkdir(exist_ok=True)
                filename = f"srt_{item_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.srt"
                file_path = transcripts_dir / filename
                file_path.write_text(srt_content, encoding="utf-8")
                save_path = str(file_path)
            except Exception as exc:
                logger.warning("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c SRT \u0432 \u0444\u0430\u0439\u043b: %s", exc)
        return {
            "content": srt_content,
            "item_id": item_id,
            "speakers": speakers,
            "segments": segments,
            "path": save_path,
        }

    @staticmethod
    def _build_srt_single(text: str, duration: float) -> str:
        """Строит SRT с одним сегментом (без диаризации)."""
        end_ts = BackendService._srt_timestamp(duration) if duration > 0 else "00:00:01,000"
        return f"1\n00:00:00,000 --> {end_ts}\n{text}\n"

    @staticmethod
    def _srt_timestamp(seconds: float) -> str:
        """Конвертирует секунды в SRT-формат: HH:MM:SS,mmm."""
        if seconds < 0:
            seconds = 0.0
        total_ms = int(round(seconds * 1000))
        h, remainder = divmod(total_ms, 3600000)
        m, remainder = divmod(remainder, 60000)
        s, ms = divmod(remainder, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _handle_get_clipboard_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает последние N вставленных транскрипций из clipboard_history.

        Параметры:
            limit (int): максимальное количество элементов (по умолчанию 10, макс 20)

        Возвращает:
            items (list): список записей {text, ts, history_id}
            count (int): общее количество элементов в истории
        """
        limit = self._coerce_bounded_int(
            value=params.get("limit", 10),
            default=10,
            min_value=1,
            max_value=20,
        )
        return {
            "items": self._clipboard_history[-limit:],
            "count": len(self._clipboard_history),
        }

    def _handle_repaste_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Находит текст по history_id в clipboard_history и возвращает его для повторной вставки.

        Параметры:
            history_id (str): идентификатор записи из clipboard_history

        Возвращает:
            text (str): текст для вставки
            history_id (str): подтверждённый идентификатор
            found (bool): True если запись найдена
        """
        history_id = str(params.get("history_id", "")).strip()
        if not history_id:
            raise RuntimeError("history_id обязателен")
        for entry in reversed(self._clipboard_history):
            if entry.get("history_id") == history_id:
                return {
                    "text": entry["text"],
                    "history_id": history_id,
                    "found": True,
                }
        raise RuntimeError(f"Запись не найдена в clipboard_history: {history_id}")

    def _handle_cleanup_old_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Удаляет записи истории старше N дней (по умолчанию 90).

        Параметры:
            older_than_days (int): порог возраста в днях (по умолчанию 90)

        Возвращает:
            deleted (int): количество удалённых записей
            remaining (int): количество оставшихся активных записей
        """
        older_than_days = int(params.get("older_than_days", 90))
        if older_than_days <= 0:
            raise RuntimeError("older_than_days должен быть положительным числом")

        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        cutoff_iso = cutoff.isoformat()

        with self.store._lock():
            active = self.store._load_active_items_unlocked()
            to_delete = [item for item in active if item.ts < cutoff_iso]
            for item in to_delete:
                self.store._append_ndjson(self.store.tombstones_path, {"id": item.id})
            remaining = len(active) - len(to_delete)

        return {"deleted": len(to_delete), "remaining": remaining}

    def _handle_get_storage_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает информацию о размере файлов данных Krab Ear.

        Возвращает:
            history_file_size_mb (float): размер history.ndjson в МБ
            transcripts_count (int): количество .md файлов в transcripts/
            transcripts_size_mb (float): суммарный размер transcripts/ в МБ
            reports_count (int): количество файлов-отчётов в data_dir
            total_data_mb (float): суммарный размер директории данных в МБ
        """
        data_dir = Path(self.store.data_dir)

        # Размер файла истории
        history_path = self.store.history_path
        history_size_mb = history_path.stat().st_size / (1024 * 1024) if history_path.exists() else 0.0

        # Транскрипты (.md файлы)
        transcripts_dir = data_dir / "transcripts"
        md_files = list(transcripts_dir.glob("*.md")) if transcripts_dir.exists() else []
        transcripts_count = len(md_files)
        transcripts_size_mb = sum(f.stat().st_size for f in md_files) / (1024 * 1024)

        # Файлы отчётов
        reports_count = len(list(data_dir.glob("*.report")) + list(data_dir.glob("report_*")))

        # Общий размер директории данных
        total_bytes = sum(
            f.stat().st_size
            for f in data_dir.rglob("*")
            if f.is_file()
        )
        total_data_mb = total_bytes / (1024 * 1024)

        return {
            "history_file_size_mb": round(history_size_mb, 3),
            "transcripts_count": transcripts_count,
            "transcripts_size_mb": round(transcripts_size_mb, 3),
            "reports_count": reports_count,
            "total_data_mb": round(total_data_mb, 3),
        }

    def _handle_get_recording_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает кумулятивную статистику записей: длительность, языки, LLM, диаризация.

        Сканирует всю активную историю через store и агрегирует метаданные.
        """
        active = self.store._load_active_items_with_lock()

        now = datetime.now()
        today_iso = now.date().isoformat()
        week_start = (now - timedelta(days=now.weekday())).date().isoformat()

        total_count = 0
        total_duration_sec = 0.0
        today_count = 0
        today_duration_sec = 0.0
        week_count = 0
        week_duration_sec = 0.0
        llm_applied_count = 0
        diarization_used_count = 0
        lang_counts: dict[str, int] = {}

        for item in active:
            total_count += 1
            dur = item.audio_duration_sec or 0.0
            total_duration_sec += dur

            day_str = item.ts[:10]  # "YYYY-MM-DD"
            if day_str == today_iso:
                today_count += 1
                today_duration_sec += dur
            if day_str >= week_start:
                week_count += 1
                week_duration_sec += dur

            if item.llm_applied:
                llm_applied_count += 1

            if item.diarization is not None and isinstance(item.diarization, dict):
                if item.diarization.get("enabled"):
                    diarization_used_count += 1

            lang = item.source_lang.strip()
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1

        avg_duration = round(total_duration_sec / total_count, 2) if total_count else 0.0
        llm_rate = round(llm_applied_count / total_count, 4) if total_count else 0.0
        diarization_rate = round(diarization_used_count / total_count, 4) if total_count else 0.0

        most_used_lang = ""
        if lang_counts:
            most_used_lang = max(lang_counts, key=lambda k: lang_counts[k])

        return {
            "total_count": total_count,
            "total_duration_sec": round(total_duration_sec, 2),
            "today_count": today_count,
            "today_duration_sec": round(today_duration_sec, 2),
            "week_count": week_count,
            "week_duration_sec": round(week_duration_sec, 2),
            "avg_duration_sec": avg_duration,
            "most_used_lang": most_used_lang,
            "lang_distribution": [
                {"lang": lang, "count": cnt}
                for lang, cnt in sorted(lang_counts.items(), key=lambda p: p[1], reverse=True)[:10]
            ],
            "llm_applied_count": llm_applied_count,
            "llm_correction_rate": llm_rate,
            "diarization_used_count": diarization_used_count,
            "diarization_usage_rate": diarization_rate,
        }

    def _handle_get_metrics_dashboard(self, params: dict[str, Any]) -> dict[str, Any]:
        """Снимок метрик реального времени: сессия, LLM, call_assist, конфиг."""
        settings = self._cached_settings()

        # Active session info
        preview_active = self._preview_thread is not None and self._preview_thread.is_alive()

        return {
            "session": {
                "recording_active": bool(getattr(self.recorder, 'is_recording', False)),
                "preview_active": preview_active,
                "preview_text_length": len(self._preview_text),
                "preview_duration_sec": self._preview_duration_sec,
            },
            "llm": {
                "enabled": settings.get("llm_rewrite_enabled", False),
                "model": settings.get("llm_model", "?"),
                "status": self._llm_rewriter.status() if self._llm_rewriter else None,
            },
            "call_assist": self._call_assist.state,
            "import": {
                # Check if import is active by looking at import queue state
                "active": False,  # Would need import state tracking
            },
            "config_snapshot": {
                "quality": settings.get("quality_profile", "balanced"),
                "cleanup": settings.get("cleanup_profile", "soft"),
                "translation_mode": settings.get("translation_mode", "off"),
                "diarization": settings.get("diarization_enabled", False),
                "network_mode": settings.get("network_mode", "offline_default"),
            },
        }

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

    def _generate_summary(self, text: str) -> str | None:
        """Генерирует краткое LLM-summary для длинного текста. Возвращает None если LLM недоступен."""
        if self._llm_rewriter is None:
            return None
        try:
            result = self._llm_rewriter.summarize(text, max_sentences=3)
            if result.ok and result.text:
                logger.info("LLM summary сгенерировано (%d мс)", result.latency_ms or 0)
                return result.text
            logger.debug("LLM summary не удалось: %s", result.fallback_reason)
            return None
        except Exception as exc:
            logger.warning("Ошибка генерации LLM summary: %s", exc)
            return None

    def _handle_summarize_item(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует LLM-summary для элемента истории по ID."""
        item_id = str(params.get("id", "")).strip()
        if not item_id:
            raise RuntimeError("Параметр id обязателен")

        # Найти элемент в истории
        with self.store._lock():
            items = self.store._load_active_items_unlocked()
        target = None
        for item in items:
            if item.id == item_id:
                target = item
                break
        if target is None:
            raise RuntimeError(f"Элемент не найден: {item_id}")

        text = target.text or ""
        if len(text) < 50:
            raise RuntimeError("Текст слишком короткий для summary")

        summary = self._generate_summary(text)
        if summary is None:
            # Fallback на локальный summary
            local = self._summarize_text_locally(text, mode="summary_short", max_points=3)
            return {
                "id": item_id,
                "summary": local["summary"],
                "llm": False,
                "source_chars": len(text),
            }

        return {
            "id": item_id,
            "summary": summary,
            "llm": True,
            "source_chars": len(text),
        }

    def _handle_llm_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает диагностическую информацию о LLM rewriter'е.

        D.10a. Используется для Swift UI статус-индикатора и dev smoke тестов.
        """
        runtime_enabled = bool(self._cached_settings().get("llm_rewrite_enabled", False))

        if self._llm_rewriter is None:
            return {
                "enabled": False,
                "admin_enabled": bool(settings.LLM_ENABLED),
                "runtime_enabled": runtime_enabled,
                "reachable": False,
                "model": None,
                "circuit_state": None,
                "last_latency_ms": None,
                "last_error": "llm_rewriter не инициализирован",
            }

        inner = self._llm_rewriter.status()
        reachable = bool(inner.get("reachable", False))
        admin_enabled = True  # если мы здесь, settings.LLM_ENABLED=True (инвариант _init_llm_rewriter)
        return {
            **inner,
            "admin_enabled": admin_enabled,
            "runtime_enabled": runtime_enabled,
            "enabled": bool(admin_enabled and runtime_enabled and reachable),
        }

    # ── Стоп-слова для фильтрации vocabulary suggestions ──────────────
    _STOP_WORDS_RU = frozenset([
        "быть", "было", "была", "были", "буду", "будет", "будут",
        "этот", "этой", "этом", "этих", "этого", "этому",
        "который", "которая", "которое", "которые", "которого", "которой",
        "может", "можно", "могут", "можем",
        "если", "когда", "потом", "потому", "после", "перед",
        "очень", "более", "менее", "также", "тоже",
        "через", "между", "около", "вокруг",
        "нужно", "нужна", "надо", "просто",
        "здесь", "сейчас", "тогда", "всегда", "никогда",
        "ничего", "некоторые", "каждый", "другой", "другие",
        "такой", "такая", "такие", "такое",
        "свой", "свою", "свои", "своей", "своего",
        "весь", "вся", "всё", "все", "всех", "всем",
        "один", "одна", "одно", "одни",
        "наш", "наша", "наши", "ваш", "ваша", "ваши",
        "есть", "нет", "там", "тут", "еще", "ещё", "уже",
        "только", "самый", "самая", "самое",
        "хорошо", "ладно", "давай", "давайте",
    ])
    _STOP_WORDS_ES = frozenset([
        "pero", "para", "como", "desde", "este", "esta", "esto",
        "estos", "estas", "donde", "cuando", "porque", "aunque",
        "puede", "pueden", "podemos", "tiene", "tienen",
        "hace", "hacen", "está", "están", "sido", "haber",
        "también", "mucho", "mucha", "muchos", "muchas",
        "otro", "otra", "otros", "otras",
        "todo", "toda", "todos", "todas",
        "cada", "mismo", "misma", "mismos",
        "algo", "nada", "siempre", "nunca",
        "aquí", "ahora", "entonces", "después", "antes",
        "entre", "sobre", "contra", "hacia",
        "solo", "bueno", "bien", "vale",
    ])

    def _handle_get_vocabulary_suggestions(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует историю транскрибаций и предлагает слова для vocabulary.

        Сканирует последние N записей истории, находит слова с частотой >= min_count,
        фильтрует стоп-слова и короткие слова, возвращает top-K кандидатов.
        """
        scan_limit = max(10, min(int(params.get("scan_limit", 100) or 100), 500))
        min_count = max(2, min(int(params.get("min_count", 3) or 3), 20))
        top_k = max(5, min(int(params.get("top_k", 20) or 20), 50))
        min_word_len = max(2, min(int(params.get("min_word_len", 4) or 4), 10))

        # Собираем тексты из последних записей истории
        items, _ = self.store.get_history_page(cursor=None, limit=scan_limit)

        # Подсчёт частоты слов
        word_freq: dict[str, int] = {}
        for item in items:
            text = str(item.get("text", "") or "")
            source_text = str(item.get("source_text", "") or "")
            # Используем source_text (до перевода) если есть, иначе text
            raw = source_text if source_text else text
            words = re.findall(r"[A-Za-zА-Яа-яÁÉÍÓÚáéíóúÑñÜü0-9_-]{2,}", raw)
            for w in words:
                key = w.strip()
                if len(key) >= min_word_len:
                    word_freq[key] = word_freq.get(key, 0) + 1

        # Фильтрация стоп-слов и уже известных vocabulary
        current_vocab = set(self.store.load_vocabulary())
        stop_words = self._STOP_WORDS_RU | self._STOP_WORDS_ES
        candidates: list[tuple[str, int]] = []
        for word, count in word_freq.items():
            if count < min_count:
                continue
            lower = word.lower()
            if lower in stop_words:
                continue
            if word in current_vocab:
                continue
            candidates.append((word, count))

        # Сортируем по частоте (desc), потом по длине (desc) для стабильности
        candidates.sort(key=lambda x: (-x[1], -len(x[0]), x[0]))
        top = candidates[:top_k]

        return {
            "suggestions": [{"word": w, "count": c} for w, c in top],
            "total_candidates": len(candidates),
            "scanned_items": len(items),
            "current_vocabulary_size": len(current_vocab),
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

    def _handle_get_audio_devices(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает список доступных входных аудиоустройств (обёртка для GUI)."""
        return {"devices": self._list_audio_inputs()}

    def _handle_test_microphone(self, params: dict[str, Any]) -> dict[str, Any]:
        """Записывает короткий фрагмент аудио и возвращает RMS/peak уровни."""
        import numpy as np

        duration = min(float(params.get("duration_sec", 2.0)), 5.0)
        try:
            import sounddevice as sd  # type: ignore

            sample_rate = 16000
            frames = int(duration * sample_rate)
            audio_data = sd.rec(frames, samplerate=sample_rate, channels=1, dtype="float32")
            sd.wait()
            audio_flat = audio_data.flatten()
            rms = float(np.sqrt(np.mean(audio_flat ** 2)))
            peak = float(np.max(np.abs(audio_flat)))
            return {
                "ok": True,
                "duration_sec": duration,
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "devices": self._list_audio_inputs(),
            }
        except Exception as exc:
            logger.warning("test_microphone: ошибка записи — %s", exc)
            return {
                "ok": False,
                "error": str(exc),
                "devices": self._list_audio_inputs(),
            }

    def _handle_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        settings = self._cached_settings()
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

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected)
        if not audio_paths:
            return {"items": [], "processed": 0, "errors": ["Не найдено аудиофайлов для транскрибации"]}

        # Загружаем пользовательский vocabulary для подсказок Whisper
        user_vocabulary = self.store.load_vocabulary() or []

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for audio_path in audio_paths:
            started_at = time.monotonic()
            try:
                # Determine audio file duration before transcription
                audio_duration_sec: float | None = None
                try:
                    import soundfile as sf
                    sf_info = sf.info(audio_path)
                    audio_duration_sec = round(sf_info.duration, 3)
                except Exception:
                    pass  # Non-critical: duration is informational

                # For file imports, default to auto-detect if no explicit hint
                import_lang_hint = lang_hint if lang_hint else "auto"
                transcribe_payload = self.transcriber.transcribe(
                    audio_path,
                    quality_profile=quality_profile,
                    cleanup_profile=cleanup_profile,
                    lang_hint=import_lang_hint,
                    extra_vocabulary=user_vocabulary if user_vocabulary else None,
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
                diarization_data = transcribe_payload.get("diarization") if isinstance(transcribe_payload, dict) else None
                detected_lang = transcribe_payload.get("language", "?") if isinstance(transcribe_payload, dict) else "?"

                translation = self.translator.translate(
                    text=text,
                    mode=translation_mode,
                    network_mode=network_mode,
                    translation_style=translation_style,
                    glossary=translation_glossary,
                )
                translated_text = translation.text.strip() if translation.ok else ""
                final_text = translated_text if (translate_and_paste and translated_text) else text

                # Format text with speaker labels if diarization produced multiple speakers
                display_text = self._format_text_with_speakers(final_text, diarization_data)

                history_item = self.store.add_history_item(
                    text=display_text,
                    paste_status="failed",
                    source_text=text,
                    translated_text=translated_text,
                    translation_mode=translation.mode,
                    source_lang=translation.source_lang,
                    target_lang=translation.target_lang,
                    translation_status=translation.status,
                    translation_engine=translation.engine,
                    diarization=diarization_data,
                    audio_duration_sec=audio_duration_sec,
                )

                # Auto-summary для длинных транскрипций (>500 символов)
                summary: str | None = None
                if len(final_text) > 500:
                    summary = self._generate_summary(final_text)

                # Save transcript to file
                try:
                    transcripts_dir = Path(self.store.data_dir) / "transcripts"
                    transcripts_dir.mkdir(exist_ok=True)
                    source_name = Path(audio_path).stem
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    transcript_filename = f"{timestamp}_{source_name}.md"
                    transcript_path = transcripts_dir / transcript_filename
                    with open(transcript_path, "w", encoding="utf-8") as f:
                        f.write(f"# Транскрипт: {Path(audio_path).name}\n\n")
                        f.write(f"- Дата: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        if audio_duration_sec is not None:
                            _mins = int(audio_duration_sec) // 60
                            _secs = audio_duration_sec - _mins * 60
                            f.write(f"- Аудио: {_mins}м {_secs:.1f}с\n")
                        f.write(f"- Обработка: {elapsed:.1f}с\n")
                        f.write(f"- Источник: {audio_path}\n")
                        f.write(f"- Язык: {detected_lang}\n")
                        diar_info = transcribe_payload.get("diarization", {}) if isinstance(transcribe_payload, dict) else {}
                        if diar_info and diar_info.get("enabled"):
                            speakers = diar_info.get("speaker_turns", [])
                            unique_speakers = len(set(t.get("speaker") for t in speakers))
                            f.write(f"- Спикеры: {unique_speakers}\n")
                        if summary:
                            f.write(f"\n## Краткое содержание\n\n{summary}\n")
                        # Use speaker-labeled text if diarization is active
                        if diar_info and diar_info.get("enabled") and diar_info.get("speaker_turns"):
                            f.write(f"\n## Диалог\n\n{display_text}\n")
                        else:
                            f.write(f"\n## Текст\n\n{final_text}\n")
                        if translated_text:
                            f.write(f"\n## Перевод ({translation.mode})\n\n{translated_text}\n")
                except Exception as exc:
                    logger.warning("Не удалось сохранить транскрипт в файл: %s", exc)

                item_result: dict[str, Any] = {
                    "path": audio_path,
                    "text": display_text,
                    "original_text": text,
                    "translated_text": translated_text,
                    "translation_mode": translation.mode,
                    "translation_style": translation_style,
                    "translation_status": translation.status,
                    "source_lang": translation.source_lang,
                    "target_lang": translation.target_lang,
                    "history_id": history_item.id,
                    "duration_sec": elapsed,
                    "audio_duration_sec": audio_duration_sec,
                    "language": detected_lang,
                }
                if summary:
                    item_result["summary"] = summary
                items.append(item_result)
            except Exception as exc:
                err_msg = str(exc)
                file_name = Path(audio_path).name
                if "Resource deadlock" in err_msg:
                    err_msg = f"Файл заблокирован (возможно iCloud): {file_name}"
                elif "timeout" in err_msg.lower():
                    err_msg = f"Превышено время транскрибации: {file_name}"
                elif "No such file" in err_msg:
                    err_msg = f"Файл не найден: {file_name}"
                elif "Permission denied" in err_msg:
                    err_msg = f"Нет доступа к файлу: {file_name}"
                elif "too large" in err_msg.lower() or "MAX_AUDIO_MB" in err_msg:
                    err_msg = f"Файл слишком большой: {file_name}"
                elif "Unsupported" in err_msg or "codec" in err_msg.lower():
                    err_msg = f"Неподдерживаемый формат аудио: {file_name}"
                else:
                    err_msg = f"{file_name}: {err_msg}"
                errors.append(err_msg)

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

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [r.resolve() for r in (self.store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
            else:
                return {"items": [], "processed": 0, "errors": [f"Path outside allowed directories: {resolved}"]}
        audio_paths = self._collect_audio_paths(selected)
        sample_limit = int(params.get("sample_limit", 5) or 5)
        safe_sample_limit = max(1, min(sample_limit, 50))
        by_ext: dict[str, int] = {}
        total_bytes = 0
        # Группировка по родительской папке для отображения структуры.
        by_folder: dict[str, int] = {}
        for audio_path in audio_paths:
            suffix = Path(audio_path).suffix.lower() or "<none>"
            by_ext[suffix] = by_ext.get(suffix, 0) + 1
            folder = str(Path(audio_path).parent)
            by_folder[folder] = by_folder.get(folder, 0) + 1
            try:
                total_bytes += Path(audio_path).stat().st_size
            except FileNotFoundError:
                continue
        return {
            "input_count": len(selected),
            "audio_count": len(audio_paths),
            "folder_count": len(by_folder),
            "by_folder": by_folder,
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
                # Сортируем по пути, чтобы части записей звонков
                # (part1.m4a, part2.m4a, ...) шли в правильном порядке.
                candidates = sorted(
                    (c for c in path.rglob("*") if c.is_file() and c.suffix.lower() in audio_ext),
                    key=lambda c: str(c),
                )
                result.extend(str(c.resolve()) for c in candidates)

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
        # Adaptive backoff: увеличивается при пустых результатах, сбрасывается при речи.
        poll_interval = 0.35
        _POLL_MIN = 0.35
        _POLL_MAX = 1.5
        _POLL_BACKOFF = 1.5

        while not self._preview_stop_event.is_set():
            if not bool(getattr(self.recorder, "is_recording", False)):
                break

            if not callable(snapshot_audio):
                self._preview_stop_event.wait(poll_interval)
                continue

            try:
                audio_data, duration_sec = snapshot_audio(max_duration_sec=12.0)
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка snapshot_audio")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue

            with self._preview_lock:
                self._preview_duration_sec = float(duration_sec)

            current_size = int(getattr(audio_data, "size", 0))
            if current_size < min_samples:
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue
            # Важный нюанс: после достижения лимита snapshot-а размер буфера стабилизируется.
            # Поэтому ориентируемся на прогресс времени записи, а не на size.
            if duration_sec - last_refresh_duration < 0.9:
                self._preview_stop_event.wait(_POLL_MIN)
                continue

            try:
                preview_payload = self.transcriber.transcribe_preview(
                    audio_data,
                    quality_profile=quality_profile,
                )
                preview_text = self._extract_transcribed_text(preview_payload)
                preview_text = self._postprocess_preview_text(preview_text)
            except Exception:
                self._preview_error_count += 1
                logger.exception("Realtime preview: ошибка transcribe_preview")
                if self._preview_error_count > 10:
                    logger.warning(
                        "Realtime preview: %d ошибок подряд, возможна системная проблема",
                        self._preview_error_count,
                    )
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
                self._preview_stop_event.wait(poll_interval)
                continue

            self._preview_error_count = 0
            if preview_text:
                with self._preview_lock:
                    self._preview_text = preview_text[-900:]
                    self._preview_updated_at = float(duration_sec)
                event_bus.emit_typed(EventType.STT_PARTIAL, SttPartial(
                    text=preview_text[-900:],
                    duration_sec=float(duration_sec),
                ))
                poll_interval = _POLL_MIN
            else:
                with self._preview_lock:
                    self._preview_text = ""
                    self._preview_updated_at = float(duration_sec)
                poll_interval = min(poll_interval * _POLL_BACKOFF, _POLL_MAX)
            last_refresh_duration = float(duration_sec)
            self._preview_stop_event.wait(poll_interval)

    @staticmethod
    def _list_audio_inputs() -> list[dict[str, Any]]:
        """Пытается безопасно получить список входных аудиоустройств."""
        try:
            import sounddevice as sd  # type: ignore
        except Exception as exc:
            logger.warning("Failed to list audio inputs: %s", exc)
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
    def _coerce_bounded(value: Any, default: int | float, min_value: int | float, max_value: int | float) -> int | float:
        """Нормализует числовое значение в допустимый диапазон. Тип определяется default."""
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    # Aliases for backward compatibility with existing call sites
    _coerce_bounded_int = _coerce_bounded
    _coerce_bounded_float = _coerce_bounded

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
        os.chmod(str(self.socket_path), 0o600)
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
    import json as _json
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "backend.log"

    if settings.LOG_FORMAT == "json":
        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return _json.dumps({
                    "ts": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                })
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_path, encoding="utf-8"),
    ]
    for h in handlers:
        h.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers)


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
