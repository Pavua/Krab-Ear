"""Мониторинг дискового пространства для Krab Ear.

DiskSpaceMonitor периодически проверяет свободное место на диске и размер
файлов данных. При превышении порогов эмитит события через EventBus.

Поддерживаемые события:
- disk.warning    — свободное место < DISK_WARNING_GB
- disk.critical   — свободное место < DISK_CRITICAL_GB
- disk.history_large — history.ndjson > HISTORY_LARGE_MB

При DISK_MONITOR_ENABLED=False мониторинг не запускается.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.config import Settings
    from backend.event_bus import EventBus

logger = logging.getLogger("KrabEar.Backend.DiskMonitor")


class DiskSpaceMonitor:
    """Фоновый монитор дискового пространства.

    Запускает daemon-поток, который раз в DISK_CHECK_INTERVAL_MIN минут
    проверяет состояние диска и эмитит warning/critical/history_large события.

    Thread-safe: start() / stop() можно вызывать из разных потоков.
    """

    def __init__(
        self,
        settings: "Settings",
        event_bus: "EventBus",
        data_dir: Path,
    ) -> None:
        self._settings = settings
        self._event_bus = event_bus
        self._data_dir = Path(data_dir)

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Кэш последнего статуса для get_status()
        self._last_status: dict[str, Any] = {}
        self._last_check_ts: str | None = None

        # Хранит last-emitted level, чтобы не дублировать одинаковые события
        self._last_disk_level: str | None = None
        self._last_history_large_emitted: bool = False

        # F1 (wave-33 MED): 30s in-process cache для get_disk_status IPC-метода.
        # _collect_status() делает рекурсивный rglob-обход (transcripts/ + history.ndjson),
        # что при тысячах файлов saturates I/O. При cache hit возвращаем cached dict
        # без rglob. Cache обновляется фоновым потоком и при check_now().
        self._disk_cache: dict[str, Any] = {}
        self._disk_cache_ts: float = 0.0
        _DISK_CACHE_TTL_SEC: float = 30.0
        self._DISK_CACHE_TTL_SEC: float = _DISK_CACHE_TTL_SEC

        # F2 (wave-33 MED): минимальный интервал между forced checks (force=True)
        # предотвращает re-emit SSE событий при частых вызовах check_now() из IPC.
        # Первый forced check всегда выполняется (ts=0.0).
        self._last_force_ts: float = 0.0
        _FORCE_MIN_INTERVAL_SEC: float = 60.0
        self._FORCE_MIN_INTERVAL_SEC: float = _FORCE_MIN_INTERVAL_SEC

        # Late-injected by BackendService after construction (Phase B Wave 60).
        # If None, disk.low_space errors are not pushed to the error bus.
        self._error_bus: Any | None = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает фоновый поток мониторинга.

        Если DISK_MONITOR_ENABLED=False — ничего не делает.
        """
        if not self._settings.DISK_MONITOR_ENABLED:
            logger.debug("DiskSpaceMonitor отключён (DISK_MONITOR_ENABLED=False)")
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("DiskSpaceMonitor уже запущен")
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="DiskSpaceMonitor",
            )
            self._thread.start()
            logger.info(
                "DiskSpaceMonitor запущен (интервал=%d мин, warn=%.1f GB, crit=%.1f GB)",
                self._settings.DISK_CHECK_INTERVAL_MIN,
                self._settings.DISK_WARNING_GB,
                self._settings.DISK_CRITICAL_GB,
            )

    def stop(self) -> None:
        """Graceful shutdown: дожидается завершения потока (до 5 с)."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.debug("DiskSpaceMonitor остановлен")

    def get_status(self) -> dict[str, Any]:
        """Возвращает последний известный статус диска.

        Возвращает:
            free_space_gb (float): свободное место на диске
            data_dir_mb (float): размер ~/.krab_ear_data/
            history_mb (float): размер history.ndjson
            transcripts_mb (float): размер transcripts/
            level (str): "ok" | "warning" | "critical"
            history_large (bool): history.ndjson > HISTORY_LARGE_MB
            last_check_ts (str | None): ISO 8601 время последней проверки
            enabled (bool): включён ли монитор
        """
        with self._lock:
            status = dict(self._last_status)
        status["enabled"] = self._settings.DISK_MONITOR_ENABLED
        return status

    def check_now(self) -> dict[str, Any]:
        """Выполняет немедленную проверку (синхронно) и возвращает статус.

        Используется для IPC handle_get_disk_status().

        F2 (wave-33 MED): повторный forced check в течение 60 s возвращает
        cached result без повторного I/O и без re-emit SSE событий.
        Первый вызов (self._last_force_ts == 0.0) всегда выполняется.
        """
        now = time.monotonic()
        with self._lock:
            since_last_force = now - self._last_force_ts
            if since_last_force < self._FORCE_MIN_INTERVAL_SEC and self._disk_cache:
                # Возвращаем кэш без re-emit
                return dict(self._disk_cache)

        status = self._collect_status()
        self._evaluate_and_emit(status, force=True)

        with self._lock:
            self._last_force_ts = time.monotonic()
            self._disk_cache = dict(status)
            self._disk_cache_ts = self._last_force_ts

        return status

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Основной цикл фонового потока."""
        # Первая проверка — сразу при старте
        try:
            status = self._collect_status()
            self._evaluate_and_emit(status)
        except Exception:
            logger.exception("DiskSpaceMonitor: ошибка первой проверки")

        interval_sec = float(self._settings.DISK_CHECK_INTERVAL_MIN or 5) * 60

        while not self._stop_event.wait(timeout=interval_sec):
            try:
                status = self._collect_status()
                self._evaluate_and_emit(status)
            except Exception:
                logger.exception("DiskSpaceMonitor: ошибка проверки")

    def _collect_status(self) -> dict[str, Any]:
        """Собирает текущие метрики диска без побочных эффектов."""
        # Свободное место на разделе, где находится data_dir
        try:
            usage = shutil.disk_usage(self._data_dir)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
        except Exception:
            logger.exception(
                "DiskSpaceMonitor: не удалось получить disk_usage для %s",
                self._data_dir,
            )
            free_gb = -1.0
            total_gb = -1.0

        history_path = self._data_dir / "history.ndjson"
        history_mb = self._safe_size_mb(history_path)

        transcripts_dir = self._data_dir / "transcripts"
        transcripts_mb = self._dir_size_mb(transcripts_dir)

        data_dir_mb = self._dir_size_mb(self._data_dir)

        # Определяем уровень предупреждения
        if free_gb >= 0 and free_gb < self._settings.DISK_CRITICAL_GB:
            level = "critical"
        elif free_gb >= 0 and free_gb < self._settings.DISK_WARNING_GB:
            level = "warning"
        else:
            level = "ok"

        # Wave 546: defensive cast — settings.HISTORY_LARGE_MB may be a MagicMock
        # (or other non-numeric) when DiskSpaceMonitor is instantiated by tests
        # that don't stub the field explicitly. Cast to float with safe fallback
        # to module default (500 MB) avoids TypeError without forcing all tests
        # to know about every settings field. Affects production safety only
        # under truly corrupt settings (logged elsewhere by SettingsValidator).
        try:
            _history_large_threshold = float(self._settings.HISTORY_LARGE_MB)
        except (TypeError, ValueError):
            _history_large_threshold = 500.0
        history_large = history_mb >= _history_large_threshold
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        status: dict[str, Any] = {
            "free_space_gb": round(free_gb, 3),
            "total_space_gb": round(total_gb, 3),
            "data_dir_mb": round(data_dir_mb, 3),
            "history_mb": round(history_mb, 3),
            "transcripts_mb": round(transcripts_mb, 3),
            "level": level,
            "history_large": history_large,
            "last_check_ts": ts,
        }

        with self._lock:
            self._last_status = status
            self._last_check_ts = ts
            # F1 (wave-33 MED): обновляем 30s cache после каждого полного сбора метрик.
            # check_now() использует этот cache при повторных forced-вызовах в течение 60s.
            self._disk_cache = dict(status)
            self._disk_cache_ts = time.monotonic()

        return status

    def _evaluate_and_emit(self, status: dict[str, Any], force: bool = False) -> None:
        """Принимает решение об эмите событий на основе статуса.

        force=True: эмитит события без проверки дубликатов (для check_now).
        force=False: подавляет повтор одного и того же уровня предупреждения.
        """
        if not self._settings.DISK_MONITOR_ENABLED:
            return

        level = status["level"]
        history_large = status["history_large"]
        free_gb = status["free_space_gb"]
        history_mb = status["history_mb"]

        # Disk level events
        if level in ("warning", "critical"):
            if force or level != self._last_disk_level:
                self._event_bus.emit(f"disk.{level}", {
                    "free_space_gb": free_gb,
                    "threshold_gb": (
                        self._settings.DISK_CRITICAL_GB
                        if level == "critical"
                        else self._settings.DISK_WARNING_GB
                    ),
                    "level": level,
                })
                logger.warning(
                    "Дисковое пространство %s: %.2f GB свободно",
                    level.upper(),
                    free_gb,
                )
                self._last_disk_level = level
                # Wave 60: push disk.low_space to error bus (severity matches level)
                self._push_disk_error(level, free_gb)
                # Wave 490: push dedicated disk.critical error when level == critical
                if level == "critical":
                    self._push_disk_critical_error(free_gb)

        else:
            # Сброс состояния при возврате к норме
            if self._last_disk_level is not None:
                self._last_disk_level = None

        # History large event
        if history_large:
            if force or not self._last_history_large_emitted:
                self._event_bus.emit("disk.history_large", {
                    "history_mb": history_mb,
                    "threshold_mb": self._settings.HISTORY_LARGE_MB,
                })
                logger.warning(
                    "Файл history.ndjson большой: %.1f MB (порог %d MB)",
                    history_mb,
                    self._settings.HISTORY_LARGE_MB,
                )
                self._last_history_large_emitted = True
        else:
            self._last_history_large_emitted = False

    def _push_disk_error(self, level: str, free_gb: float) -> None:
        """Push disk.low_space error to error bus. Never raises.

        Wave 60: severity mirrors threshold level (warn for DISK_WARNING_GB,
        critical for DISK_CRITICAL_GB) by overriding the registry default.
        """
        error_bus = self._error_bus
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            severity = "critical" if level == "critical" else "warn"
            entry = ERROR_REGISTRY.get("disk.low_space", {})
            err = KrabError(
                severity=severity,
                component="disk",
                code="disk.low_space",
                message_user=entry.get("user_msg_ru", "Мало места на диске"),
                message_debug=f"disk.{level}: {free_gb:.2f} GB free",
                timestamp=datetime.now(timezone.utc),
                context={"level": level, "free_gb": free_gb},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("DiskSpaceMonitor: error_bus.push failed")

    def _push_disk_critical_error(self, free_gb: float) -> None:
        """Push disk.critical error to error bus. Never raises.

        Wave 490: dedicated critical code (separate from disk.low_space warn).
        """
        error_bus = self._error_bus
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("disk.critical", {})
            err = KrabError(
                severity="critical",
                component="disk",
                code="disk.critical",
                message_user=entry.get(
                    "user_msg_ru",
                    "🔴 КРИТИЧНО: меньше 1 GB на диске",
                ),
                message_debug=f"disk.critical: {free_gb:.2f} GB free",
                timestamp=datetime.now(timezone.utc),
                context={"level": "critical", "free_gb": free_gb},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("DiskSpaceMonitor: disk.critical error_bus.push failed")

    # ------------------------------------------------------------------
    # Утилиты
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_size_mb(path: Path) -> float:
        """Возвращает размер файла в MB или 0.0 если файл не найден."""
        try:
            return path.stat().st_size / (1024 * 1024)
        except (FileNotFoundError, OSError):
            return 0.0

    @staticmethod
    def _dir_size_mb(directory: Path) -> float:
        """Возвращает суммарный размер всех файлов в директории (рекурсивно), MB."""
        if not directory.exists():
            return 0.0
        total = 0
        try:
            for f in directory.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except (OSError, FileNotFoundError):
                        pass
        except (OSError, PermissionError):
            pass
        return total / (1024 * 1024)
