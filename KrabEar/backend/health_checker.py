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
        """Проверяет доступность STT-модели.

        Использует только реальные сигналы движка:

        1. mlx_whisper импортируемость — если модуль недоступен, STT физически
           не может работать на этой платформе (→ ``unavailable``).
        2. ``engine._unavailable_models`` — dict {model_id: timestamp}, который
           AudioEngine заполняет при неудачах. Если текущая модель там есть,
           STT деградировал (→ ``unavailable``).
        3. ``engine.current_model`` — имя текущей настроенной модели.

        Предыдущая реализация опиралась на ``engine._whisper_model`` (атрибут,
        которого никогда не существует: MLX хранит веса внутренне и не
        экспонирует Python-хэндл). Следствие: ``cached`` всегда был ``False``,
        а ``current_model`` всегда не-None после ``AudioEngine.__init__`` —
        ветка ``warming_up`` никогда не достигалась, и HealthChecker
        докладывал ``ok`` независимо от реального состояния прогрева.
        Исправлено: ложно-здоровая ветка удалена, добавлены реальные сигналы.

        Ограничение: GPU warm-state (загружены ли веса в Metal-память)
        не наблюдаем из Python без изменений движка. HealthChecker честно
        сообщает ``ok`` при доступной и не-упавшей модели; первый вызов
        может быть холодным — это приемлемо и задокументировано.
        """
        try:
            if self._transcriber is None:
                return {"status": "unavailable", "model": None}

            engine = getattr(self._transcriber, "engine", None)
            if engine is None:
                return {"status": "unavailable", "model": None}

            current_model = getattr(engine, "current_model", None)

            # Реальный сигнал 1: mlx_whisper недоступен на этой платформе.
            # AudioEngine импортирует mlx_whisper на уровне модуля и ставит
            # его в None при сбое. Проверяем через importlib — если импорт
            # падает, STT физически невозможен.
            import importlib
            try:
                mlx_mod = importlib.import_module("mlx_whisper")
                mlx_available = mlx_mod is not None
            except Exception:
                mlx_available = False

            if not mlx_available:
                return {
                    "status": "unavailable",
                    "model": current_model,
                    "detail": "mlx_whisper not importable on this platform",
                }

            # Реальный сигнал 2: текущая модель помечена упавшей движком.
            # engine._unavailable_models хранит метки времени сбоев с TTL ~5 min.
            unavailable_models: dict = getattr(engine, "_unavailable_models", {})
            if current_model and current_model in unavailable_models:
                return {
                    "status": "unavailable",
                    "model": current_model,
                    "detail": "model marked unavailable after failure",
                }

            if current_model:
                return {"status": "ok", "model": current_model}

            # current_model is None — не должно возникать после AudioEngine.__init__,
            # но защищаемся на случай stub/fake движка.
            return {"status": "unavailable", "model": None, "detail": "current_model not set"}

        except Exception as exc:
            logger.warning("stt_model health check failed: %s", exc)
            return {"status": "error", "model": None, "error": str(exc)}

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

        - "unhealthy" если критическая подсистема (stt_model, history_store)
          недоступна (``unavailable``) или в ошибке (``error``/``critical``)
        - "degraded" если любая проверка имеет статус warning/circuit_open/error/critical
        - "healthy" в остальных случаях

        Ранее список деградированных статусов включал ``warming_up`` — статус,
        который был мёртвым кодом в ``_check_stt_model`` (удалён вместе с
        исправлением ложно-здоровой STT-проверки). Убран отсюда тоже.
        """
        critical_checks = {"stt_model", "history_store"}

        for name, result in checks.items():
            status = result.get("status", "ok")
            if status in ("error", "critical", "unavailable") and name in critical_checks:
                return "unhealthy"

        for result in checks.values():
            status = result.get("status", "ok")
            if status in ("warning", "circuit_open", "error", "critical"):
                return "degraded"

        return "healthy"
