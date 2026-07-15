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

Второй инвариант: is_recording re-check непосредственно перед Pa_Terminate
(спека §4.2) — запись, стартовавшая за время adapter.stop() (join до 3с),
не должна попасть под Pa_Terminate; остаточное µс-окно между re-check и
_terminate принято (неустранимо без лока на уровне рекордера).

Третий инвариант: maintenance-окно stop→Pa_Terminate закрыто для чужих
start() через begin_maintenance()/end_maintenance() адаптера — иначе
Swift-поллер (self-heal, тик 0.75с), увидев running:false после нашего
stop(), спавнит НОВЫЙ тред слушателя, под которым исполнится Pa_Terminate
(гонка с поллер-self-heal — Critical ревью Task 4). Окно закрывается ДО
restore-фазы: там терминейт уже позади, гонка стартов — benign no-op.
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
            # fail-closed: неизвестное состояние рекордера трактуем как идущую
            # запись — DEFERRED (попытка отложена, не потрачена), а не танец
            # дальше в сторону Pa_Terminate.
            logger.exception("AudioReinitCoordinator: is_recording() упал")
            return ReinitOutcome.DEFERRED_RECORDING

        saved_model: str | None = None
        saved_threshold: float | None = None
        was_running = False
        deferred_mid_dance = False
        reinit_failed = False
        epoch_snapshot: int | None = None
        adapter = self._wake_word_adapter

        # Fix A (Critical, ревью Task 4): окно обслуживания. Пока открыто,
        # adapter.start() отказывает — иначе Swift-поллер (тик 0.75с), увидев
        # running:false после нашего stop(), спавнит НОВЫЙ тред слушателя,
        # под которым исполнится Pa_Terminate (crash-класс). finally закрывает
        # окно ДО _restore_listener: в restore-фазе терминейт уже позади,
        # гонка стартов там вырождается в benign no-op («уже запущен»-гард).
        maintenance_guard = getattr(adapter, "begin_maintenance", None)
        if callable(maintenance_guard):
            maintenance_guard()
        try:
            if adapter is not None:
                # Chip Finding 5: базовое значение stop-epoch ДО танца.
                # Ожидание = base + 1 (наш собственный stop ниже); любой
                # ВНЕШНИЙ stop (toggle-off/pause) в ЛЮБОЙ фазе танца —
                # включая конкурентный с нашим stop-join — сдвигает счётчик
                # мимо ожидания и отменяет restore.
                get_epoch = getattr(adapter, "stop_epoch", None)
                epoch_before = get_epoch() if callable(get_epoch) else None
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
                        logger.exception(
                            "AudioReinitCoordinator: adapter.stop() упал"
                        )
                        stopped = False
                    # None — легаси duck-type без возврата: трактуем как успех.
                    if stopped is False:
                        logger.error(
                            "AudioReinitCoordinator: тред слушателя не вышел — "
                            "Pa_Terminate небезопасен, THREAD_HUNG"
                        )
                        return ReinitOutcome.THREAD_HUNG
                    if epoch_before is not None:
                        # Наш stop() выше двинул счётчик ровно на 1.
                        epoch_snapshot = epoch_before + 1

            # TOCTOU-окно: между первым чеком и этим местом лежал adapter.stop()
            # с join до 3с — диктовка могла стартовать. Pa_Terminate под живым
            # стримом рекордера — тот же crash-класс, что и THREAD_HUNG-инвариант.
            # Остаточное окно (µс между этим чеком и _terminate) неустранимо без
            # лока на уровне рекордера — принято.
            try:
                recording_started_mid_dance = bool(self._is_recording())
            except Exception:
                logger.exception(
                    "AudioReinitCoordinator: is_recording() упал (re-check)"
                )
                recording_started_mid_dance = True  # fail-closed
            if recording_started_mid_dance:
                logger.info(
                    "AudioReinitCoordinator: запись стартовала во время танца — "
                    "reinit отложен, слушатель восстанавливается"
                )
                deferred_mid_dance = True
            else:
                logger.warning(
                    "AudioReinitCoordinator: переинициализация аудио-стека "
                    "(PortAudio)"
                )
                try:
                    self._reinit_audio_backend()
                except Exception:
                    logger.exception(
                        "AudioReinitCoordinator: reinit_audio_backend завершился "
                        "с исключением"
                    )
                    reinit_failed = True
        finally:
            end_guard = getattr(adapter, "end_maintenance", None)
            if callable(end_guard):
                end_guard()

        if deferred_mid_dance:
            self._restore_listener(
                adapter, was_running, saved_model, saved_threshold, epoch_snapshot,
            )
            return ReinitOutcome.DEFERRED_RECORDING

        if not self._restore_listener(
            adapter, was_running, saved_model, saved_threshold, epoch_snapshot,
        ):
            reinit_failed = True

        return ReinitOutcome.FAILED if reinit_failed else ReinitOutcome.OK

    def _restore_listener(
        self,
        adapter: Any,
        was_running: bool,
        saved_model: str | None,
        saved_threshold: float | None,
        epoch_snapshot: int | None = None,
    ) -> bool:
        """Восстановить wake-word слушатель после танца.

        False — adapter.start() упал (вызывающий решает, фатально ли это),
        True — восстановлен либо восстанавливать было нечего.
        """
        if adapter is None or not was_running or not saved_model:
            return True
        # Chip Finding 5 (Fable-гейт волны watchdog): внешний stop во время
        # танца (владелец выключил тумблер / поллер послал pause) не должен
        # «включаться обратно» restore'ом — иначе микрофон wake word слушал
        # бы при выключенном тумблере до следующего рестарта backend.
        # Остаточное µс-окно между этим чеком и start() принято (симметрично
        # is_recording re-check танца).
        get_epoch = getattr(adapter, "stop_epoch", None)
        if (
            epoch_snapshot is not None
            and callable(get_epoch)
            and get_epoch() != epoch_snapshot
        ):
            logger.info(
                "AudioReinitCoordinator: слушатель остановлен снаружи во время "
                "танца (stop-epoch %s -> %s) — restore пропущен",
                epoch_snapshot, get_epoch(),
            )
            return True
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
            return False
        return True

    @staticmethod
    def _on_wake_word_detected_after_reinit(model_name: str, score: float) -> None:
        """Доставка детекций агенту идёт через _record_detection() безусловно
        внутри цикла слушателя — этому callback'у достаточно лога."""
        logger.info(
            "AudioReinitCoordinator: wake word обнаружен после reinit "
            "(model=%r, score=%.3f)",
            model_name, score,
        )
