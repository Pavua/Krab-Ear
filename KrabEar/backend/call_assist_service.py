"""Call Assist — бизнес-логика ассистента звонков с интеграцией Voice Gateway.

Выделено из service.py для уменьшения размера монолитного BackendService.
Все handler-методы делегируются сюда из BackendService.handle_request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable
from urllib import error as urllib_error, parse as urllib_parse, request as urllib_request
import uuid

from backend.observability import add_breadcrumb, mask_phone
from backend.ipc_errors import IpcOperationalError

logger = logging.getLogger("KrabEar.Backend.CallAssist")


class VoiceGatewayClient:
    """HTTP-клиент для взаимодействия с Voice Gateway."""

    # Class-level timeout constants (PR #9 — extracted from scattered literals)
    SESSION_LIFECYCLE_TIMEOUT = 3.5  # start/stop session — короткий чтобы UI не висел
    HTTP_TIMEOUT = 4.0  # general REST calls (get/post/delete)

    @staticmethod
    def start_session(
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
            with urllib_request.urlopen(request, timeout=VoiceGatewayClient.SESSION_LIFECYCLE_TIMEOUT) as response:
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
    def get(
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
            with urllib_request.urlopen(request, timeout=VoiceGatewayClient.HTTP_TIMEOUT) as response:
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
    def post(
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
            with urllib_request.urlopen(request, timeout=VoiceGatewayClient.HTTP_TIMEOUT) as response:
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
    def delete(
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
            with urllib_request.urlopen(request, timeout=VoiceGatewayClient.HTTP_TIMEOUT) as response:
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
    def stop_session(
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
            with urllib_request.urlopen(request, timeout=VoiceGatewayClient.SESSION_LIFECYCLE_TIMEOUT):
                return {"ok": True}
        except urllib_error.HTTPError as exc:
            return {"ok": False, "error": f"http_{exc.code}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


class CallAssistService:
    """Бизнес-логика ассистента звонков: сессии, timeline, quick-phrase, стоимость."""

    def __init__(
        self,
        store: Any,
        recorder: Any,
        transcriber: Any,
        *,
        gateway: VoiceGatewayClient | None = None,
        extract_text_fn: Callable[[Any], str] | None = None,
        reset_preview_fn: Callable[[], None] | None = None,
        start_preview_fn: Callable[[str], None] | None = None,
        coerce_bool_fn: Callable[[Any, bool], bool] | None = None,
        settings_get: Callable[[str, Any], Any] | None = None,
    ) -> None:
        self.store: Any = store
        self.recorder: Any = recorder
        self.transcriber: Any = transcriber
        self.gateway: VoiceGatewayClient = gateway or VoiceGatewayClient()
        self._extract_text: Callable[[Any], str] = extract_text_fn or self._default_extract_text
        self._reset_preview: Callable[[], None] = reset_preview_fn or (lambda: None)
        self._start_preview: Callable[[str], None] = start_preview_fn or (lambda qp: None)
        self._coerce_bool: Callable[[Any, bool], bool] = coerce_bool_fn or self._default_coerce_bool
        self._settings_get: Callable[[str, Any], Any] = settings_get or (lambda k, d: d)
        self._lock: threading.Lock = threading.Lock()
        self._state: dict[str, Any] = {
            "active": False,
            "status": "idle",
            "session_id": None,
            "gateway_session_id": None,
        }
        self._pending_post_count: int = 0
        self._max_pending_post_depth_observed: int = 0

    @staticmethod
    def _default_extract_text(payload: Any) -> str:
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
    def _default_coerce_bool(value: Any, default: bool) -> bool:
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

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    # ------------------------------------------------------------------
    # Handlers (сигнатуры совпадают с BackendService._handle_*)
    # ------------------------------------------------------------------

    def handle_start(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запускает сессию ассистента звонка с интеграцией Voice Gateway."""
        # F1: guard against double-start race — reject if a session is already active.
        # Mark active=True atomically under the lock *before* doing any I/O so that
        # concurrent callers see the "taken" state immediately.
        with self._lock:
            if self._state.get("active"):
                existing_id = self._state.get("session_id", "")
                logger.warning(
                    "handle_start: отклонено — сессия %s уже активна",
                    existing_id,
                )
                return {
                    "ok": False,
                    "error": "already_active",
                    "session_id": existing_id,
                    "status": self._state.get("status", "running"),
                }
            # Claim the slot immediately so concurrent callers see active=True
            self._state["active"] = True
            self._state["status"] = "starting"

        if not self.recorder.is_recording:
            started = self.recorder.start()
            if started:
                self._reset_preview()
                self._start_preview("balanced")

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
            gateway_result = self.gateway.start_session(
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
        with self._lock:
            self._state = state

        phone_raw = str(params.get("phone", "")).strip()
        add_breadcrumb(
            category="call",
            message="call_dial",
            level="info",
            data={
                "provider": str(params.get("provider", "voip")),
                "phone_masked": mask_phone(phone_raw) if phone_raw else None,
                "translation_mode": translation_mode,
            },
        )

        if gateway_session_id and gateway_status == "ok":
            t = threading.Thread(
                target=self._assist_loop,
                args=(gateway_session_id, voice_gateway_url, voice_gateway_api_key),
                daemon=True,
            )
            t.start()

        return dict(state)

    def handle_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        """Останавливает текущую сессию ассистента звонка."""
        stopped_at = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            active = bool(self._state.get("active"))
            state = dict(self._state)
            if not active:
                idle_state = {
                    "active": False,
                    "status": "idle",
                    "session_id": None,
                    "gateway_session_id": None,
                    "stopped_at": stopped_at,
                }
                self._state = idle_state
                return idle_state

            self._state["active"] = False
            self._state["status"] = "stopped"
            self._state["stopped_at"] = stopped_at
            state = dict(self._state)

        # F2 / C3 (wave-31): only stop recorder when call assist was actually active.
        # The recorder is shared with the main recording workflow; stopping it
        # unconditionally would silently abort an unrelated recording that happened
        # to be running when handle_stop is called on an already-idle session.
        if active and self.recorder.is_recording:
            try:
                self.recorder.stop()
            except Exception:
                # AudioRecorderStopTimeout и родня: зависший worker не должен
                # рушить stop-флоу call assist — сессия уже помечена stopped,
                # дальше идут VG-вызовы. Раньше stop() молча возвращал None.
                logger.exception("call_assist stop: рекордер не остановился")

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
            summary_result = self.gateway.post(
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
            gateway_result = self.gateway.stop_session(
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

        with self._lock:
            self._state = dict(state)

        started_at_str = str(state.get("started_at") or "")
        call_duration_sec: float | None = None
        if started_at_str:
            try:
                from datetime import timezone  # noqa: PLC0415
                started_dt = datetime.fromisoformat(started_at_str)
                stopped_dt = datetime.fromisoformat(stopped_at)
                if started_dt.tzinfo is None:
                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                if stopped_dt.tzinfo is None:
                    stopped_dt = stopped_dt.replace(tzinfo=timezone.utc)
                call_duration_sec = round((stopped_dt - started_dt).total_seconds(), 1)
            except Exception:  # noqa: BLE001
                pass
        add_breadcrumb(
            category="call",
            message="call_hangup",
            level="info",
            data={
                "duration_sec": call_duration_sec,
                "summary_status": state.get("summary_status"),
            },
        )
        return state

    def handle_get_state(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.state

    def handle_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает diagnostics и explain-пакет почему перевод не появился.

        Privacy gate (C2, wave-31): когда privacy_mode_enabled=True — возвращает
        минимальный ответ без live-транскрипта и translation payload'а.
        """
        if self._settings_get("privacy_mode_enabled", False):
            with self._lock:
                active = bool(self._state.get("active"))
            return {
                "active": active,
                "gateway_session_id": None,
                "diagnostics": {},
                "why": {},
                "pending_posts": {"current": 0, "max_observed": 0},
                "privacy_mode_active": True,
            }
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._lock:
            gateway_session_id = str(self._state.get("gateway_session_id") or "").strip()
            active = bool(self._state.get("active"))
            pending_current = self._pending_post_count
            pending_max = self._max_pending_post_depth_observed
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        diag_result = self.gateway.get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/diagnostics",
        )
        if not diag_result.get("ok"):
            raise IpcOperationalError(f"Gateway diagnostics error: {diag_result.get('error', 'unknown')}")

        include_why = bool(params.get("include_why", True))
        why_payload: dict[str, Any] = {}
        if include_why:
            why_result = self.gateway.get(
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
            "pending_posts": {
                "current": pending_current,
                "max_observed": pending_max,
            },
        }

    def handle_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        """Запрашивает summary текущей звонковой сессии.

        Privacy gate (C2, wave-31): когда privacy_mode_enabled=True — возвращает
        пустой summary без транскрипта.
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {
                "gateway_session_id": None,
                "summary": {},
                "privacy_mode_active": True,
            }
        t0 = time.monotonic()
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._lock:
            gateway_session_id = str(self._state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        max_items = int(params.get("max_items", 30) or 30)
        max_items = max(1, min(max_items, 200))
        summary_result = self.gateway.post(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=f"/v1/sessions/{gateway_session_id}/summary",
            payload={"max_items": max_items},
        )
        ok = summary_result.get("ok", False)
        add_breadcrumb(
            category="call",
            message="call_assist_summary",
            level="info" if ok else "warning",
            data={
                "ok": ok,
                "max_items": max_items,
                "duration_ms": round((time.monotonic() - t0) * 1000),
            },
        )
        if not ok:
            raise IpcOperationalError(f"Gateway summary error: {summary_result.get('error', 'unknown')}")
        return {
            "gateway_session_id": gateway_session_id,
            "summary": summary_result.get("payload", {}),
        }

    def handle_quick_phrase(self, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет быструю фразу на перевод/озвучку в Voice Gateway."""
        text = str(params.get("text", "")).strip()
        if not text:
            raise RuntimeError("text обязателен")

        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._lock:
            gateway_session_id = str(self._state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError("Нет активной gateway-сессии call assist")

        source_lang = str(params.get("source_lang", "ru")).strip().lower() or "ru"
        target_lang = str(params.get("target_lang", "es")).strip().lower() or "es"
        voice = str(params.get("voice", "default")).strip() or "default"
        style = str(params.get("style", "chat")).strip() or "chat"

        quick_result = self.gateway.post(
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
            raise IpcOperationalError(f"Gateway quick-phrase error: {quick_result.get('error', 'unknown')}")
        return {
            "gateway_session_id": gateway_session_id,
            "quick_phrase": quick_result.get("payload", {}),
        }

    def handle_list_quick_phrases(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает библиотеку быстрых фраз из Voice Gateway."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()

        source_lang = str(params.get("source_lang", "ru")).strip().lower() or "ru"
        target_lang = str(params.get("target_lang", "es")).strip().lower() or "es"
        category = str(params.get("category", "all")).strip().lower() or "all"
        limit = int(params.get("limit", 30) or 30)
        limit = max(1, min(limit, 200))

        # wave-1770: URL-encode params (same pattern as handle_timeline lines 827-863).
        # Without quote(), a value like "ru&admin=1" injects extra query params.
        query = (
            f"/v1/quick-phrases?source_lang={urllib_parse.quote(source_lang, safe='')}"
            f"&target_lang={urllib_parse.quote(target_lang, safe='')}"
            f"&category={urllib_parse.quote(category, safe='')}&limit={limit}"
        )
        result = self.gateway.get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=query,
        )
        if not result.get("ok"):
            # Voice Gateway часто offline (port 8090 не запущен) — это нормально,
            # quick-phrases это optional feature. Возвращаем пустой набор + status,
            # чтобы клиент мог показать "VG недоступен" вместо crash'а.
            # Schema совместима с success path (payload содержит "items").
            err = str(result.get("error", "unknown"))
            logger.info(
                "list_quick_phrases: gateway недоступен (%s) — возвращаю пустой набор",
                err[:80],
            )
            return {
                "items": [],
                "status": "gateway_unavailable",
                "error": err,
            }
        return result.get("payload", {})

    def handle_list_templates(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает локальные шаблоны быстрых реплик."""
        settings = self.store.load_settings()
        templates = self._normalize_templates(settings.get("call_quick_templates", []))
        return {"templates": templates}

    def handle_add_template(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет пользовательский шаблон фразы."""
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

    def handle_remove_template(self, params: dict[str, Any]) -> dict[str, Any]:
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

    def handle_template(self, params: dict[str, Any]) -> dict[str, Any]:
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
        return self.handle_quick_phrase(payload)

    def handle_cost_estimate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Считает оценку telephony+AI стоимости через Voice Gateway."""
        t0 = time.monotonic()
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
        use_live_pricing = self._coerce_bool(params.get("use_live_pricing", True), True)

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
        result = self.gateway.get(
            voice_gateway_url=voice_gateway_url,
            api_key=voice_gateway_api_key,
            path=query,
        )
        ok = result.get("ok", False)
        payload = result.get("payload", {}) if ok else {}
        total_usd = float(payload.get("total_usd", 0.0)) if isinstance(payload, dict) else 0.0
        add_breadcrumb(
            category="call",
            message="call_assist_cost_estimate",
            level="info" if ok else "warning",
            data={
                "ok": ok,
                "country": country,
                "total_usd": round(total_usd, 4),
                "duration_ms": round((time.monotonic() - t0) * 1000),
            },
        )
        if not ok:
            raise IpcOperationalError(f"Gateway cost estimate error: {result.get('error', 'unknown')}")
        return payload if isinstance(payload, dict) else {"ok": True, "country": country}

    def handle_timeline(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает timeline текущей звонковой сессии.

        Privacy gate (C2, wave-31): когда privacy_mode_enabled=True — возвращает
        пустой timeline без transcript-событий.
        """
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "items": [], "count": 0, "privacy_mode_active": True}
        gw_url, gw_key, gw_sid = self._gateway_context()
        limit = int(params.get("limit", 80) or 80)
        limit = max(1, min(limit, 500))
        kind = str(params.get("kind", "")).strip()
        contains = str(params.get("contains", "")).strip()
        query_parts = [f"limit={limit}"]
        if kind:
            query_parts.append(f"kind={urllib_parse.quote(kind, safe='')}")
        if contains:
            query_parts.append(f"contains={urllib_parse.quote(contains, safe='')}")
        path = f"/v1/sessions/{gw_sid}/timeline?{'&'.join(query_parts)}"
        result = self.gateway.get(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "items": [], "count": 0}

    def handle_cost_report(self, params: dict[str, Any]) -> dict[str, Any]:
        """Считает usage-показатели и вызывает Gateway cost estimate."""
        gw_url, gw_key, gw_sid = self._gateway_context()
        settings = self.store.load_settings()

        country = str(params.get("country", "ES")).strip().upper()
        if len(country) != 2:
            country = "ES"
        use_live_pricing = self._coerce_bool(params.get("use_live_pricing", True), True)
        budget_limit = float(params.get("budget_limit") or settings.get("call_budget_usd", 2.0))
        limit = int(params.get("stats_limit", 400) or 400)
        limit = max(100, min(limit, 2000))

        stats_result = self.gateway.get(
            voice_gateway_url=gw_url,
            api_key=gw_key,
            path=f"/v1/sessions/{gw_sid}/timeline/stats?limit={limit}",
        )
        if not stats_result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline stats error: {stats_result.get('error', 'unknown')}")
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
        cost_result = self.gateway.get(
            voice_gateway_url=gw_url,
            api_key=gw_key,
            path=f"/v1/telephony/cost/estimate?{'&'.join(query_parts)}",
        )
        if not cost_result.get("ok"):
            raise IpcOperationalError(f"Gateway cost estimate error: {cost_result.get('error', 'unknown')}")

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

    def handle_timeline_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        gw_url, gw_key, gw_sid = self._gateway_context("timeline stats")
        limit = int(params.get("limit", 1000) or 1000)
        limit = max(1, min(limit, 5000))
        path = f"/v1/sessions/{gw_sid}/timeline/stats?limit={limit}"
        result = self.gateway.get(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline stats error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "stats": {"count": 0}}

    def handle_timeline_summary(self, params: dict[str, Any]) -> dict[str, Any]:
        # wave-1770: privacy gate — sibling handle_summary/handle_timeline gate (wave-31 C2),
        # but the timeline_* variants were missed. Summary contains conversation transcript.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "summary": "", "tasks": [], "privacy_mode_active": True}
        gw_url, gw_key, gw_sid = self._gateway_context("timeline summary")
        limit = int(params.get("limit", 400) or 400)
        limit = max(1, min(limit, 5000))
        max_tasks = int(params.get("max_tasks", 8) or 8)
        max_tasks = max(1, min(max_tasks, 20))
        path = f"/v1/sessions/{gw_sid}/timeline/summary?limit={limit}&max_tasks={max_tasks}"
        result = self.gateway.get(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline summary error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "summary": "", "tasks": []}

    def handle_timeline_export(self, params: dict[str, Any]) -> dict[str, Any]:
        # wave-1770: privacy gate — export returns the FULL conversation log.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": True, "format": "md", "content": "", "privacy_mode_active": True}
        gw_url, gw_key, gw_sid = self._gateway_context("timeline export")
        export_format = str(params.get("format", "md")).strip().lower() or "md"
        if export_format not in {"md", "ndjson"}:
            export_format = "md"
        limit = int(params.get("limit", 200) or 200)
        limit = max(1, min(limit, 1000))
        path = f"/v1/sessions/{gw_sid}/timeline/export?format={export_format}&limit={limit}"
        result = self.gateway.get(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline export error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "format": export_format, "content": ""}

    def handle_timeline_clear(self, params: dict[str, Any]) -> dict[str, Any]:
        gw_url, gw_key, gw_sid = self._gateway_context("очистки timeline")
        keep_last = int(params.get("keep_last", 0) or 0)
        keep_last = max(0, min(keep_last, 200))
        path = f"/v1/sessions/{gw_sid}/timeline?keep_last={keep_last}"
        result = self.gateway.delete(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline clear error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        return payload if isinstance(payload, dict) else {"ok": True, "keep_last": keep_last}

    def handle_timeline_to_history(self, params: dict[str, Any]) -> dict[str, Any]:
        """Сохраняет экспорт timeline в историю Krab Ear."""
        # wave-1770: privacy gate — persists the full conversation transcript to history.
        # Must block in privacy mode (most sensitive of the three — writes to disk).
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "privacy_mode_active": True, "history_id": None}
        gw_url, gw_key, gw_sid = self._gateway_context("сохранения timeline")

        export_format = str(params.get("format", "md")).strip().lower() or "md"
        if export_format not in {"md", "ndjson"}:
            export_format = "md"
        limit = int(params.get("limit", 400) or 400)
        limit = max(1, min(limit, 2000))
        include_summary = self._coerce_bool(params.get("include_summary", True), True)
        include_stats = self._coerce_bool(params.get("include_stats", True), True)
        max_tasks = int(params.get("max_tasks", 8) or 8)
        max_tasks = max(1, min(max_tasks, 20))

        summary_payload: dict[str, Any] = {}
        stats_payload: dict[str, Any] = {}
        if include_summary:
            summary_result = self.gateway.get(
                voice_gateway_url=gw_url,
                api_key=gw_key,
                path=f"/v1/sessions/{gw_sid}/timeline/summary?limit={limit}&max_tasks={max_tasks}",
            )
            if summary_result.get("ok"):
                raw_summary = summary_result.get("payload", {})
                if isinstance(raw_summary, dict):
                    summary_payload = raw_summary
        if include_stats:
            stats_result = self.gateway.get(
                voice_gateway_url=gw_url,
                api_key=gw_key,
                path=f"/v1/sessions/{gw_sid}/timeline/stats?limit={limit}",
            )
            if stats_result.get("ok"):
                raw_stats = stats_result.get("payload", {})
                if isinstance(raw_stats, dict):
                    stats_payload = raw_stats

        path = f"/v1/sessions/{gw_sid}/timeline/export?format={export_format}&limit={limit}"
        result = self.gateway.get(voice_gateway_url=gw_url, api_key=gw_key, path=path)
        if not result.get("ok"):
            raise IpcOperationalError(f"Gateway timeline export error: {result.get('error', 'unknown')}")
        payload = result.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        content = str(payload.get("content", "")).strip()
        if not content:
            raise RuntimeError("Timeline пуст, нечего сохранять в историю")

        session_tag = str(params.get("session_tag", "")).strip()
        if not session_tag:
            session_tag = gw_sid
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
                for idx, task in enumerate(tasks[:max_tasks], start=1):
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
        add_breadcrumb(
            category="call",
            message="call_assist_timeline_to_history",
            level="info",
            data={
                "format": export_format,
                "chars": len(content),
                "summary_included": bool(summary_payload),
                "stats_included": bool(stats_payload),
            },
        )
        return {
            "ok": True,
            "gateway_session_id": gw_sid,
            "format": export_format,
            "history_id": history_item.id,
            "chars": len(content),
            "summary_included": bool(summary_payload),
            "stats_included": bool(stats_payload),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _gateway_context(self, label: str = "") -> tuple[str, str, str]:
        """Возвращает (url, api_key, gateway_session_id) или бросает RuntimeError."""
        settings = self.store.load_settings()
        voice_gateway_url = str(settings.get("voice_gateway_url", "http://127.0.0.1:8090")).strip()
        voice_gateway_api_key = str(settings.get("voice_gateway_api_key", "")).strip()
        with self._lock:
            gateway_session_id = str(self._state.get("gateway_session_id") or "").strip()
        if not gateway_session_id:
            raise RuntimeError(f"Нет активной gateway_session_id для {label}" if label else "Нет активной gateway_session_id")
        return voice_gateway_url, voice_gateway_api_key, gateway_session_id

    def _assist_loop(self, session_id: str, gateway_url: str, api_key: str) -> None:
        """Фоновый цикл отправки транскрибации в Voice Gateway."""
        last_sent_text = ""
        backoff_delay: float = 0.0
        _BACKOFF_STEPS = [0.5, 1.0, 2.0, 4.0]
        _BACKOFF_MAX = 4.0

        while True:
            with self._lock:
                if not self._state.get("active"):
                    break

            if not self.recorder.is_recording:
                break

            try:
                audio_data, duration_sec = self.recorder.snapshot_audio(max_duration_sec=25.0)
                current_size = getattr(audio_data, "size", 0)
                if current_size < 16000:
                    time.sleep(1.0)  # sync context OK: runs in threading.Thread
                    continue

                logger.debug(f"Call Assist: transcribing {current_size} samples")
                preview_payload = self.transcriber.transcribe_preview(
                    audio_data,
                    quality_profile="balanced",
                )
                text = self._extract_text(preview_payload)

                if text and text != last_sent_text:
                    # wave-1770: do NOT log transcript preview — call text is PII even at
                    # DEBUG. Log length only (enough for diagnostics, no content leak).
                    logger.debug("Call Assist: sending text len=%d", len(text))

                    with self._lock:
                        self._pending_post_count += 1
                        current_pending = self._pending_post_count
                        if current_pending > self._max_pending_post_depth_observed:
                            self._max_pending_post_depth_observed = current_pending
                    if current_pending > 3:
                        logger.warning("Call assist backpressure: %d POSTs in flight", current_pending)

                    try:
                        resp = self.gateway.post(
                            gateway_url,
                            api_key,
                            f"/v1/sessions/{session_id}/events",
                            {"type": "stt.partial", "data": {"text": text}},
                        )
                    finally:
                        with self._lock:
                            self._pending_post_count = max(0, self._pending_post_count - 1)

                    logger.debug("Call Assist: post ok=%s", resp.get("ok"))
                    if resp.get("ok"):
                        backoff_delay = 0.0
                        last_sent_text = text
                    else:
                        if backoff_delay == 0.0:
                            backoff_delay = _BACKOFF_STEPS[0]
                        else:
                            backoff_delay = min(backoff_delay * 2, _BACKOFF_MAX)
                        logger.warning(
                            "Call Assist: POST failed (%s), backoff=%.1fs",
                            resp.get("error"),
                            backoff_delay,
                        )
                        time.sleep(backoff_delay)  # sync context OK: runs in threading.Thread
                        continue

            except Exception:
                logger.exception("Call Assist loop error")

            time.sleep(1.5)  # sync context OK: runs in threading.Thread

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

    @staticmethod
    def _normalize_templates(raw: Any) -> list[dict[str, str]]:
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
