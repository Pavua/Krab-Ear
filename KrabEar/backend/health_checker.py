"""Агрегатор проверок состояния Krab Ear backend.

Возвращает структурированный статус по ключевым подсистемам:
STT-модель, LLM, место на диске, хранилище истории, аудиоустройства.
"""

from __future__ import annotations
from KrabEar.__version__ import __version__ as VERSION

import shutil
import time
import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.state_store import StateStore
    from backend.transcriber import Transcriber

logger = logging.getLogger("KrabEar.Backend.HealthChecker")


# Порог «мало места» — 2 ГБ
DISK_WARN_GB = 2.0
DISK_CRIT_GB = 0.5


class HealthChecker:
    """Проверяет состояние всех ключевых подсистем бэкенда."""

    def __init__(
        self,
        store: StateStore,
        transcriber: Transcriber | None = None,
        llm_rewriter: Any | None = None,
        start_time: float | None = None,
    ) -> None:
        self._store = store
        self._transcriber = transcriber
        self._llm_rewriter = llm_rewriter
        self._start_time = start_time if start_time is not None else time.monotonic()

    # ------------------------------------------------------------------
    # Публичный метод
    # ------------------------------------------------------------------

    def check_all(self) -> dict[str, Any]:
        """Запускает все проверки и возвращает агрегированный статус.

        Каждая проверка независима — сбой одной не влияет на другие.
        """
        checks: dict[str, dict[str, Any]] = {}

        checks["stt_model"] = self._check_stt_model()
        checks["llm"] = self._check_llm()
        checks["disk_space"] = self._check_disk_space()
        checks["history_store"] = self._check_history_store()
        checks["audio_devices"] = self._check_audio_devices()

        overall = self._aggregate_status(checks)

        return {
            "status": overall,
            "checks": checks,
            "uptime_sec": round(time.monotonic() - self._start_time, 1),
            "version": VERSION,
        }

    # ------------------------------------------------------------------
    # Отдельные проверки
    # ------------------------------------------------------------------

    def _check_stt_model(self) -> dict[str, Any]:
        """Проверяет доступность STT-модели."""
        try:
            if self._transcriber is None:
                return {"status": "unavailable", "model": None, "cached": False}

            engine = getattr(self._transcriber, "engine", None)
            if engine is None:
                return {"status": "unavailable", "model": None, "cached": False}

            current_model = getattr(engine, "current_model", None)
            # Проверяем, загружена ли модель в память (кэшировано)
            cached = getattr(engine, "_whisper_model", None) is not None

            if current_model:
                return {"status": "ok", "model": current_model, "cached": cached}
            else:
                # current_model is None означает, что MLX ещё не прогрел модель.
                # Если при этом модель не кэширована — это состояние cold-start warming_up,
                # а не "ok". Возвращаем специальный статус, чтобы не давать
                # ложно-здоровый сигнал до завершения инициализации STT.
                if not cached:
                    return {"status": "warming_up", "model": None, "cached": False}
                # cached=True но current_model=None — редкий переходный случай;
                # берём имя модели из конфига как hint.
                try:
                    from core.config import settings
                    model_name = settings.MODEL_BALANCED
                except Exception as exc:
                    logger.debug("Не удалось прочитать MODEL_BALANCED из config: %s", exc)
                    model_name = "unknown"
                return {"status": "ok", "model": model_name, "cached": True}

        except Exception as exc:
            logger.warning("stt_model health check failed: %s", exc)
            return {"status": "error", "model": None, "cached": False, "error": str(exc)}

    def _check_llm(self) -> dict[str, Any]:
        """Проверяет состояние LLM-перезаписчика."""
        try:
            if self._llm_rewriter is None:
                return {"status": "unavailable", "model": None}

            llm_status = self._llm_rewriter.status()
            circuit_state = llm_status.get("circuit_state", "closed")
            model = llm_status.get("model", "unknown")

            if circuit_state == "open":
                return {"status": "circuit_open", "model": model, "circuit_state": circuit_state}
            else:
                return {"status": "ok", "model": model, "circuit_state": circuit_state}

        except Exception as exc:
            logger.warning("llm health check failed: %s", exc)
            return {"status": "error", "model": None, "error": str(exc)}

    def _check_disk_space(self) -> dict[str, Any]:
        """Проверяет свободное место на диске, где хранится data_dir."""
        try:
            data_dir = Path(self._store.data_dir)
            # Если директория ещё не существует, используем родительскую
            check_path = data_dir if data_dir.exists() else data_dir.parent
            usage = shutil.disk_usage(str(check_path))
            free_gb = round(usage.free / (1024 ** 3), 2)

            if free_gb < DISK_CRIT_GB:
                status = "critical"
            elif free_gb < DISK_WARN_GB:
                status = "warning"
            else:
                status = "ok"

            return {"status": status, "free_gb": free_gb}

        except Exception as exc:
            logger.warning("disk_space health check failed: %s", exc)
            return {"status": "error", "free_gb": None, "error": str(exc)}

    def _check_history_store(self) -> dict[str, Any]:
        """Проверяет доступность хранилища истории."""
        try:
            entries = self._store.count_active_items()

            # Размер файла истории
            ndjson_path = Path(self._store.data_dir) / "history.ndjson"
            size_mb = 0.0
            if ndjson_path.exists():
                size_mb = round(ndjson_path.stat().st_size / (1024 * 1024), 2)

            return {"status": "ok", "entries": entries, "size_mb": size_mb}

        except Exception as exc:
            logger.warning("history_store health check failed: %s", exc)
            return {"status": "error", "entries": None, "size_mb": None, "error": str(exc)}

    def _check_audio_devices(self) -> dict[str, Any]:
        """Проверяет доступность аудиоустройств ввода."""
        try:
            import sounddevice as sd  # type: ignore

            devices = sd.query_devices()
            input_devices = [d for d in devices if d.get("max_input_channels", 0) > 0]
            count = len(input_devices)

            default_name = None
            try:
                default_info = sd.query_devices(kind="input")
                default_name = default_info.get("name") if isinstance(default_info, dict) else None
            except Exception as exc:
                logger.debug("Не удалось опросить default audio device: %s", exc)

            if count == 0:
                return {"status": "warning", "count": 0, "default": None}

            return {"status": "ok", "count": count, "default": default_name}

        except ImportError:
            return {"status": "unavailable", "count": 0, "default": None, "error": "sounddevice not installed"}
        except Exception as exc:
            logger.warning("audio_devices health check failed: %s", exc)
            return {"status": "error", "count": 0, "default": None, "error": str(exc)}

    # ------------------------------------------------------------------
    # Агрегация
    # ------------------------------------------------------------------

    def _aggregate_status(self, checks: dict[str, dict[str, Any]]) -> str:
        """Вычисляет общий статус по результатам всех проверок.

        - "unhealthy" если критическая подсистема (stt_model, history_store) недоступна/ошибка
        - "degraded" если любая проверка имеет статус warning/circuit_open
        - "healthy" в остальных случаях
        """
        critical_checks = {"stt_model", "history_store"}

        for name, result in checks.items():
            status = result.get("status", "ok")
            if status in ("error", "critical") and name in critical_checks:
                return "unhealthy"

        for result in checks.values():
            status = result.get("status", "ok")
            if status in ("warning", "circuit_open", "error", "critical", "warming_up"):
                return "degraded"

        return "healthy"
