"""AudioReinitCoordinator — единственный владелец танца переинициализации
аудио-стека (спека docs/superpowers/specs/2026-07-15-wake-word-watchdog-design.md §4.2).

Танец переехал из AudioSelfHealer._perform_reinit, чтобы AudioSelfHealer
(пассивный триггер — пустые диктовки) и WakeWordWatchdog (активный триггер —
stale heartbeat) делили ОДИН путь лечения с single-flight локом, а не
дрейфующие копии (класс «double-write одного side effect из двух tap'ов»).

Ключевой инвариант: если adapter.stop() вернул False (тред слушателя завис
внутри PortAudio-вызова — сигнатура живого инцидента 2026-07-13), звать
sd._terminate() НЕЛЬЗЯ (Pa_Terminate при заблокированном в библиотеке треде —
риск сегфолта) — возвращаем THREAD_HUNG, лечение уходит на уровень процесса.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("KrabEar.Backend.AudioReinit")

_WAKE_WORD_THRESHOLD_DEFAULT = 0.5


class ReinitOutcome(str, Enum):
    OK = "ok"
    DEFERRED_RECORDING = "deferred_recording"
    THREAD_HUNG = "thread_hung"
    BUSY = "busy"
    FAILED = "failed"


class AudioReinitCoordinator:
    """Single-flight танец: сохранить wake-word сессию → stop() →
    reinit PortAudio → восстановить сессию.

    Parameters
    ----------
    reinit_audio_backend:
        Zero-arg callable (production: ``sd._terminate(); sd._initialize()``).
    is_recording:
        Zero-arg callable; True — идёт диктовка, reinit откладывается.
    wake_word_adapter:
        Duck-typed OpenWakeWordAdapter (``is_running``/``active_model``/
        ``active_threshold``/``stop``/``start``) или None.
    """

    def __init__(
        self,
        *,
        reinit_audio_backend: Callable[[], None],
        is_recording: Callable[[], bool],
        wake_word_adapter: Any = None,
    ) -> None:
        self._reinit_audio_backend = reinit_audio_backend
        self._is_recording = is_recording
        self._wake_word_adapter = wake_word_adapter
        # Non-blocking single-flight: конкурент получает BUSY и приходит со
        # своим следующим триггером, а не ждёт в блокировке.
        self._flight_lock = threading.Lock()

    def reinit_with_wake_word_restore(self) -> ReinitOutcome:
        if not self._flight_lock.acquire(blocking=False):
            logger.info("AudioReinitCoordinator: reinit уже идёт — BUSY")
            return ReinitOutcome.BUSY
        try:
            return self._dance()
        finally:
            self._flight_lock.release()

    # ------------------------------------------------------------------

    def _dance(self) -> ReinitOutcome:
        try:
            if self._is_recording():
                logger.info(
                    "AudioReinitCoordinator: идёт активная запись — reinit отложен"
                )
                return ReinitOutcome.DEFERRED_RECORDING
        except Exception:
            logger.exception("AudioReinitCoordinator: is_recording() упал")

        saved_model: str | None = None
        saved_threshold: float | None = None
        was_running = False
        adapter = self._wake_word_adapter

        if adapter is not None:
            try:
                was_running = bool(adapter.is_running())
            except Exception:
                logger.exception("AudioReinitCoordinator: is_running() упал")
                was_running = False
            if was_running:
                try:
                    saved_model = adapter.active_model()
                    get_thr = getattr(adapter, "active_threshold", None)
                    saved_threshold = get_thr() if callable(get_thr) else None
                except Exception:
                    logger.exception(
                        "AudioReinitCoordinator: не удалось прочитать "
                        "состояние wake word перед reinit"
                    )
                try:
                    stopped = adapter.stop()
                except Exception:
                    logger.exception("AudioReinitCoordinator: adapter.stop() упал")
                    stopped = False
                # None — легаси duck-type без возврата: трактуем как успех.
                if stopped is False:
                    logger.error(
                        "AudioReinitCoordinator: тред слушателя не вышел — "
                        "Pa_Terminate небезопасен, THREAD_HUNG"
                    )
                    return ReinitOutcome.THREAD_HUNG

        reinit_failed = False
        logger.warning(
            "AudioReinitCoordinator: переинициализация аудио-стека (PortAudio)"
        )
        try:
            self._reinit_audio_backend()
        except Exception:
            logger.exception(
                "AudioReinitCoordinator: reinit_audio_backend завершился с исключением"
            )
            reinit_failed = True

        if adapter is not None and was_running and saved_model:
            try:
                adapter.start(
                    saved_model,
                    self._on_wake_word_detected_after_reinit,
                    threshold=(
                        saved_threshold
                        if saved_threshold is not None
                        else _WAKE_WORD_THRESHOLD_DEFAULT
                    ),
                )
            except Exception:
                logger.exception(
                    "AudioReinitCoordinator: не удалось перезапустить wake word "
                    "после reinit"
                )
                reinit_failed = True

        return ReinitOutcome.FAILED if reinit_failed else ReinitOutcome.OK

    @staticmethod
    def _on_wake_word_detected_after_reinit(model_name: str, score: float) -> None:
        """Доставка детекций агенту идёт через _record_detection() безусловно
        внутри цикла слушателя — этому callback'у достаточно лога."""
        logger.info(
            "AudioReinitCoordinator: wake word обнаружен после reinit "
            "(model=%r, score=%.3f)",
            model_name, score,
        )
