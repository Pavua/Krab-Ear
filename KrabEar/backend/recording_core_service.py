"""RecordingCoreService — recording lifecycle + transcribe orchestration.

Extracted from BackendService Wave 331 (sister to CallSessionService PR #420).
~600+ LOC moved, including:
  - handle_start_recording
  - handle_stop_recording (orchestrator + 5 phase helpers)
  - handle_get_recording_state
  - handle_transcribe_paths_async
  - handle_get_transcribe_progress
  - handle_cancel_transcribe_job
  - handle_preview_transcribe_paths
  + audio quality guard helpers, coerce helpers, text extraction/postprocess
"""

from __future__ import annotations

import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

from backend.event_bus import bus as event_bus
from backend.models import DEFAULT_SETTINGS
from backend.observability import add_breadcrumb
from contracts.registry import EventType
from contracts.stt_events import SttFailed, SttFinal
from contracts.translation_events import TranslationCompleted, TranslationFailed
from core.utils import TextUtils

logger = logging.getLogger("KrabEar.Backend.RecordingCore")


class RecordingCoreService:
    """Обработчики IPC для recording lifecycle и batch-транскрибации.

    Принимает в конструкторе все необходимые collaborators; не имеет
    прямой зависимости на BackendService — только интерфейсы.
    """

    def __init__(
        self,
        *,
        recorder: Any,
        transcriber: Any,
        translator: Any,
        store: Any,
        vocabulary: Any,
        job_tracker: Any,
        settings_svc: Any,
        auto_glossary: Any,
        context_memory: Any,
        semantic_searcher: Any,
        auto_backup: Any,
        action_items_extractor: Any,
        transcript_writer_cls: Any,
        clipboard_history: list,
        get_preview_state_fn: Callable[[], tuple[str, float]],
        start_preview_fn: Callable,  # fn(quality_profile: str) -> None
        stop_preview_fn: Callable,  # fn() -> None
        reset_preview_fn: Callable,  # fn() -> None
        cached_settings_fn: Callable,  # fn() -> dict
        get_runtime_setting_fn: Callable,  # fn(key, default) -> value
        generate_summary_fn: Callable,  # fn(text: str) -> str | None
        format_text_with_speakers_fn: Callable,  # fn(text, diarization) -> str
    ) -> None:
        self._recorder = recorder
        self._transcriber = transcriber
        self._translator = translator
        self._store = store
        self._vocabulary = vocabulary
        self._job_tracker = job_tracker
        self._settings_svc = settings_svc
        self._auto_glossary = auto_glossary
        self._context_memory = context_memory
        self._semantic_searcher = semantic_searcher
        self._auto_backup = auto_backup
        self._action_items_extractor = action_items_extractor
        self._transcript_writer_cls = transcript_writer_cls
        self._clipboard_history = clipboard_history
        self._get_preview_state_fn = get_preview_state_fn
        self._start_preview_fn = start_preview_fn
        self._stop_preview_fn = stop_preview_fn
        self._reset_preview_fn = reset_preview_fn
        self._cached_settings_fn = cached_settings_fn
        self._get_runtime_setting_fn = get_runtime_setting_fn
        self._generate_summary_fn = generate_summary_fn
        self._format_text_with_speakers_fn = format_text_with_speakers_fn

        # Service-owned mutable state
        self._rt_partial: Any = None
        self._rt_session_id: str = ""
        self._last_stt_engine: str | None = None
        self._transcription_counter: int = 0

    # ------------------------------------------------------------------ #
    # Public IPC handlers                                                  #
    # ------------------------------------------------------------------ #

    def handle_start_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запускает запись. Идемпотентен — повторный start не считается ошибкой."""
        started = self._recorder.start()
        if not started:
            preview_text, preview_duration = self._get_preview_state_fn()
            return {
                "status": "already_recording",
                "is_recording": True,
                "duration_sec": preview_duration,
                "preview_text": preview_text,
            }
        self._reset_preview_fn()
        settings = self._cached_settings_fn()
        try:
            brain_model = str(settings.get("llm_brain_model", "")).strip()
            unload_enabled = bool(settings.get("llm_brain_unload_on_recording", True))
            if brain_model and unload_enabled:
                from backend.lm_studio_lifecycle import unload_model_async
                base_url = str(settings.get("llm_base_url", "http://localhost:1234/v1"))
                unload_model_async(base_url, brain_model)
        except Exception as exc:
            logger.debug("LM Studio brain unload hook failed: %s", exc)
        add_breadcrumb(
            category="recording",
            message="started",
            level="info",
            data={"quality_profile": str(settings.get("quality_profile", "balanced"))},
        )
        if bool(settings.get("realtime_preview_enabled", True)):
            quality_profile = str(settings.get("quality_profile", "balanced"))
            self._start_preview_fn(quality_profile)
        if bool(settings.get("realtime_partial_enabled", True)):
            import uuid as _uuid
            self._rt_session_id = _uuid.uuid4().hex
            _interval = float(settings.get("rt_partial_interval_sec", 3.0))
            _buffer = float(settings.get("rt_partial_buffer_sec", 8.0))
            _sample_rate = int(getattr(self._recorder, "sample_rate", 16000))
            try:
                from backend.realtime_partial import RealtimePartialTranscriber
                self._rt_partial = RealtimePartialTranscriber(
                    transcriber=self._transcriber,
                    recorder=self._recorder,
                    event_bus=event_bus,
                    interval_sec=_interval,
                    buffer_sec=_buffer,
                )
                self._rt_partial.start(
                    session_id=self._rt_session_id,
                    sample_rate=_sample_rate,
                )
            except Exception:
                logger.exception("Не удалось запустить RealtimePartialTranscriber")
                self._rt_partial = None
        return {"status": "recording"}

    def handle_stop_recording(self, params: dict[str, Any]) -> dict[str, Any]:
        """Orchestrate the stop-recording pipeline via 5 phase helpers."""
        settings = self._cached_settings_fn()

        phase_a = self._stop_recording_phase_a(params, settings)
        if "early_return" in phase_a:
            return phase_a["early_return"]

        audio = phase_a["audio"]
        duration_sec = phase_a["duration_sec"]
        stop_tail_trim_ms = phase_a["stop_tail_trim_ms"]
        _rt_session_id = phase_a["rt_session_id"]
        sr = phase_a["sr"]

        phase_b = self._stop_recording_phase_b(audio, duration_sec, stop_tail_trim_ms, sr)
        if "early_return" in phase_b:
            return phase_b["early_return"]

        silence_detected = phase_b["silence_detected"]
        background_guard_rejected = phase_b["background_guard_rejected"]

        phase_c = self._stop_recording_phase_c(audio, duration_sec, sr)
        transcribe_payload = phase_c["transcribe_payload"]

        phase_d = self._stop_recording_phase_d(
            transcribe_payload=transcribe_payload,
            duration_sec=duration_sec,
            sr=sr,
            stop_tail_trim_ms=stop_tail_trim_ms,
            silence_detected=silence_detected,
            silence_guard_enabled=sr["silence_guard_enabled"],
            background_guard_rejected=background_guard_rejected,
        )
        if "early_return" in phase_d:
            return phase_d["early_return"]

        return self._stop_recording_phase_e(
            phase_d=phase_d,
            sr=sr,
            duration_sec=duration_sec,
            stop_tail_trim_ms=stop_tail_trim_ms,
            silence_detected=silence_detected,
            silence_guard_enabled=sr["silence_guard_enabled"],
            background_guard_rejected=background_guard_rejected,
            rt_session_id=_rt_session_id,
            settings=settings,
        )

    def handle_get_recording_state(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние записи."""
        preview_text, preview_duration = self._get_preview_state_fn()
        audio_rms = (
            self._recorder.snapshot_rms()
            if hasattr(self._recorder, "snapshot_rms")
            else 0.0
        )
        session_id = "__live__"
        elapsed_sec = 0.0
        if hasattr(self._recorder, "get_duration_sec"):
            try:
                elapsed_sec = float(self._recorder.get_duration_sec() or 0.0)
            except Exception:
                elapsed_sec = preview_duration or 0.0
        return {
            "is_recording": bool(getattr(self._recorder, "is_recording", False)),
            "duration_sec": preview_duration,
            "preview_text": preview_text,
            "audio_rms": audio_rms,
            "elapsed_sec": elapsed_sec,
            "session_id": session_id,
        }

    def handle_transcribe_paths_async(self, params: dict[str, Any]) -> dict[str, Any]:
        """Асинхронный вариант транскрибации: возвращает job_id сразу."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")
        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [
            r.resolve()
            for r in (self._store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
        selected: list[str] = []
        for p in selected_raw:
            resolved = Path(p).expanduser().resolve()
            if any(str(resolved).startswith(str(root)) for root in allowed_roots):
                selected.append(str(resolved))
        try:
            audio_paths = self._collect_audio_paths(selected) if selected else []
        except Exception:
            audio_paths = []
        total_files = len(audio_paths)

        job_id = self._job_tracker.create_job(total_files=total_files)
        job_params = dict(params)

        def _emit_status(
            op: str,
            stage: str = "",
            progress: float | None = None,
            current_file: str | None = None,
            file_index: int | None = None,
        ) -> None:
            payload: dict[str, Any] = {
                "op": op, "stage": stage,
                "total_files": total_files, "ts": time.time(),
            }
            if progress is not None:
                payload["progress"] = progress
            if current_file is not None:
                payload["current_file"] = current_file
            if file_index is not None:
                payload["file_index"] = file_index
            event_bus.emit("app.status", payload)

        def _on_file_start(index: int, audio_path: str) -> None:
            self._job_tracker.update(
                job_id, status="running",
                current_file=Path(audio_path).name,
                current_stage="idle", file_index=index + 1,
            )
            _emit_status("transcribe_job", stage="idle",
                         progress=index / total_files if total_files else 0.0,
                         current_file=Path(audio_path).name, file_index=index + 1)

        def _on_file_done(index: int, item: dict[str, Any] | None, err: str | None) -> None:
            state = self._job_tracker.get(job_id) or {}
            new_items = list(state.get("items") or [])
            new_errors = list(state.get("errors") or [])
            if item is not None:
                new_items.append(item)
            if err is not None:
                new_errors.append(err)
            self._job_tracker.update(
                job_id, items=new_items, errors=new_errors,
                processed=len(new_items),
            )
            _emit_status("transcribe_job", stage="idle",
                         progress=(index + 1) / total_files if total_files else 1.0,
                         file_index=index + 1)

        def _progress_callback(stage: str) -> None:
            self._job_tracker.update(job_id, current_stage=str(stage))
            state = self._job_tracker.get(job_id) or {}
            fi = state.get("file_index") or 0
            _emit_status("transcribe_job", stage=str(stage),
                         progress=max(0, fi - 1) / total_files if total_files else 0.0,
                         file_index=fi)

        def _cancel_check() -> bool:
            state = self._job_tracker.get(job_id)
            return bool(state and state.get("cancel_requested"))

        def _worker() -> None:
            try:
                self._job_tracker.update(job_id, status="running")
                _emit_status("transcribe_job", stage="started", progress=0.0)
                result = self._transcribe_paths_core(
                    job_params,
                    progress_callback=_progress_callback,
                    cancel_check=_cancel_check,
                    on_file_start=_on_file_start,
                    on_file_done=_on_file_done,
                )
                state = self._job_tracker.get(job_id) or {}
                if state.get("cancel_requested"):
                    _emit_status("idle", stage="", progress=1.0)
                    self._job_tracker.update(
                        job_id, status="cancelled",
                        items=list(result.get("items") or []),
                        errors=list(result.get("errors") or []),
                        processed=len(result.get("items") or []),
                        current_stage="idle", finished_at=time.monotonic(),
                    )
                else:
                    _emit_status("idle", stage="", progress=1.0)
                    self._job_tracker.mark_done(
                        job_id,
                        items=list(result.get("items") or []),
                        errors=list(result.get("errors") or []),
                    )
            except Exception as exc:
                logger.exception("Async transcribe job %s упал", job_id)
                _emit_status("idle", stage="", progress=1.0)
                self._job_tracker.mark_failed(job_id, str(exc))

        thread = threading.Thread(target=_worker, name=f"transcribe-{job_id}", daemon=True)
        thread.start()
        return {"job_id": job_id}

    def handle_get_transcribe_progress(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает текущее состояние async-job'а."""
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        state = self._job_tracker.get(job_id)
        if state is None:
            raise RuntimeError(f"Неизвестный job_id: {job_id}")

        status = str(state.get("status") or "queued")
        items_raw = list(state.get("items") or [])
        items_out = items_raw if status in ("done", "failed", "cancelled") else []

        elapsed_sec = float(state.get("elapsed_sec") or 0.0)
        eta_sec: float | None = None
        total_audio = 0.0
        for it in items_raw:
            dur = it.get("audio_duration_sec") if isinstance(it, dict) else None
            if isinstance(dur, (int, float)):
                total_audio += float(dur)
        if total_audio > 0:
            eta_sec = max(0.0, total_audio * 10.0 - elapsed_sec)

        return {
            "status": status,
            "current_file": str(state.get("current_file") or ""),
            "current_stage": str(state.get("current_stage") or "idle"),
            "file_index": int(state.get("file_index") or 0),
            "total_files": int(state.get("total_files") or 0),
            "elapsed_sec": round(elapsed_sec, 3),
            "eta_sec": round(eta_sec, 3) if eta_sec is not None else None,
            "processed": int(state.get("processed") or 0),
            "errors": list(state.get("errors") or []),
            "items": items_out,
        }

    def handle_cancel_transcribe_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сигнализирует воркеру об отмене job'а."""
        job_id = str(params.get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("Параметр job_id обязателен")
        cancelled = self._job_tracker.cancel(job_id)
        return {"cancelled": bool(cancelled)}

    def handle_preview_transcribe_paths(self, params: dict[str, Any]) -> dict[str, Any]:
        """Быстрый предпросмотр импорта: считает аудиофайлы без транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        selected_raw = [str(item).strip() for item in raw_paths if str(item).strip()]
        allowed_roots = [
            r.resolve()
            for r in (self._store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
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

    # ------------------------------------------------------------------ #
    # Phase helpers (stop_recording pipeline)                              #
    # ------------------------------------------------------------------ #

    def _stop_recording_phase_a(
        self, params: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        """Stop preview worker, stop realtime partial, stop recorder.

        Returns a dict that always contains either:
          - ``{"early_return": <response dict>}`` — caller should return immediately, OR
          - keys: audio, duration_sec, stop_tail_trim_ms, rt_session_id, sr
        """
        self._stop_preview_fn()
        rt_session_id = self._rt_session_id
        if self._rt_partial is not None:
            try:
                self._rt_partial.stop()
            except Exception:
                logger.exception("Ошибка при остановке RealtimePartialTranscriber")
            finally:
                self._rt_partial = None

        stop_tail_trim_ms = self._coerce_bounded(
            value=params.get("stop_tail_trim_ms", settings.get("stop_tail_trim_ms", 180)),
            default=180, min_value=0, max_value=1200,
        )
        stopped = self._stop_recorder_guarded(stop_tail_trim_ms=stop_tail_trim_ms)
        if stopped is None:
            preview_text, preview_duration = self._get_preview_state_fn()
            return {
                "early_return": {
                    "status": "already_stopped",
                    "is_recording": False,
                    "duration_sec": preview_duration,
                    "preview_text": preview_text,
                    "stop_tail_trim_ms": stop_tail_trim_ms,
                }
            }

        audio, duration_sec = stopped
        add_breadcrumb(
            category="recording", message="stopped", level="info",
            data={"duration_sec": round(float(duration_sec), 2)},
        )

        try:
            brain_model = str(settings.get("llm_brain_model", "")).strip()
            preload_enabled = bool(settings.get("llm_brain_preload_on_stop", True))
            if brain_model and preload_enabled:
                from backend.lm_studio_lifecycle import load_model_async
                base_url = str(settings.get("llm_base_url", "http://localhost:1234/v1"))
                load_model_async(base_url, brain_model)
        except Exception as exc:
            logger.debug("LM Studio brain preload hook failed: %s", exc)

        sr = self._load_stop_recording_settings(params, settings)

        if getattr(audio, "size", 0) == 0:
            return {
                "early_return": self._build_empty_audio_response(
                    duration_sec=duration_sec,
                    quality_profile=sr["quality_profile"],
                    cleanup_profile=sr["cleanup_profile"],
                    translation_mode=sr["translation_mode"],
                    translate_and_paste=sr["translate_and_paste"],
                    stop_tail_trim_ms=stop_tail_trim_ms,
                )
            }

        return {
            "audio": audio,
            "duration_sec": duration_sec,
            "stop_tail_trim_ms": stop_tail_trim_ms,
            "rt_session_id": rt_session_id,
            "sr": sr,
        }

    def _stop_recording_phase_b(
        self,
        audio: Any,
        duration_sec: float,
        stop_tail_trim_ms: int,
        sr: dict[str, Any],
    ) -> dict[str, Any]:
        """Run silence guard and background guard."""
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]
        translation_mode = sr["translation_mode"]
        translate_and_paste = sr["translate_and_paste"]
        sample_rate = sr["sample_rate"]

        silence_detected = False
        if sr["silence_guard_enabled"]:
            silence_detected = self._looks_like_silence_audio(
                audio=audio, sample_rate=sample_rate,
                rms_threshold=sr["silence_rms_threshold"],
                peak_threshold=sr["silence_peak_threshold"],
                active_ratio_threshold=sr["silence_active_ratio_threshold"],
            )
            if silence_detected:
                logger.info(
                    "Silence guard: stop_recording классифицирован как тишина, STT пропущен",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "rms_threshold": sr["silence_rms_threshold"],
                        "peak_threshold": sr["silence_peak_threshold"],
                        "active_ratio_threshold": sr["silence_active_ratio_threshold"],
                    },
                )
                return {
                    "early_return": self._build_empty_audio_response(
                        duration_sec=duration_sec,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        translation_mode=translation_mode,
                        translate_and_paste=translate_and_paste,
                        stop_tail_trim_ms=stop_tail_trim_ms,
                        silence_detected=True,
                        silence_guard_enabled=True,
                    )
                }

        background_guard_rejected = False
        if sr["background_guard_enabled"]:
            background_guard_rejected = self._looks_like_distant_background_speech(
                audio=audio, sample_rate=sample_rate,
                min_peak=sr["background_guard_min_peak"],
                min_rms=sr["background_guard_min_rms"],
                uniform_frame_threshold=sr["background_guard_uniform_frame_threshold"],
                max_uniform_active_ratio=sr["background_guard_max_uniform_active_ratio"],
            )
            if background_guard_rejected:
                logger.info(
                    "Background guard: stop_recording отклонен как фоновая речь",
                    extra={
                        "duration_sec": round(float(duration_sec), 3),
                        "min_peak": sr["background_guard_min_peak"],
                        "min_rms": sr["background_guard_min_rms"],
                        "uniform_frame_threshold": sr["background_guard_uniform_frame_threshold"],
                        "max_uniform_active_ratio": sr["background_guard_max_uniform_active_ratio"],
                    },
                )
                return {
                    "early_return": self._build_empty_audio_response(
                        duration_sec=duration_sec,
                        quality_profile=quality_profile,
                        cleanup_profile=cleanup_profile,
                        translation_mode=translation_mode,
                        translate_and_paste=translate_and_paste,
                        stop_tail_trim_ms=stop_tail_trim_ms,
                        silence_guard_enabled=sr["silence_guard_enabled"],
                        background_guard_rejected=True,
                    )
                }

        return {
            "silence_detected": silence_detected,
            "background_guard_rejected": background_guard_rejected,
        }

    def _stop_recording_phase_c(
        self,
        audio: Any,
        duration_sec: float,
        sr: dict[str, Any],
    ) -> dict[str, Any]:
        """Load vocabulary/context/glossary and run the transcriber."""
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]
        lang_hint: str | None = sr["lang_hint"]

        user_vocabulary = self._vocabulary.load() or []

        _recent_history, _ = self._store.get_history_page(cursor=None, limit=10)
        _cached_settings_hw = self._settings_svc.cached_settings()
        _stt_hotwords_enabled = bool(_cached_settings_hw.get("stt_hotwords_enabled", True))
        _stt_hotwords: list[str] = (
            _cached_settings_hw.get("stt_hotwords", []) if _stt_hotwords_enabled else []
        )

        _auto_glossary_terms: list[str] = []
        _cached_settings_ag = self._settings_svc.cached_settings()
        _ag_window_days = int(_cached_settings_ag.get("auto_glossary_window_days", DEFAULT_SETTINGS.get("auto_glossary_window_days", 7)))
        _ag_top_n = int(_cached_settings_ag.get("auto_glossary_top_n", DEFAULT_SETTINGS.get("auto_glossary_top_n", 30)))
        if _cached_settings_ag.get("auto_glossary_enabled", DEFAULT_SETTINGS.get("auto_glossary_enabled", True)):
            try:
                _auto_glossary_terms = self._auto_glossary.build(
                    window_days=_ag_window_days, top_n=_ag_top_n
                )
            except Exception as _ag_exc:
                logger.warning("auto_glossary: ошибка при построении глоссария: %s", _ag_exc)

        _combined_hotwords: list[str] | None = None
        if _stt_hotwords or _auto_glossary_terms:
            _seen_hw: set[str] = set()
            _combined_hw: list[str] = []
            for _w in list(_stt_hotwords) + list(_auto_glossary_terms):
                _w = _w.strip()
                if _w and _w.lower() not in _seen_hw:
                    _seen_hw.add(_w.lower())
                    _combined_hw.append(_w)
            _combined_hotwords = _combined_hw if _combined_hw else None

        add_breadcrumb(
            category="transcription", message="transcribe_start", level="info",
            data={
                "quality_profile": quality_profile,
                "audio_len_sec": round(float(duration_sec), 2),
                "lang_hint": lang_hint or "auto",
                "auto_glossary_terms": len(_auto_glossary_terms),
            },
        )

        transcribe_payload = self._transcriber.transcribe(
            audio,
            quality_profile=quality_profile,
            cleanup_profile=cleanup_profile,
            lang_hint=lang_hint,
            extra_vocabulary=user_vocabulary if user_vocabulary else None,
            history_context=_recent_history if _recent_history else None,
            stt_hotwords=_combined_hotwords,
        )

        return {"transcribe_payload": transcribe_payload}

    def _stop_recording_phase_d(
        self,
        transcribe_payload: Any,
        duration_sec: float,
        sr: dict[str, Any],
        stop_tail_trim_ms: int,
        silence_detected: bool,
        silence_guard_enabled: bool,
        background_guard_rejected: bool,
    ) -> dict[str, Any]:
        """Extract text, apply soft-cleanup retry, translate, diarize."""
        translation_mode = sr["translation_mode"]
        translation_style = sr["translation_style"]
        translation_glossary = sr["translation_glossary"]
        translate_and_paste = sr["translate_and_paste"]
        network_mode = sr["network_mode"]
        quality_profile = sr["quality_profile"]
        cleanup_profile = sr["cleanup_profile"]

        text = self._postprocess_transcribed_text(self._extract_transcribed_text(transcribe_payload))
        transcription_error = self._extract_transcribed_error(transcribe_payload)

        if not text and not transcription_error:
            raw_text = str(self._extract_transcribed_text(transcribe_payload) or "").strip()
            if len(raw_text) >= 30 and duration_sec >= 8.0:
                logger.warning(
                    "Retry transcribe с soft cleanup: raw_text len=%d, duration=%.1fs",
                    len(raw_text), duration_sec,
                )
                text = TextUtils.normalize_phrase(raw_text).strip()
                text = re.sub(r"\s+([,.;:!?])", r"\1", text)
                text = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", text)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    first_alpha = next((i for i, c in enumerate(text) if c.isalpha()), -1)
                    if first_alpha >= 0:
                        text = text[:first_alpha] + text[first_alpha].upper() + text[first_alpha + 1:]
                    if not re.search(r"[.!?…]$", text):
                        text = f"{text}."

        if not text:
            if transcription_error:
                event_bus.emit_typed(EventType.STT_FAILED, SttFailed(reason=transcription_error, duration_sec=duration_sec))
            return {
                "early_return": {
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
            }

        translation = self._translator.translate(
            text=text, mode=translation_mode, network_mode=network_mode,
            translation_style=translation_style, glossary=translation_glossary,
        )
        translated_text = translation.text.strip() if translation.ok else ""
        final_text = translated_text if (translate_and_paste and translated_text) else text
        translation_status = translation.status
        if translation.ok and translated_text:
            event_bus.emit_typed(EventType.TRANSLATION_COMPLETED, TranslationCompleted(
                history_id="",
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
        if tp.get("engine"):
            self._last_stt_engine = str(tp["engine"])
        confidence = tp.get("confidence", 0.0)
        add_breadcrumb(
            category="transcription", message="transcribe_complete", level="info",
            data={"confidence": round(float(confidence), 3), "word_count": len(text.split()) if text else 0},
        )
        if confidence < 0.4 and text:
            logger.warning("Низкая уверенность STT: %.2f", confidence)
        diarization_data = tp.get("diarization")

        display_text = self._format_text_with_speakers_fn(final_text, diarization_data)

        return {
            "text": text,
            "display_text": display_text,
            "translated_text": translated_text,
            "final_text": final_text,
            "translation": translation,
            "translation_status": translation_status,
            "confidence": confidence,
            "diarization_data": diarization_data,
            "tp": tp,
        }

    def _stop_recording_phase_e(
        self,
        phase_d: dict[str, Any],
        sr: dict[str, Any],
        duration_sec: float,
        stop_tail_trim_ms: int,
        silence_detected: bool,
        silence_guard_enabled: bool,
        background_guard_rejected: bool,
        rt_session_id: str | None,
        settings: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist history item, update side-caches, build final result dict."""
        text = phase_d["text"]
        display_text = phase_d["display_text"]
        translated_text = phase_d["translated_text"]
        final_text = phase_d["final_text"]
        translation = phase_d["translation"]
        translation_status = phase_d["translation_status"]
        confidence = phase_d["confidence"]
        diarization_data = phase_d["diarization_data"]
        tp = phase_d["tp"]

        item = self._store.add_history_item(
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
            audio_duration_sec=duration_sec if duration_sec else None,
            confidence=confidence if confidence else None,
            emotion=tp.get("emotion") if isinstance(tp.get("emotion"), str) else None,
            word_timestamps=tp.get("word_timestamps") if isinstance(tp.get("word_timestamps"), list) else None,
            speaker_turns=tp.get("speaker_turns") if isinstance(tp.get("speaker_turns"), list) else None,
        )
        self._clipboard_history.append({
            "text": final_text,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "history_id": item.id,
        })
        if len(self._clipboard_history) > 20:
            del self._clipboard_history[:-20]

        try:
            self._context_memory.update(text)
        except Exception:
            pass

        if self._semantic_searcher.is_enabled and settings.SEMANTIC_SEARCH_AUTO_INDEX:
            _index_text = display_text or text
            _index_id = item.id
            threading.Thread(
                target=self._semantic_searcher.index_item,
                args=(_index_id, _index_text),
                daemon=True, name="semantic-index",
            ).start()

        self._transcription_counter += 1
        if self._transcription_counter % 100 == 0:
            try:
                self._auto_backup.check_and_backup()
            except Exception:
                pass

        result_payload = {
            "status": "ok",
            "duration_sec": duration_sec,
            "quality_profile": sr["quality_profile"],
            "cleanup_profile": sr["cleanup_profile"],
            "translation_mode": translation.mode,
            "translation_style": sr["translation_style"],
            "translate_and_paste": sr["translate_and_paste"],
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
        if rt_session_id:
            try:
                event_bus.emit(
                    "realtime.final_transcript",
                    {"session_id": rt_session_id, "text": final_text,
                     "is_partial": False, "ts": time.time()},
                )
            except Exception:
                logger.debug("Не удалось emit realtime.final_transcript", exc_info=True)

        if self._coerce_bool(settings.get("auto_save_transcripts", False), default=False):
            try:
                transcripts_dir = Path(self._store.data_dir) / "transcripts"
                item_dict = {
                    "text": display_text, "ts": item.ts,
                    "audio_duration_sec": duration_sec,
                    "confidence": tp.get("confidence"),
                    "translated_text": translated_text,
                    "translation_status": translation_status,
                    "diarization": diarization_data,
                }
                saved_path = self._transcript_writer_cls.write_transcript(item_dict, transcripts_dir)
                result_payload["transcript_file"] = str(saved_path)
            except Exception:
                logger.exception("Не удалось автосохранить транскрибацию в .md")

        if self._coerce_bool(settings.get("action_items_auto_extract", False), default=False):
            min_dur = float(settings.get("action_items_min_duration_sec", 60.0))
            if self._action_items_extractor is not None and (duration_sec or 0.0) >= min_dur:
                try:
                    lang = str(tp.get("language", "ru") or "ru").lower()[:2]
                    ai_result = self._action_items_extractor.extract(display_text, language=lang)
                    if ai_result.ok:
                        self._store.update_history_item_action_items(
                            item_id=item.id,
                            action_items=[ai.to_dict() for ai in ai_result.action_items],
                            decisions=ai_result.decisions,
                            questions=ai_result.questions,
                        )
                        result_payload["action_items_extracted"] = True
                        result_payload["action_items_count"] = len(ai_result.action_items)
                except Exception:
                    logger.exception("Авто-извлечение action items провалилось для %s", item.id)

        return result_payload

    # ------------------------------------------------------------------ #
    # Transcribe-paths core (sync, used by async worker too)              #
    # ------------------------------------------------------------------ #

    def _transcribe_paths_core(
        self,
        params: dict[str, Any],
        *,
        progress_callback: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        on_file_start: Callable[[int, str], None] | None = None,
        on_file_done: Callable[[int, dict[str, Any] | None, str | None], None] | None = None,
    ) -> dict[str, Any]:
        """Общее ядро синхронной и асинхронной транскрибации."""
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise RuntimeError("Параметр paths должен быть массивом")

        settings = self._cached_settings_fn()
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
        allowed_roots = [
            r.resolve()
            for r in (self._store.data_dir, Path.home(), Path("/tmp"), Path(tempfile.gettempdir()))
        ]
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

        user_vocabulary = self._vocabulary.load() or []

        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for file_index, audio_path in enumerate(audio_paths):
            if cancel_check is not None and cancel_check():
                break
            self._safe_callback(on_file_start, file_index, audio_path)
            started_at = time.monotonic()
            try:
                audio_duration_sec: float | None = None
                try:
                    import soundfile as sf
                    sf_info = sf.info(audio_path)
                    audio_duration_sec = round(sf_info.duration, 3)
                except Exception:
                    pass

                import_lang_hint = lang_hint if lang_hint else "auto"
                if progress_callback is not None:
                    self._transcriber.engine.set_quality_profile(quality_profile)
                    transcribe_payload = self._transcriber.engine.transcribe(
                        audio_path,
                        cleanup_profile=cleanup_profile,
                        is_preview=False,
                        domain="casual",
                        extra_vocabulary=user_vocabulary if user_vocabulary else None,
                        lang_hint=import_lang_hint,
                        progress_callback=progress_callback,
                    )
                else:
                    transcribe_payload = self._transcriber.transcribe(
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
                    err_line = f"{audio_path}: {err}" if err else f"{audio_path}: пустой результат"
                    errors.append(err_line)
                    self._safe_callback(on_file_done, file_index, None, err_line)
                    continue
                diarization_data = transcribe_payload.get("diarization") if isinstance(transcribe_payload, dict) else None
                detected_lang = transcribe_payload.get("language", "?") if isinstance(transcribe_payload, dict) else "?"

                translation = self._translator.translate(
                    text=text, mode=translation_mode, network_mode=network_mode,
                    translation_style=translation_style, glossary=translation_glossary,
                )
                translated_text = translation.text.strip() if translation.ok else ""
                final_text = translated_text if (translate_and_paste and translated_text) else text
                display_text = self._format_text_with_speakers_fn(final_text, diarization_data)

                history_item = self._store.add_history_item(
                    text=display_text, paste_status="failed",
                    source_text=text, translated_text=translated_text,
                    translation_mode=translation.mode,
                    source_lang=translation.source_lang,
                    target_lang=translation.target_lang,
                    translation_status=translation.status,
                    translation_engine=translation.engine,
                    diarization=diarization_data,
                    audio_duration_sec=audio_duration_sec,
                    emotion=(transcribe_payload.get("emotion") if isinstance(transcribe_payload, dict) and isinstance(transcribe_payload.get("emotion"), str) else None),
                    word_timestamps=(transcribe_payload.get("word_timestamps") if isinstance(transcribe_payload, dict) and isinstance(transcribe_payload.get("word_timestamps"), list) else None),
                    speaker_turns=(transcribe_payload.get("speaker_turns") if isinstance(transcribe_payload, dict) and isinstance(transcribe_payload.get("speaker_turns"), list) else None),
                )

                summary: str | None = None
                if len(final_text) > 500:
                    summary = self._generate_summary_fn(final_text)

                try:
                    transcripts_dir = Path(self._store.data_dir) / "transcripts"
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
                            speakers_list = diar_info.get("speaker_turns", [])
                            unique_speakers = len(set(t.get("speaker") for t in speakers_list))
                            f.write(f"- Спикеры: {unique_speakers}\n")
                        if summary:
                            f.write(f"\n## Краткое содержание\n\n{summary}\n")
                        if diar_info and diar_info.get("enabled") and diar_info.get("speaker_turns"):
                            f.write(f"\n## Диалог\n\n{display_text}\n")
                        else:
                            f.write(f"\n## Текст\n\n{final_text}\n")
                        if translated_text:
                            f.write(f"\n## Перевод ({translation.mode})\n\n{translated_text}\n")
                except Exception as exc:
                    logger.warning("Не удалось сохранить транскрипт в файл: %s", exc)

                item_result: dict[str, Any] = {
                    "path": audio_path, "text": display_text,
                    "original_text": text, "translated_text": translated_text,
                    "translation_mode": translation.mode, "translation_style": translation_style,
                    "translation_status": translation.status,
                    "source_lang": translation.source_lang, "target_lang": translation.target_lang,
                    "history_id": history_item.id, "duration_sec": elapsed,
                    "audio_duration_sec": audio_duration_sec, "language": detected_lang,
                }
                if summary:
                    item_result["summary"] = summary
                items.append(item_result)
                self._safe_callback(on_file_done, file_index, item_result, None)
            except Exception as exc:
                err_msg = str(exc)
                file_name = Path(audio_path).name
                if "Resource deadlock" in err_msg or "errno 11" in err_msg or "[Errno 11]" in err_msg or "[Errno 35]" in err_msg:
                    err_msg = f"Файл заблокирован (возможно iCloud): {file_name}"
                elif "timeout" in err_msg.lower():
                    err_msg = f"Превышено время транскрибации: {file_name}"
                elif "No such file" in err_msg:
                    err_msg = f"Файл не найден: {file_name}"
                elif "Permission denied" in err_msg:
                    err_msg = f"Нет доступа к файлу: {file_name}"
                elif "too large" in err_msg.lower() or "MAX_AUDIO_MB" in err_msg or "слишком большой" in err_msg.lower():
                    err_msg = f"{file_name}: {err_msg}"
                elif "Unsupported" in err_msg or "codec" in err_msg.lower():
                    err_msg = f"Неподдерживаемый формат аудио: {file_name}"
                else:
                    err_msg = f"{file_name}: {err_msg}"
                errors.append(err_msg)
                self._safe_callback(on_file_done, file_index, None, err_msg)

        return {"items": items, "processed": len(items), "errors": errors}

    # ------------------------------------------------------------------ #
    # Helper / utility methods                                            #
    # ------------------------------------------------------------------ #

    def _load_stop_recording_settings(
        self, params: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "quality_profile": str(params.get("quality_profile") or settings.get("quality_profile", "balanced")),
            "cleanup_profile": str(params.get("cleanup_profile") or settings.get("cleanup_profile", "soft")),
            "lang_hint": params.get("lang_hint") or None,
            "translation_mode": str(params.get("translation_mode") or settings.get("translation_mode", "off")),
            "translation_style": str(params.get("translation_style") or settings.get("translation_style", "neutral")),
            "translation_glossary": settings.get("translation_glossary", {}),
            "translate_and_paste": bool(
                params.get("translate_and_paste")
                if "translate_and_paste" in params
                else settings.get("translate_and_paste", False)
            ),
            "network_mode": str(settings.get("network_mode", "offline_default")),
            "silence_guard_enabled": self._coerce_bool(settings.get("silence_guard_enabled", True), default=True),
            "silence_rms_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_rms_threshold", 0.0020),
                default=0.0020, min_value=0.0003, max_value=0.05,
            ),
            "silence_peak_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_peak_threshold", 0.0120),
                default=0.0120, min_value=0.001, max_value=0.2,
            ),
            "silence_active_ratio_threshold": self._coerce_bounded(
                value=settings.get("silence_guard_active_ratio_threshold", 0.015),
                default=0.015, min_value=0.001, max_value=0.30,
            ),
            "background_guard_enabled": self._coerce_bool(settings.get("background_guard_enabled", True), default=True),
            "background_guard_min_peak": self._coerce_bounded(
                value=settings.get("background_guard_min_peak", 0.025),
                default=0.025, min_value=0.003, max_value=0.25,
            ),
            "background_guard_min_rms": self._coerce_bounded(
                value=settings.get("background_guard_min_rms", 0.0040),
                default=0.0040, min_value=0.0008, max_value=0.08,
            ),
            "background_guard_uniform_frame_threshold": self._coerce_bounded(
                value=settings.get("background_guard_uniform_frame_threshold", 0.0060),
                default=0.0060, min_value=0.001, max_value=0.20,
            ),
            "background_guard_max_uniform_active_ratio": self._coerce_bounded(
                value=settings.get("background_guard_max_uniform_active_ratio", 0.92),
                default=0.92, min_value=0.40, max_value=0.99,
            ),
            "sample_rate": self._coerce_bounded(
                value=getattr(self._recorder, "sample_rate", 16000),
                default=16000, min_value=8000, max_value=192000,
            ),
        }

    def _build_empty_audio_response(
        self,
        duration_sec: float,
        quality_profile: str,
        cleanup_profile: str,
        translation_mode: str,
        translate_and_paste: bool,
        stop_tail_trim_ms: int,
        silence_detected: bool = False,
        silence_guard_enabled: bool = False,
        background_guard_rejected: bool = False,
    ) -> dict[str, Any]:
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
            "silence_detected": silence_detected,
            "silence_guard_enabled": silence_guard_enabled,
            "background_guard_rejected": background_guard_rejected,
        }

    def _stop_recorder_guarded(self, stop_tail_trim_ms: int) -> tuple[Any, float] | None:
        stop_callable = getattr(self._recorder, "stop", None)
        if not callable(stop_callable):
            raise RuntimeError("Рекордер не поддерживает stop()")
        try:
            return stop_callable(trim_tail_ms=stop_tail_trim_ms)
        except TypeError:
            return stop_callable()

    @staticmethod
    def _safe_callback(fn: Callable | None, *args: Any) -> None:
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                logger.exception("Callback %s упал", fn)

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
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
        coerce = int if isinstance(default, int) else float
        try:
            parsed = coerce(value)
        except (TypeError, ValueError):
            parsed = coerce(default)
        return max(min_value, min(parsed, max_value))

    @staticmethod
    def _looks_like_silence_audio(
        audio: Any,
        sample_rate: int,
        rms_threshold: float,
        peak_threshold: float,
        active_ratio_threshold: float,
    ) -> bool:
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
        frame_size = max(1, int(sample_rate * 0.02))
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
        frame_size = max(1, int(sample_rate * 0.02))
        frame_count = int(data.size // frame_size)
        if frame_count <= 0:
            return low_level
        shaped = data[: frame_count * frame_size].reshape(frame_count, frame_size)
        frame_rms = np.sqrt(np.mean(np.square(shaped), axis=1, dtype=np.float64))
        mean_rms = float(np.mean(frame_rms))
        std_rms = float(np.std(frame_rms))
        variation_coeff = std_rms / max(mean_rms, 1e-8)
        duration_sec = float(data.size) / max(float(sample_rate), 1.0)
        dynamic_uniform_threshold = max(0.0012, min(uniform_frame_threshold, max(min_rms * 0.35, 0.0012)))
        active_ratio = float(np.mean(frame_rms >= dynamic_uniform_threshold))
        background_pattern = active_ratio >= max_uniform_active_ratio and variation_coeff < 0.35
        very_uniform = active_ratio >= 0.96 and variation_coeff < 0.18
        return background_pattern and (low_level or (very_uniform and duration_sec >= 4.0))

    @staticmethod
    def _extract_transcribed_text(payload: Any) -> str:
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
        if isinstance(payload, dict):
            error = payload.get("error")
            if error is not None:
                return str(error).strip()
        return ""

    @staticmethod
    def _is_known_prompt_echo(normalized_text: str) -> bool:
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
        if (
            "сохраняй" in words and "смысл" in words
            and any(token.startswith("корр") for token in words)
            and any(token.startswith("пункт") for token in words)
        ):
            return True
        compact = " ".join(words)
        return bool(re.search(r"сохраняй\s+смысл.*корр\w*.*пункт\w*", compact))

    @staticmethod
    def _contains_repeated_chunk(words: list[str], min_repeats: int = 3) -> bool:
        total = len(words)
        if total < 6:
            return False
        max_chunk = min(7, total // min_repeats)
        for chunk_size in range(2, max_chunk + 1):
            start = 0
            while start + (chunk_size * min_repeats) <= total:
                chunk = words[start: start + chunk_size]
                repeats = 1
                while start + (chunk_size * (repeats + 1)) <= total:
                    next_chunk = words[start + (chunk_size * repeats): start + (chunk_size * (repeats + 1))]
                    if next_chunk != chunk:
                        break
                    repeats += 1
                if repeats >= min_repeats:
                    return True
                start += 1
        return False

    @staticmethod
    def _looks_like_looping_artifact(words: list[str], min_words: int, min_bigram_hits: int) -> bool:
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
        return top_bigram_freq >= min_bigram_hits

    @staticmethod
    def _collapse_immediate_duplicate_phrase(normalized_text: str) -> str:
        normalized = str(normalized_text or "").strip()
        if not normalized:
            return ""
        words = normalized.split()
        total = len(words)
        if total < 8:
            return ""
        if total % 2 == 0:
            half = total // 2
            if words[:half] == words[half:]:
                collapsed = " ".join(words[:half]).strip()
                if not collapsed:
                    return ""
                return f"{collapsed[0].upper()}{collapsed[1:]}."
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
    def _postprocess_transcribed_text(text: str) -> str:
        _logger = logging.getLogger("KrabEar.Backend.RecordingCore")
        clean = str(text or "").strip()
        if not clean:
            return ""
        lowered = clean.lower()
        if "<begin_of_box>" in lowered or "<end_of_box>" in lowered or "\"action\":" in lowered:
            _logger.warning("postprocess: drop reason=tech_artifact, len=%d, sample=%r", len(clean), clean[:80])
            return ""
        normalized = TextUtils.normalize_phrase(clean)
        if RecordingCoreService._is_known_prompt_echo(normalized):
            _logger.warning("postprocess: drop reason=known_prompt_echo, len=%d, sample=%r", len(clean), clean[:80])
            return ""
        collapsed_duplicate = RecordingCoreService._collapse_immediate_duplicate_phrase(normalized)
        if collapsed_duplicate:
            clean = collapsed_duplicate
        words = re.findall(r"[A-Za-zА-Яа-я0-9'-]+", clean.lower())
        if RecordingCoreService._looks_like_looping_artifact(words, min_words=8, min_bigram_hits=4):
            _logger.warning("postprocess: drop reason=looping_artifact, len=%d, sample=%r", len(clean), clean[:80])
            return ""
        clean = re.sub(r"\s+([,.;:!?])", r"\1", clean)
        clean = re.sub(r"([,.;:!?])([^\s])", r"\1 \2", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        first_alpha_idx = next((idx for idx, char in enumerate(clean) if char.isalpha()), -1)
        if first_alpha_idx >= 0:
            clean = clean[:first_alpha_idx] + clean[first_alpha_idx].upper() + clean[first_alpha_idx + 1:]
        if not re.search(r"[.!?…]$", clean):
            if len(words) >= 4:
                clean = f"{clean}."
        return clean.strip()

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
                candidates = sorted(
                    (c for c in path.rglob("*") if c.is_file() and c.suffix.lower() in audio_ext),
                    key=lambda c: str(c),
                )
                result.extend(str(c.resolve()) for c in candidates)
        unique: list[str] = []
        seen: set[str] = set()
        for item in result:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique
