"""Metadata-часть координированного shutdown Krab Ear backend.

Request-only SIGTERM/SIGINT callback останавливает admission, а обычный
``finally`` после доказанной квиесценции выполняет:
- сохраняет словарь на диск;
- сбрасывает (закрывает) audit log;
- сохраняет статистику использования;
- сохраняет статистику воспроизведения;
- запускает компактирование истории при необходимости;
- при standalone-вызове сначала дренирует IPC-сокет;
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

from backend.shutdown_forensics import _MARKER as _ALIVE_MARKER_FILE

logger = logging.getLogger("KrabEar.Backend.ShutdownHandler")

_SHUTDOWN_INFO_FILE = "shutdown_info.json"

# Сколько не-владелец ждёт чужой shutdown, прежде чем считать барьер недоказанным.
_NON_OWNER_WAIT_TIMEOUT_SEC = 20.0


class GracefulShutdownHandler:
    """Координирует корректное завершение работы backend-сервиса.

    Использование::

        handler = GracefulShutdownHandler(data_dir=data_dir)
        handler.register(service)          # signal только просит IPC-loop выйти
        # … сервис работает …
        # владелец control-flow вызывает handler.shutdown() в finally

    Args:
        data_dir: директория, куда сохраняется ``shutdown_info.json``.
        error_bus: опциональный ``ErrorBus``; если передан, его ``flush_all()``
            вызывается перед финальным выходом, чтобы сбросить в Sentry все
            накопленные warn-tier батчи.
    """

    def __init__(
        self,
        data_dir: str | os.PathLike | None = None,
        error_bus: Any = None,
    ) -> None:
        self._data_dir: Path | None = Path(data_dir) if data_dir else None
        self._service: Any = None
        self._error_bus: Any = error_bus
        self._lock = threading.Lock()
        # _shutdown_started гарантирует, что только один поток выполняет shutdown()
        self._shutdown_started = False
        self._shutdown_done = threading.Event()
        self._shutdown_owner_thread_id: int | None = None
        self._safe_to_close_service = False

        # R1 (2026-07-24): момент старта процесса для uptime_sec в shutdown_info.json;
        # снимок сигнального контекста, который заполняет только _signal_handler().
        self._started_monotonic = time.monotonic()
        self._signal_context: dict[str, Any] | None = None

        # Метаданные последнего завершения — сохраняются в файл
        self._last_shutdown_time: str | None = None
        self._last_shutdown_clean: bool | None = None

        # Загрузить прошлый shutdown_info при старте
        self._load()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def register(self, service: Any) -> None:
        """Привязать сервис и установить request-only signal callback.

        Callback не выполняет teardown: host-loop обязан выйти по IPC-request
        и вызвать ``shutdown()`` из обычного ``finally``. Сервис без
        ``_ipc_server.request_stop_from_signal()`` отклоняется при регистрации,
        чтобы SIGTERM не превратился в тихий no-op.

        В W1787 это намеренно изменённый legacy-контракт: прежний ``register``
        выполнял lock/I/O прямо внутри signal callback и был deadlock-prone.

        Args:
            service: экземпляр ``BackendService`` (или совместимый объект).
                     Ожидаемые опциональные атрибуты:

                - ``vocabulary``  — ``VocabularyStore`` с методом ``load()`` / ``save()``;
                - ``_audit_logger`` — ``AuditLogger`` с методом ``close()``;
                - ``_usage_tracker`` — ``UsageTracker`` с методом ``get_usage_stats()`` и ``_persist()``;
                - ``_playback_tracker`` — ``PlaybackTracker`` с методом ``_save()``;
                - ``store`` — ``StateStore`` с методами ``maybe_compact()`` и свойством ``history_path``;
                - ``_ipc_server`` — обязательный ``IPCServer`` с методами
                  ``request_stop_from_signal()`` и ``stop()``.

        Метод потокобезопасен — повторный вызов заменяет сервис.
        """
        self.bind(service)

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        logger.info("GracefulShutdownHandler зарегистрирован (SIGTERM + SIGINT)")

    @staticmethod
    def _require_ipc_contract(service: Any) -> None:
        """Сервис обязан нести IPC-сервер с полным shutdown-контрактом."""
        server = getattr(service, "_ipc_server", None)
        request_stop = getattr(server, "request_stop_from_signal", None)
        stop = getattr(server, "stop", None)
        if not callable(request_stop) or not callable(stop):
            raise TypeError(
                "shutdown-handler требует _ipc_server с "
                "request_stop_from_signal() и stop()"
            )

    def bind(self, service: Any) -> None:
        """Привязать сервис без перехвата сигналов процесса.

        Production-entrypoint использует этот метод, потому что signal callback
        там только просит IPC accept-loop выйти, а teardown выполняет ``finally``.

        Приёмочное ревью 2026-07-23 (F3): строгая проверка жила только в
        ``register()``, который прод больше не вызывает — гейт стоял на мёртвом
        пути, а живой принимал что угодно.
        """
        self._require_ipc_contract(service)
        with self._lock:
            self._service = service
        logger.debug("GracefulShutdownHandler привязан к сервису без сигналов")

    def shutdown(self, *, ipc_already_stopped: bool = False) -> bool:
        """Сохранить общие ресурсы только после доказанной IPC-квиесценции.

        Первый caller становится владельцем. Конкурентные caller-ы ждут его
        результат, а реентерабельный вызов владельца немедленно получает
        ``False`` и не блокирует сам себя.

        ``ipc_already_stopped`` передаёт владение от production-координатора:
        он уже закрыл admission и дождался всех IPC-handler-ов.

        :returns: ``True``, когда общие ресурсы безопасно закрывать дальше.
        """
        owner_thread_id = threading.get_ident()
        run_shutdown = False
        with self._lock:
            if self._shutdown_started:
                if self._shutdown_owner_thread_id == owner_thread_id:
                    return False
                shutdown_done = self._shutdown_done
            else:
                self._shutdown_started = True
                self._shutdown_owner_thread_id = owner_thread_id
                self._safe_to_close_service = False
                service = self._service
                shutdown_done = self._shutdown_done
                run_shutdown = True

        if not run_shutdown:
            # F4: без таймаута зависший владелец блокировал вызывающего навечно
            # (sticky state without an exit). Недождавшийся — fail-closed.
            if not shutdown_done.wait(timeout=_NON_OWNER_WAIT_TIMEOUT_SEC):
                logger.error(
                    "Ожидание чужого shutdown превысило %.1f с — барьер не доказан",
                    _NON_OWNER_WAIT_TIMEOUT_SEC,
                )
                return False
            with self._lock:
                return self._safe_to_close_service

        shutdown_start = time.monotonic()
        clean = True
        errors: list[str] = []
        safe_to_close_service = False

        logger.info("GracefulShutdownHandler: начинаем завершение…")

        try:
            # IPC — первый ownership-барьер. Пока handler жив, сохранение или
            # закрытие общего store/audit/event_bus создало бы гонку use-after-close.
            if not ipc_already_stopped:
                try:
                    ipc_stopped = self._close_socket(service)
                except Exception as exc:
                    clean = False
                    errors.append(f"socket: {exc}")
                    logger.exception("Ошибка закрытия IPC-сокета при завершении")
                    return False
                if ipc_stopped is False:
                    clean = False
                    errors.append(
                        "socket: IPC-handler-ы не подтвердили завершение"
                    )
                    logger.error(
                        "IPC-квиесценция не подтверждена; persistence пропущен"
                    )
                    return False

            cleanup_steps = (
                (
                    "vocabulary",
                    self._save_vocabulary,
                    "Ошибка сохранения словаря при завершении",
                ),
                (
                    "audit_log",
                    self._flush_audit_log,
                    "Ошибка сброса audit log при завершении",
                ),
                (
                    "usage_stats",
                    self._save_usage_stats,
                    "Ошибка сохранения usage stats при завершении",
                ),
                (
                    "playback_stats",
                    self._save_playback_stats,
                    "Ошибка сохранения playback stats при завершении",
                ),
                (
                    "compact",
                    self._maybe_compact_history,
                    "Ошибка компактирования истории при завершении",
                ),
                (
                    "event_replay",
                    self._close_event_replay,
                    "Ошибка закрытия EventReplayManager при завершении",
                ),
                (
                    "event_bus_sentinel",
                    self._broadcast_event_bus_sentinel,
                    "Ошибка рассылки sentinel в EventBus при завершении",
                ),
            )
            for error_key, cleanup, error_message in cleanup_steps:
                try:
                    cleanup(service)
                except Exception as exc:
                    clean = False
                    errors.append(f"{error_key}: {exc}")
                    logger.exception(error_message)

            try:
                self._close_error_bus()
            except Exception as exc:
                clean = False
                errors.append(f"error_bus: {exc}")
                logger.exception("Ошибка сброса error_bus при завершении")

            elapsed_ms = round((time.monotonic() - shutdown_start) * 1000, 1)
            ts_now = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self._last_shutdown_time = ts_now
                self._last_shutdown_clean = clean
            try:
                self._persist(ts_now, clean, elapsed_ms, errors)
            except Exception as exc:
                # _persist штатно ловит filesystem-ошибки сам; этот guard
                # защищает single-flight event при тестовом/внешнем override.
                clean = False
                errors.append(f"shutdown_info: {exc}")
                with self._lock:
                    self._last_shutdown_clean = False
                logger.exception("Ошибка сохранения shutdown_info при завершении")

            if clean:
                logger.info(
                    "GracefulShutdownHandler: завершение выполнено за %.1f мс",
                    elapsed_ms,
                )
            else:
                logger.warning(
                    "GracefulShutdownHandler: завершение с ошибками за %.1f мс: %s",
                    elapsed_ms,
                    "; ".join(errors),
                )

            # Ошибки отдельных metadata-шагов отражаются в clean=False, но
            # IPC уже дренирован: дальнейший close сервиса остаётся безопасным.
            safe_to_close_service = True
            return True
        finally:
            with self._lock:
                if not safe_to_close_service:
                    self._last_shutdown_time = datetime.now(
                        timezone.utc
                    ).isoformat()
                    self._last_shutdown_clean = False
                self._safe_to_close_service = safe_to_close_service
                self._shutdown_owner_thread_id = None
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
                # W975 MEDIUM: правильная логика — started но ещё не done.
                "shutdown_in_progress": (
                    self._shutdown_started and not self._shutdown_done.is_set()
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

    def _close_event_replay(self, service: Any) -> None:
        """Закрывает файл персистенции EventReplayManager. W829 MEDIUM-1."""
        replay = getattr(service, "_event_replay", None)
        if replay is None:
            return
        close = getattr(replay, "close", None)
        if callable(close):
            close()
            logger.debug("EventReplayManager закрыт")

    def _broadcast_event_bus_sentinel(self, service: Any) -> None:
        """Рассылает None-сентинель всем подписчикам EventBus для немедленного закрытия SSE/WS.

        Использует ``event_bus._subscribers`` напрямую через ``broadcast_shutdown_sentinel()``,
        если метод доступен. Безопасен при отсутствии event_bus на сервисе.
        """
        # Try to get event_bus from service attributes first; fall back to global singleton.
        eb = getattr(service, "_event_bus", None) or getattr(service, "event_bus", None)
        if eb is None:
            # Fall back to the module-level singleton used everywhere else.
            try:
                import backend.event_bus as _eb_mod
                eb = _eb_mod.bus
            except Exception:
                logger.debug("EventBus sentinel: не удалось импортировать global bus")
                return
        broadcast = getattr(eb, "broadcast_shutdown_sentinel", None)
        if callable(broadcast):
            sent = broadcast()
            if sent:
                logger.info("EventBus sentinel разослан %d подписчику(-ам) при завершении", sent)
            else:
                logger.debug("EventBus sentinel: нет активных подписчиков при завершении")

    def _close_socket(self, service: Any) -> bool:
        """Остановить IPC и вернуть подтверждение завершения handler-ов."""
        server = getattr(service, "_ipc_server", None)
        if server is None:
            # Осознанно True: IPC-handler-ы принадлежат серверу, поэтому его
            # отсутствие означает, что мешать закрытию ресурсов физически некому.
            # (Ревью 2026-07-23 предлагало fail-closed — отклонено при гейте:
            # неполный сервис теперь отсекается валидацией в bind(), а для
            # embed-сценариев без IPC отказ ронял бы сохранение метаданных.)
            return True
        stop = getattr(server, "stop", None)
        if not callable(stop):
            logger.error("IPC-сервер не предоставляет обязательный stop()")
            return False
        stop_result = stop()
        if stop_result is False:
            logger.error("IPC-сервер не подтвердил завершение handler-ов")
            return False
        logger.debug("IPC-сокет закрыт")
        return True

    def _close_error_bus(self) -> None:
        """Сбрасывает все накопленные warn-tier батчи в Sentry через ErrorBus.flush_all().

        Вызывается последним шагом shutdown() перед записью метаданных, чтобы
        ни один накопленный warn-батч не был молча потерян при корректном завершении.
        """
        # Also check service for _error_bus as a fallback (set via register())
        error_bus = self._error_bus
        if error_bus is None and self._service is not None:
            error_bus = getattr(self._service, "_error_bus", None)
        if error_bus is None:
            return
        flush_all = getattr(error_bus, "flush_all", None)
        if not callable(flush_all):
            return
        flushed = flush_all()
        if flushed:
            logger.info(
                "ErrorBus: %d warn-tier ошибок сброшено в Sentry при завершении", flushed
            )
        else:
            logger.debug("ErrorBus: нет накопленных warn-батчей при завершении")

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
        # R1 (2026-07-24): аддитивные поля форензики — какой сигнал пришёл, сколько
        # процесс прожил, шла ли запись/встреча в момент сигнала. Существующие
        # читатели shutdown_info.json не ломаются (новые ключи, старые не тронуты).
        ctx = self._signal_context or {}
        sig_num = ctx.get("signal")
        try:
            sig_name = signal.Signals(sig_num).name if sig_num is not None else None
        except ValueError:
            sig_name = str(sig_num)
        payload.update({
            "signal": sig_name,
            "uptime_sec": round(time.monotonic() - self._started_monotonic, 1),
            "recording_active": bool(ctx.get("recording_active", False)),
            "meeting_active": bool(ctx.get("meeting_active", False)),
            "pid": os.getpid(),
        })
        tmp_path = path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp_path.replace(path)
        except Exception:
            logger.warning("Не удалось сохранить shutdown_info.json", exc_info=True)
            return

        # R1 Task 6: маркер живой жизни удаляется ТОЛЬКО после доказанной
        # записи shutdown_info.json выше — если запись провалилась (return
        # в except-ветке уже произошёл), маркер обязан остаться, иначе
        # следующий старт ошибочно сочтёт эту смерть graceful (форензика
        # молча потеряется). Удаление — best-effort, ошибка не критична:
        # худший случай — лишний (безвредный) сбор форензики в следующий раз.
        try:
            (self._data_dir / _ALIVE_MARKER_FILE).unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "Не удалось удалить runtime_alive.marker после graceful shutdown",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Обработчик сигналов
    # ------------------------------------------------------------------

    def _capture_signal_context(self, signum: int) -> None:
        """Снять форензический контекст сигнала — какой сигнал пришёл, шла ли
        запись/встреча (R1, 2026-07-24). Исполняется КАК SIGNAL CALLBACK ОС:
        никаких локов, I/O или логирования — только присваивания простых
        объектов через getattr(..., default) (инвариант F1/F5 приёмки #1891).
        recorder.is_recording — @property, берущее recorder._lock, поэтому
        читаем приватный recorder._is_recording НАПРЯМУЮ: racy read осознан
        (bool, CPython атомарное чтение) — это диагностика, не критичная логика.

        Вынесено в отдельный метод (амендмент Task 8, найдено живым e2e-
        смоком), потому что production НЕ регистрирует ``_signal_handler``
        напрямую через ``signal.signal()`` — ``main()`` в service.py держит
        СВОЙ локальный колбэк (единственный владелец OS-сигналов, см.
        ``bind()`` докстринг) и зовёт этот метод явно, чтобы не дублировать
        логику построения ``_signal_context`` в двух местах (sibling-drift).
        """
        service = self._service
        recorder = getattr(service, "recorder", None)
        meeting = getattr(service, "_meeting_svc", None)
        self._signal_context = {
            "signal": signum,
            "recording_active": bool(getattr(recorder, "_is_recording", False)),
            "meeting_active": getattr(meeting, "_session", None) is not None,
        }

    def _signal_handler(self, signum: int, frame: Any) -> None:
        """Signal-safe запрос: teardown выполнит владелец обычного control-flow.

        Используется legacy-путём ``register()`` (см. докстринг метода) и
        напрямую в unit-тестах; production-путь (``main()`` в service.py,
        владеющий ``signal.signal()``) зовёт ``_capture_signal_context()`` +
        ``request_stop_from_signal()`` по отдельности (тот же эффект, без
        дублирования тела метода).
        """
        del frame
        self._capture_signal_context(signum)
        service = self._service
        server = getattr(service, "_ipc_server", None)
        request_stop = getattr(server, "request_stop_from_signal", None)
        if callable(request_stop):
            request_stop()
