"""Watchdog за длительностью обычной (не-meeting) записи.

Живой инцидент 2026-08-05: хоткейная диктовка провисела незамеченной
52 минуты, что обвалило весь STT-fallback конвейер (GigaAM деградировал до
9 символов на 151 чанке → Whisper упёрся в собственный предел на такой
длине → Remote STT отвалился без ключа → критическая ошибка распознавания),
спасена только backstop-таймаутом IPC (caf4089d). У звонков уже есть
CallAutoEnd с max_duration; у обычной диктовки такого предохранителя не
было.

RecordingDurationWatchdog — простой периодический монитор (тот же паттерн,
что DiskSpaceMonitor): раз в RECORDING_DURATION_CHECK_INTERVAL_SEC секунд
проверяет активную запись через AudioRecorder.is_recording/get_duration_sec
и пушит через error_bus предупреждение, когда запись идёт дольше
RECORDING_DURATION_WARN_SEC — задолго до порога, на котором начинается
деградация STT. Watchdog ТОЛЬКО ЧИТАЕТ состояние рекордера — не управляет
lifecycle записи и не взаимодействует с generation-ownership CAS (R2),
поэтому не несёт риска для того контракта.

2026-08-05, Fable-ревью MEDIUM-2/MEDIUM-B: recorder ОБЩИЙ между диктовкой/
quick-capture (незамеченный запуск — реальная проблема, см. модуль) и
ДРУГИМИ владельцами с легитимно долгой записью — meeting (C2 Live Meeting
Overlay) И Call Assist (CallAssistService захватывает recorder НАПРЯМУЮ,
без generation вообще — current_recording_owner() вернёт None). Изначальная
версия исключала только "owner == meeting" — MEDIUM-B поймал, что Call
Assist (owner=None) всё равно ловил нагоняющие тосты всю легитимную беседу.
Правильная форма — INCLUSION, не EXCLUSION: `owner_is_dictation_like` —
опциональный колбэк, возвращающий True только для owner'ов, которые
ДЕЙСТВИТЕЛЬНО рискуют быть забытыми (dictation/quick_capture). False/None-
owner (meeting, call assist, любой БУДУЩИЙ невладеющий caller) —
автоматически исключены без необходимости перечислять каждый новый случай.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from core.config import Settings
    from backend.error_bus import ErrorBus
    from backend.recorder import AudioRecorder

logger = logging.getLogger("KrabEar.Backend.RecordingDurationWatchdog")


class RecordingDurationWatchdog:
    """Фоновый монитор длительности активной записи."""

    def __init__(
        self,
        settings: "Settings",
        error_bus: "ErrorBus | None",
        recorder: "AudioRecorder",
        owner_is_dictation_like: "Callable[[], bool] | None" = None,
    ) -> None:
        self._settings = settings
        self._error_bus = error_bus
        self._recorder = recorder
        self._owner_is_dictation_like = owner_is_dictation_like

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запускает фоновый поток мониторинга.

        Если RECORDING_DURATION_WATCHDOG_ENABLED=False — ничего не делает.
        """
        if not self._settings.RECORDING_DURATION_WATCHDOG_ENABLED:
            logger.debug(
                "RecordingDurationWatchdog отключён "
                "(RECORDING_DURATION_WATCHDOG_ENABLED=False)"
            )
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.debug("RecordingDurationWatchdog уже запущен")
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                daemon=True,
                name="RecordingDurationWatchdog",
            )
            self._thread.start()
            logger.info(
                "RecordingDurationWatchdog запущен (warn=%.0fs, interval=%.0fs)",
                float(self._settings.RECORDING_DURATION_WARN_SEC),
                float(self._settings.RECORDING_DURATION_CHECK_INTERVAL_SEC),
            )

    def stop(self) -> None:
        """Graceful shutdown: дожидается завершения потока (до 5 с)."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        logger.debug("RecordingDurationWatchdog остановлен")

    def tick(self) -> None:
        """Один цикл проверки. Вызывается фоновым потоком и напрямую тестами.

        Никогда не бросает — ошибка проверки не должна убить поток watchdog'а.
        """
        try:
            if not self._recorder.is_recording:
                return
            # 2026-08-05 MEDIUM-B (Fable): inclusion, не exclusion — owner
            # может быть meeting, call assist (None, без generation) или
            # любой БУДУЩИЙ невладеющий caller; предупреждаем ТОЛЬКО за
            # owner'ов, реально рискующих остаться забытыми.
            if (
                self._owner_is_dictation_like is not None
                and not self._owner_is_dictation_like()
            ):
                return
            duration_sec = float(self._recorder.get_duration_sec())
            warn_sec = float(self._settings.RECORDING_DURATION_WARN_SEC)
            if duration_sec >= warn_sec:
                self._push_warning(duration_sec)
        except Exception:
            logger.exception("RecordingDurationWatchdog: ошибка проверки")

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _run(self) -> None:
        # 2026-08-05 LOW-4 (Fable): `or 30` ловит только ровно 0, отрицательное
        # значение (env-опечатка) даёт Event.wait(timeout<0) → возврат сразу
        # → CPU spin. max(1.0, ...) закрывает и 0, и отрицательные значения.
        interval_sec = max(
            1.0, float(self._settings.RECORDING_DURATION_CHECK_INTERVAL_SEC or 30)
        )
        while not self._stop_event.wait(timeout=interval_sec):
            self.tick()

    def _push_warning(self, duration_sec: float) -> None:
        """Push recording.long_duration_warning to error bus. Никогда не бросает."""
        error_bus = self._error_bus
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone

            entry: dict[str, Any] = ERROR_REGISTRY.get(
                "recording.long_duration_warning", {}
            )
            err = KrabError(
                severity=entry.get("severity", "warn"),
                component="recording",
                code="recording.long_duration_warning",
                message_user=entry.get(
                    "user_msg_ru",
                    "Запись идёт долго — не забыли остановить?",
                ),
                message_debug=f"recording duration={duration_sec:.0f}s",
                timestamp=datetime.now(timezone.utc),
                context={"duration_sec": duration_sec},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception(
                "RecordingDurationWatchdog: error_bus.push failed"
            )
