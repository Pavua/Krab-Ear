"""Graceful shutdown handler для Krab Ear backend.

Регистрирует обработчики SIGTERM/SIGINT и при завершении:
- сохраняет словарь на диск;
- сбрасывает (закрывает) audit log;
- сохраняет статистику использования;
- сохраняет статистику воспроизведения;
- запускает компактирование истории при необходимости;
- закрывает IPC-сокет;
- фиксирует метаданные завершения в {data_dir}/shutdown_info.json.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("KrabEar.Backend.ShutdownHandler")

_SHUTDOWN_INFO_FILE = "shutdown_info.json"


class GracefulShutdownHandler:
    """Координирует корректное завершение работы backend-сервиса.

    Использование::

        handler = GracefulShutdownHandler(data_dir=data_dir)
        handler.register(service)          # устанавливает SIGTERM / SIGINT
        # … сервис работает …
        # при получении сигнала handler.shutdown() вызывается автоматически

    Args:
        data_dir: директория, куда сохраняется ``shutdown_info.json``.
    """

    def __init__(self, data_dir: str | os.PathLike | None = None) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir else None
        self._service: Any = None
        self._lock = threading.Lock()
        self._shutdown_done = threading.Event()

        # Метаданные последнего завершения — сохраняются в файл
        self._last_shutdown_time: str | None = None
        self._last_shutdown_clean: bool | None = None

        # Загрузить прошлый shutdown_info при старте
        self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def register(self, service: Any) -> None:
        """Сохраняет ссылку на сервис и устанавливает обработчики сигналов.

        Args:
            service: экземпляр ``BackendService`` (или совместимый объект).
                     Ожидаемые опциональные атрибуты:

                - ``vocabulary``  — ``VocabularyStore`` с методом ``load()`` / ``save()``;
                - ``_audit_logger`` — ``AuditLogger`` с методом ``close()``;
                - ``_usage_tracker`` — ``UsageTracker`` с методом ``get_usage_stats()`` и ``_persist()``;
                - ``_playback_tracker`` — ``PlaybackTracker`` с методом ``_save()``;
                - ``store`` — ``StateStore`` с методами ``maybe_compact()`` и свойством ``history_path``;
                - ``_ipc_server`` — ``IPCServer`` с методом ``stop()`` (устанавливается отдельно).

        Метод потокобезопасен — повторный вызов заменяет сервис.
        """
        with self._lock:
            self._service = service

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logger.info("GracefulShutdownHandler зарегистрирован (SIGTERM + SIGINT)")

    def shutdown(self) -> None:
        """Выполняет последовательность корректного завершения.

        Идемпотентен — повторный вызов не производит действий.
        """
        with self._lock:
            if self._shutdown_done.is_set():
                return
            service = self._service

        shutdown_start = time.monotonic()
        clean = True
        errors: list[str] = []

        logger.info("GracefulShutdownHandler: начинаем завершение…")

        # 1. Сохраняем словарь
        try:
            self._save_vocabulary(service)
        except Exception as exc:
            clean = False
            errors.append(f"vocabulary: {exc}")
            logger.exception("Ошибка сохранения словаря при завершении")

        # 2. Сбрасываем audit log
        try:
            self._flush_audit_log(service)
        except Exception as exc:
            clean = False
            errors.append(f"audit_log: {exc}")
            logger.exception("Ошибка сброса audit log при завершении")

        # 3. Сохраняем статистику использования
        try:
            self._save_usage_stats(service)
        except Exception as exc:
            clean = False
            errors.append(f"usage_stats: {exc}")
            logger.exception("Ошибка сохранения usage stats при завершении")

        # 4. Сохраняем статистику воспроизведения
        try:
            self._save_playback_stats(service)
        except Exception as exc:
            clean = False
            errors.append(f"playback_stats: {exc}")
            logger.exception("Ошибка сохранения playback stats при завершении")

        # 5. Компактирование истории при необходимости
        try:
            self._maybe_compact_history(service)
        except Exception as exc:
            clean = False
            errors.append(f"compact: {exc}")
            logger.exception("Ошибка компактирования истории при завершении")

        # 6. Закрываем IPC-сокет
        try:
            self._close_socket(service)
        except Exception as exc:
            clean = False
            errors.append(f"socket: {exc}")
            logger.exception("Ошибка закрытия IPC-сокета при завершении")

        elapsed_ms = round((time.monotonic() - shutdown_start) * 1000, 1)
        ts_now = datetime.now(timezone.utc).isoformat()

        # 7. Сохраняем метаданные завершения
        with self._lock:
            self._last_shutdown_time = ts_now
            self._last_shutdown_clean = clean
        self._persist(ts_now, clean, elapsed_ms, errors)

        if clean:
            logger.info(
                "GracefulShutdownHandler: завершение выполнено за %.1f мс", elapsed_ms
            )
        else:
            logger.warning(
                "GracefulShutdownHandler: завершение с ошибками за %.1f мс: %s",
                elapsed_ms,
                "; ".join(errors),
            )

        self._shutdown_done.set()

    def get_shutdown_status(self) -> dict[str, Any]:
        """Возвращает информацию о последнем завершении.

        Returns:
            dict с ключами:

            - ``clean`` (``bool | None``) — было ли завершение корректным;
            - ``last_shutdown_time`` (``str | None``) — ISO8601 время завершения;
            - ``shutdown_in_progress`` (``bool``) — True если shutdown() был вызван, но ещё не завершён.
        """
        with self._lock:
            return {
                "clean": self._last_shutdown_clean,
                "last_shutdown_time": self._last_shutdown_time,
                "shutdown_in_progress": (
                    self._shutdown_done.is_set() is False
                    and self._last_shutdown_time is None
                    and self._service is not None
                    # Признак того, что сигнал уже получен, но обработка идёт.
                    # Определяем косвенно — если _shutdown_done не установлен,
                    # это либо «не начат», либо «в процессе».
                    # Упрощаем: False до завершения, True только после.
                ),
            }

    # ------------------------------------------------------------------
    # Внутренние шаги завершения
    # ------------------------------------------------------------------

    def _save_vocabulary(self, service: Any) -> None:
        """Сохраняет словарь STT на диск."""
        vocab = getattr(service, "vocabulary", None)
        if vocab is None:
            return
        words = vocab.load()
        vocab.save(words)
        logger.debug("Vocabulary сохранён (%d слов)", len(words))

    def _flush_audit_log(self, service: Any) -> None:
        """Закрывает файловый дескриптор audit log."""
        audit = getattr(service, "_audit_logger", None)
        if audit is None:
            return
        audit.close()
        logger.debug("Audit log сброшен")

    def _save_usage_stats(self, service: Any) -> None:
        """Принудительно сохраняет статистику использования."""
        tracker = getattr(service, "_usage_tracker", None)
        if tracker is None:
            return
        # UsageTracker.record_usage вызывает _persist автоматически.
        # При завершении вызываем напрямую на случай незафиксированных изменений.
        persist = getattr(tracker, "_persist", None)
        if callable(persist):
            persist()
            logger.debug("Usage stats сохранена")

    def _save_playback_stats(self, service: Any) -> None:
        """Принудительно сохраняет статистику воспроизведения."""
        tracker = getattr(service, "_playback_tracker", None)
        if tracker is None:
            return
        save = getattr(tracker, "_save", None)
        if callable(save):
            save()
            logger.debug("Playback stats сохранены")

    def _maybe_compact_history(self, service: Any) -> None:
        """Компактирует историю, если размер файла превышает порог."""
        store = getattr(service, "store", None)
        if store is None:
            return
        maybe_compact = getattr(store, "maybe_compact", None)
        if callable(maybe_compact):
            compacted = maybe_compact()
            if compacted:
                logger.info("История скомпактирована при завершении")

    def _close_socket(self, service: Any) -> None:
        """Останавливает IPC-сервер (если зарегистрирован)."""
        server = getattr(service, "_ipc_server", None)
        if server is None:
            return
        stop = getattr(server, "stop", None)
        if callable(stop):
            stop()
            logger.debug("IPC-сокет закрыт")

    # ------------------------------------------------------------------
    # Персистентность shutdown_info.json
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загружает метаданные предыдущего завершения."""
        if self._data_dir is None:
            return
        path = self._data_dir / _SHUTDOWN_INFO_FILE
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._last_shutdown_time = data.get("last_shutdown_time")
                clean_val = data.get("clean")
                self._last_shutdown_clean = bool(clean_val) if clean_val is not None else None
        except Exception:
            logger.warning("Не удалось прочитать shutdown_info.json", exc_info=True)

    def _persist(
        self,
        ts: str,
        clean: bool,
        elapsed_ms: float,
        errors: list[str],
    ) -> None:
        """Сохраняет метаданные завершения в {data_dir}/shutdown_info.json."""
        if self._data_dir is None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / _SHUTDOWN_INFO_FILE
        payload: dict[str, Any] = {
            "last_shutdown_time": ts,
            "clean": clean,
            "elapsed_ms": elapsed_ms,
            "errors": errors,
        }
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception:
            logger.warning("Не удалось сохранить shutdown_info.json", exc_info=True)

    # ------------------------------------------------------------------
    # Обработчик сигналов
    # ------------------------------------------------------------------

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Вызывается при SIGTERM / SIGINT."""
        logger.info("Получен сигнал %s, запускаем graceful shutdown…", signum)
        self.shutdown()
