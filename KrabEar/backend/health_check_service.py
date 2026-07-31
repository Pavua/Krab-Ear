"""HealthCheckService — обработчики IPC-методов диагностики и проверки здоровья Krab Ear.

Выделен из backend/service.py для снижения размера монолитного модуля.
Содержит 7 IPC-обработчиков: ping, health_check, get_diagnostics,
probe_llm_http, get_startup_diagnostics, check_integrity, handshake.

КРИТИЧНО: контракт handle_ping должен оставаться bit-exact —
HealthMonitor.swift проверяет поле status == "ok" по каждому 3-секундному тику.
"""

from __future__ import annotations

import logging
import platform
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.event_bridge import EventBridge
    from backend.health_checker import HealthChecker
    from backend.integrity_checker import IntegrityChecker
    from backend.llm_probe import LLMHttpProbe
    from backend.llm_rewriter import LLMRewriter
    from backend.metrics_collector import MetricsCollector
    from backend.settings_service import SettingsService
    from backend.startup_diagnostics import StartupDiagnostics
    from backend.state_store import StateStore
    from backend.transcriber import Transcriber

logger = logging.getLogger("KrabEar.Backend.HealthCheckService")


class HealthCheckService:
    """Обработчики IPC-команд диагностики и проверки здоровья бэкенда."""

    def __init__(
        self,
        store: "StateStore",
        health_checker: "HealthChecker",
        startup_diagnostics: "StartupDiagnostics",
        integrity_checker: "IntegrityChecker",
        llm_probe: "LLMHttpProbe | None" = None,
        metrics_collector: "MetricsCollector | None" = None,
        event_bridge: "EventBridge | None" = None,
        # Optional collaborators for get_diagnostics
        transcriber: "Transcriber | None" = None,
        llm_rewriter: "LLMRewriter | None" = None,
        settings_svc: "SettingsService | None" = None,
        start_time: float | None = None,
        app_version: str = "",
        recorder: Any = None,
        last_stt_engine_ref: list[str] | None = None,
        wake_word_watchdog: Any = None,
        rest_inprocess: Any = None,
        rest_watchdog: Any = None,
    ) -> None:
        self.store = store
        self._health_checker = health_checker
        self._startup_diagnostics = startup_diagnostics
        self._integrity_checker = integrity_checker
        self._llm_probe = llm_probe
        self._metrics_collector = metrics_collector
        self._event_bridge = event_bridge
        self._transcriber = transcriber
        self._llm_rewriter = llm_rewriter
        self._settings_svc = settings_svc
        self._start_time: float = start_time if start_time is not None else time.monotonic()
        self._app_version: str = app_version
        self._recorder = recorder
        # last_stt_engine_ref: mutable single-element list updated by BackendService on each transcription.
        # BackendService должен обновлять last_stt_engine_ref[0] при каждом stop_recording.
        self._last_stt_engine_ref: list[str] = last_stt_engine_ref if last_stt_engine_ref is not None else [""]
        # 2026-07-15 (спека wake-word-watchdog): опциональный — get_diagnostics
        # деградирует до schema-parity fallback, если watchdog не подключён.
        self._wake_word_watchdog = wake_word_watchdog
        # M2 (спека 2026-07-16 §4.2): опциональный — рубильник REST_IN_PROCESS_ENABLED
        # по умолчанию выключен, get_diagnostics деградирует до schema-parity fallback.
        self._rest_inprocess = rest_inprocess
        # S3/Задача 7b: сторож REST (rest_watchdog.py) — опциональный по той
        # же причине, что rest_inprocess: конструируется только когда
        # рубильник REST_IN_PROCESS_ENABLED включён (см. service.py).
        self._rest_watchdog = rest_watchdog

    # ------------------------------------------------------------------
    # handle_ping
    # КРИТИЧНО: не менять поля / типы — HealthMonitor.swift парсит ответ.
    # Контракт: {"status": "ok", "service": str, "version": str,
    #            "uptime_sec": float, "is_recording": bool, "history_count": int}
    # ------------------------------------------------------------------

    def _is_privacy_mode(self) -> bool:
        """Returns True if privacy_mode_enabled is active via SettingsService."""
        if self._settings_svc is None:
            return False
        try:
            return bool(self._settings_svc.cached_settings().get("privacy_mode_enabled", False))
        except Exception:
            return False

    def handle_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает статус сервиса. Используется HealthMonitor.swift (3-сек тик).

        ВАЖНО: контракт bit-exact — не менять имена полей и типы.
        """
        # wave-1770 HIGH: history_count reveals user activity pattern.
        # Schema stays identical (int), but returns 0 in privacy mode to avoid
        # leaking recording count through this 3-second polling endpoint.
        if self._is_privacy_mode():
            history_count = 0
        else:
            try:
                history_count = self.store.count_active_items()
            except Exception:
                history_count = -1
        return {
            "status": "ok",
            "service": "krabear-backend",
            "version": self._app_version,
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "is_recording": bool(getattr(self._recorder, "is_recording", False)),
            "history_count": history_count,
        }

    # ------------------------------------------------------------------
    # handle_health_check
    # ------------------------------------------------------------------

    def handle_health_check(self, params: dict[str, Any]) -> dict[str, Any]:
        """Агрегированный health check всех ключевых подсистем бэкенда."""
        return self._health_checker.check_all()

    # ------------------------------------------------------------------
    # handle_get_diagnostics
    # ------------------------------------------------------------------

    def handle_get_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает комплексную диагностику: системная информация, STT, LLM, история и кэш настроек."""
        # Import inside method to avoid heavy dep at module load time and to match service.py pattern
        from backend.performance_profiler import profiler as _performance_profiler
        from core.config import settings as _global_settings

        try:
            diarization_device = str(self._transcriber.engine._resolve_diarization_device()) if self._transcriber else "unknown"
        except Exception:
            diarization_device = "unknown"

        # wave-1770 HIGH: mask history stats in privacy mode.
        privacy = self._is_privacy_mode()
        if privacy:
            history_count = 0
        else:
            try:
                history_count = self.store.count_active_items()
            except Exception:
                history_count = -1

        # Агрегированный отчёт профайлера по всем отслеживаемым span'ам (STT/translate/LLM).
        try:
            profiler_report = _performance_profiler.get_profile_report()
        except Exception as exc:
            logger.warning("Не удалось получить отчёт профайлера: %s", exc)
            profiler_report = {
                "methods": {},
                "slowest_methods": [],
                "total_profiled_time_sec": 0.0,
                "error": str(exc),
            }

        return {
            "system": {
                "python_version": sys.version,
                "platform": platform.platform(),
                "uptime_sec": time.monotonic() - self._start_time,
            },
            "stt": {
                "model_balanced": _global_settings.MODEL_BALANCED,
                "model_max": _global_settings.MODEL_MAX_CANDIDATES,
                "quality_profile": getattr(self._transcriber.engine, "quality_profile", None) if self._transcriber else None,
                "current_model": getattr(self._transcriber.engine, "current_model", None) if self._transcriber else None,
                "diarization_enabled": _global_settings.DIARIZATION_ENABLED,
                "diarization_device": diarization_device,
                "last_engine": self._last_stt_engine_ref[0] if self._last_stt_engine_ref else "",
            },
            "llm": self._llm_rewriter.status() if self._llm_rewriter else {"enabled": False},
            # 2026-07-15 (спека wake-word-watchdog §4.3): heartbeat-сторож
            # независимого wake-word аудио-потока. Schema-parity fallback,
            # если watchdog не подключён (не роняет get_diagnostics).
            "wake_word_watchdog": (
                self._wake_word_watchdog.state()
                if self._wake_word_watchdog is not None
                else {"enabled": False, "wired": False}
            ),
            # wave-1770 HIGH: suppress transcript count in privacy mode;
            # data_dir paths are always included (needed for diagnostics tooling
            # and don't expose transcript content).
            "history": {
                "total_items": history_count,  # 0 in privacy mode
                "data_dir": str(self.store.data_dir),
                "transcripts_dir": str(Path(self.store.data_dir) / "transcripts"),
            },
            "settings_cache": {
                "ttl_sec": self._settings_svc._cache_ttl if self._settings_svc else 0,
                "cached": self._settings_svc._cache is not None if self._settings_svc else False,
            },
            "profiler": profiler_report,
            # W1685 F5: use injected MetricsCollector (was dead injection — never read).
            # Returns a brief summary for diagnostics panels; safe when collector is None.
            "metrics_summary": self._get_metrics_summary(),
            # Event-мост IPC->REST (spec 2026-07-07-event-bridge-design.md) diagnostics.
            "event_bridge": self._get_event_bridge_summary(),
            # In-process REST (spec 2026-07-16 §4.2): enabled/running/port/error.
            "rest_in_process": self._get_rest_inprocess_summary(),
            # S3/Задача 7b (спека 2026-07-31-s3-rest-flip-design.md §Р6):
            # активный сторож REST. Schema-parity fallback по образцу
            # wake_word_watchdog выше — если сторож не подключён (рубильник
            # REST_IN_PROCESS_ENABLED выключен), get_diagnostics не роняется.
            "rest_watchdog": (
                self._rest_watchdog.state()
                if self._rest_watchdog is not None
                else {"enabled": False, "wired": False}
            ),
            # B3 (spec 2026-07-19-b3-brain-lease-visibility): кто держит LM Studio.
            "brain_lease": self._build_brain_lease_summary(),
        }

    # ------------------------------------------------------------------
    # handle_get_brain_lease_status (B3, spec 2026-07-19)
    # ------------------------------------------------------------------

    def _build_brain_lease_summary(self) -> dict[str, Any]:
        """Снимок brain-lease «кто держит LM Studio» (backend/brain_lease.py).

        Общий билдер для handle_get_brain_lease_status и секции ``brain_lease``
        в get_diagnostics. Никогда не роняет вызывающих: current_lease_holder()
        NEVER raises по контракту модуля, чтение настроек обёрнуто отдельно.
        Payload lock-файла пишет ЧУЖОЙ процесс (Krab userbot) — схеме не
        доверяем, каждое поле коэрсим с fallback null.
        """
        enabled = True
        try:
            if self._settings_svc is not None:
                enabled = bool(
                    self._settings_svc.cached_settings().get("llm_brain_lease_enabled", True)
                )
        except Exception:
            pass

        from backend.brain_lease import current_lease_holder

        summary: dict[str, Any] = {
            "enabled": enabled,
            "held": False,
            "owner": None,
            "pid": None,
            "acquired_ts": None,
            "exp_ts": None,
            "seconds_left": None,
        }
        payload = current_lease_holder()
        if payload is None:
            return summary

        def _as_float(value: Any) -> float | None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _as_int(value: Any) -> int | None:
            # bool — подкласс int; «pid: true» от чужого писателя — мусор, не pid.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return int(value)

        exp_ts = _as_float(payload.get("exp_ts"))
        owner = payload.get("owner")
        summary["held"] = True
        summary["owner"] = str(owner) if owner is not None else None
        summary["pid"] = _as_int(payload.get("pid"))
        summary["acquired_ts"] = _as_float(payload.get("acquired_ts"))
        summary["exp_ts"] = exp_ts
        summary["seconds_left"] = (
            max(0.0, exp_ts - time.time()) if exp_ts is not None else None
        )
        return summary

    def handle_get_brain_lease_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """Кто держит LM Studio «brain» (кросс-процессный лиз ~/.openclaw).

        Только флаги/числа/имя процесса-владельца — privacy-гейт не нужен
        (класс get_privacy_dashboard). Абсолютный lock_path наружу не отдаём
        (урок get_stt_model_status #1814).
        """
        return {"ok": True, **self._build_brain_lease_summary()}

    def _get_event_bridge_summary(self) -> dict[str, Any]:
        """Возвращает EventBridge.get_diagnostics() либо schema-parity fallback.

        Никогда не роняет get_diagnostics (аналог _get_metrics_summary, W1685 F5).
        """
        if self._event_bridge is None:
            return {
                "enabled": False, "state": "disabled",
                "queue_depth": 0, "sent": 0, "dropped": 0, "dropped_stale": 0, "failed": 0,
            }
        try:
            return self._event_bridge.get_diagnostics()
        except Exception:
            logger.warning("HealthCheckService: EventBridge.get_diagnostics() упал", exc_info=True)
            return {
                "enabled": False, "state": "error",
                "queue_depth": 0, "sent": 0, "dropped": 0, "dropped_stale": 0, "failed": 0,
            }

    def _get_rest_inprocess_summary(self) -> dict[str, Any]:
        """Возвращает InProcessRestServer.status() либо schema-parity fallback.

        Никогда не роняет get_diagnostics (аналог _get_event_bridge_summary).
        Отсутствие коллаборатора — не ошибка: рубильник REST_IN_PROCESS_ENABLED
        по умолчанию выключен, поэтому честный ответ здесь "выключен", а не
        пустой словарь.
        """
        if self._rest_inprocess is None:
            return {"enabled": False, "running": False, "port": None, "error": None}
        try:
            return dict(self._rest_inprocess.status())
        except Exception:
            logger.warning("HealthCheckService: InProcessRestServer.status() упал", exc_info=True)
            return {"enabled": False, "running": False, "port": None, "error": "status_failed"}

    def _get_metrics_summary(self) -> dict[str, Any]:
        """Возвращает краткий снимок MetricsCollector для диагностического вывода.

        Возвращает ``{"available": False}`` если collector не был передан при
        инициализации (необязательный параметр). Обёрнуто в try/except — никогда
        не роняет get_diagnostics.
        """
        if self._metrics_collector is None:
            return {"available": False}
        try:
            summary = self._metrics_collector.get_summary()
            return {"available": True, **summary}
        except Exception as exc:
            logger.warning("Не удалось получить снимок MetricsCollector: %s", exc)
            return {"available": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # handle_probe_llm_http
    # ------------------------------------------------------------------

    def handle_probe_llm_http(self, params: dict[str, Any]) -> dict[str, Any]:
        """Однократный ping LM Studio HTTP endpoint. Возвращает reachable, latency_ms, model."""
        if self._llm_rewriter is None:
            return {"reachable": False, "latency_ms": 0, "model": None}
        ok = self._llm_rewriter.warmup()
        return {
            "reachable": bool(ok),
            "latency_ms": getattr(self._llm_rewriter, "_last_latency_ms", 0) or 0,
            "model": getattr(self._llm_rewriter, "_model", None),
        }

    # ------------------------------------------------------------------
    # handle_get_startup_diagnostics
    # ------------------------------------------------------------------

    def handle_get_startup_diagnostics(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает результаты диагностики при старте бэкенда."""
        report = self._startup_diagnostics.run_all_checks()
        return report.to_dict()

    # ------------------------------------------------------------------
    # handle_check_integrity
    # ------------------------------------------------------------------

    def handle_check_integrity(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет целостность файлов данных Krab Ear."""
        report = self._integrity_checker.check_integrity(self.store.data_dir)
        return {
            "status": report.status,
            "total_items": report.total_items,
            "orphaned_tombstones": report.orphaned_tombstones,
            "invalid_json_lines": report.invalid_json_lines,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "auto_fixable": c.auto_fixable,
                }
                for c in report.checks
            ],
        }

    # ------------------------------------------------------------------
    # handle_handshake  (W795 — moved from BackendService)
    # ------------------------------------------------------------------

    def handle_handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        """Swift→backend handshake on connect.

        Verifies version compatibility and returns backend metadata.
        Swift sends this once immediately after establishing a connection.

        Params:
            swift_agent_version (str): Swift agent bundle version, e.g. "1.0.0"
            capabilities (list[str]): declared Swift capabilities,
                e.g. ["error_bus_consumer", "live_subs", "selection_translator"]
        """
        swift_version = params.get("swift_agent_version", "unknown")
        swift_capabilities = params.get("capabilities", [])
        logger.info(
            "IPC handshake: swift_version=%s capabilities=%s",
            swift_version, swift_capabilities,
        )
        return {
            "ok": True,
            "backend_version": self._app_version or "1.0.0",
            "phase_b_capable": True,   # has list_recent_errors, report_paste_failure, etc.
            "phase_c_capable": True,   # has handshake, report_reconnect
            "swift_version_ack": swift_version,
        }
