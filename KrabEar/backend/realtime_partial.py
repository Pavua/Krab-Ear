"""Realtime partial transcription — live text during active recording.

RealtimePartialTranscriber запускается при start_recording (если включён флаг
realtime_partial_enabled) и каждые ``interval_sec`` секунд:
  1. Снимает snapshot аудиобуфера (последние ``buffer_sec`` секунд).
  2. Запускает preview STT через ``Transcriber.transcribe_preview()``.
  3. Публикует событие ``realtime.partial_transcript`` в event_bus.

Финальное событие ``realtime.final_transcript`` публикуется из BackendService
после завершения полной транскрибации.

Поток:
    start(session_id) → worker loop → stop(timeout_sec)

Ошибки: первые 5 подряд — WARNING. После ``_MAX_CONSECUTIVE_ERRORS`` (10)
подряд без успеха — воркер завершает цикл и эмитирует событие
``realtime.partial_disabled`` через event_bus. Пользователь может перезапустить
partial transcription через IPC (stop + start recording).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("KrabEar.RealtimePartial")

_REALTIME_PARTIAL_TYPE = "realtime.partial_transcript"
_REALTIME_FINAL_TYPE = "realtime.final_transcript"

# После этого числа подряд идущих ошибок — лог переходит на WARNING.
_ERROR_WARN_THRESHOLD = 5

# После этого числа подряд идущих ошибок — воркер завершает цикл.
_MAX_CONSECUTIVE_ERRORS = 10

_REALTIME_PARTIAL_DISABLED_TYPE = "realtime.partial_disabled"


class RealtimePartialTranscriber:
    """Фоновый поток, эмитирующий частичные транскрибации через event_bus.

    Args:
        transcriber: экземпляр ``Transcriber`` (должен иметь ``transcribe_preview``).
        recorder: экземпляр ``AudioRecorder`` (должен иметь ``snapshot_audio``).
        event_bus: объект шины событий (должен иметь ``emit(type_str, dict)``).
        interval_sec: интервал между preview STT (секунды).
        buffer_sec: длина буфера для preview (секунды).
        privacy_getter: callable() → bool; если возвращает True — emit пропускается.
            Вызывается каждую итерацию цикла, что позволяет переключать privacy_mode
            во время записи с задержкой ≤ interval_sec.
    """

    def __init__(
        self,
        transcriber: Any,
        recorder: Any,
        event_bus: Any,
        interval_sec: float = 3.0,
        buffer_sec: float = 8.0,
        privacy_getter: Any = None,
    ) -> None:
        self._transcriber = transcriber
        self._recorder = recorder
        self._event_bus = event_bus
        self._interval_sec = max(0.1, float(interval_sec))
        self._buffer_sec = max(1.0, float(buffer_sec))
        # callable() → bool; None means privacy mode is not tracked at this layer
        self._privacy_getter = privacy_getter

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # C2a: пауза на время LLM/диар (Metal)
        self._stop_requested: bool = False
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()  # W1746: protect _thread access
        self._session_id: str = ""
        self._sample_rate: int = 16000

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True если фоновый поток запущен и не остановлен."""
        with self._thread_lock:
            t = self._thread
        return t is not None and t.is_alive()

    def start(self, session_id: str, sample_rate: int = 16000) -> None:
        """Запустить поток частичной транскрибации.

        Idempotent: если поток уже запущен — ничего не делает.

        Defensive guard: если transcriber не имеет метода transcribe_preview
        (например, _FakeTranscriber в тестах), thread не запускается. Иначе
        worker loop ловит AttributeError бесконечно и спамит логи (на CI это
        приводит к 10-min job timeout).
        """
        if not callable(getattr(self._transcriber, "transcribe_preview", None)):
            logger.info(
                "RealtimePartialTranscriber отключён: transcriber %s не имеет метода transcribe_preview",
                type(self._transcriber).__name__,
            )
            return
        with self._thread_lock:
            # Handle сохраняется после timeout stop(); пока такой поток жив,
            # общий Event нельзя очищать — иначе старый worker продолжит работу.
            if self._thread is not None and self._thread.is_alive():
                return
            self._session_id = session_id
            self._sample_rate = sample_rate
            self._stop_event.clear()
            self._stop_requested = False
            new_thread = threading.Thread(
                target=self._worker,
                name="RealtimePartialTranscriber",
                daemon=True,
            )
            self._thread = new_thread
            new_thread.start()
        logger.debug(
            "RealtimePartialTranscriber запущен: session=%s interval=%.1fs buffer=%.1fs",
            session_id,
            self._interval_sec,
            self._buffer_sec,
        )

    def stop(self, timeout_sec: float = 30.0) -> bool:
        """Остановить поток и дождаться его завершения.

        Idempotent: безопасно вызывать если поток не запущен.

        Таймаут по умолчанию покрывает STT-вызов. При его истечении метод
        возвращает False и сохраняет живой handle: следующий start() обязан
        отказаться, иначе очистка общего Event оживит старый worker.
        """
        with self._thread_lock:
            # Флаг и handle захватываются одной линейной операцией со start(),
            # чтобы параллельный запуск не успел очистить только что заданный Event.
            self._stop_requested = True
            self._stop_event.set()
            thread_to_join = self._thread
        if (
            thread_to_join is not None
            and thread_to_join.is_alive()
            and thread_to_join is not threading.current_thread()
        ):
            thread_to_join.join(timeout=max(0.0, float(timeout_sec)))

        if thread_to_join is not None and thread_to_join.is_alive():
            logger.warning(
                "realtime_partial worker не завершился за %.1f с"
                " — daemon может отправить устаревший partial"
                " (session=%s)",
                timeout_sec,
                self._session_id,
            )
            return False

        with self._thread_lock:
            if self._thread is thread_to_join:
                self._thread = None
        logger.debug("RealtimePartialTranscriber остановлен: session=%s", self._session_id)
        return True

    def pause(self) -> None:
        """Приостановить снапшоты/эмиты без остановки треда (C2a, Metal-констрейнт).

        Idempotent. Текущая итерация (если уже в STT) дорабатывает — вызывающий
        GPU-слот сериализован, короткое перекрытие исключено его очередью.
        """
        self._pause_event.set()
        logger.debug("RealtimePartialTranscriber: pause (session=%s)", self._session_id)

    def resume(self) -> None:
        """Снять паузу. Idempotent."""
        self._pause_event.clear()
        logger.debug("RealtimePartialTranscriber: resume (session=%s)", self._session_id)

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Основной цикл фонового потока."""
        error_count = 0
        last_transcribed_duration: float = 0.0

        while not self._stop_event.is_set():
            # Быстрая проверка флага остановки в начале каждой итерации.
            # Дублирует _stop_event но позволяет остановиться без ожидания wait().
            if self._stop_requested:
                break
            # Ждём интервал (прерывается при вызове stop())
            self._stop_event.wait(self._interval_sec)
            if self._stop_event.is_set():
                break

            if self._pause_event.is_set():
                continue  # пауза: пропускаем итерацию, тред жив

            try:
                audio, duration_sec = self._recorder.snapshot_audio(
                    max_duration_sec=self._buffer_sec
                )
            except Exception as exc:
                self._log_error("snapshot_audio упал", exc, error_count)
                error_count += 1
                if error_count >= _MAX_CONSECUTIVE_ERRORS:
                    break
                continue

            # snapshot может разблокироваться уже после stop(); в этом случае
            # нельзя начинать новый Metal/STT-вызов.
            if self._stop_event.is_set() or self._stop_requested:
                break

            # Пропускаем если нет новых данных (меньше 0.5 сек прогресса)
            if (duration_sec - last_transcribed_duration) < 0.5:
                continue

            # Проверяем что буфер не пустой
            size = getattr(audio, "size", None)
            if size is not None and size == 0:
                continue

            try:
                result = self._transcriber.transcribe_preview(
                    audio_data=audio,
                    quality_profile="balanced",
                )
            except Exception as exc:
                self._log_error("transcribe_preview упал", exc, error_count)
                error_count += 1
                if error_count >= _MAX_CONSECUTIVE_ERRORS:
                    break
                continue

            # stop() мог сработать, пока Metal/STT-вызов был заблокирован.
            # После разблокировки результат уже относится к закрытой сессии и
            # не должен попасть в event bus как устаревший partial.
            if self._stop_event.is_set() or self._stop_requested:
                break

            text = (result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                continue

            last_transcribed_duration = duration_sec
            error_count = 0  # сбрасываем счётчик при успехе

            # Privacy guard (re-read every iteration for mid-recording toggle support).
            # FAIL CLOSED: если privacy_getter бросает исключение — считаем privacy ON
            # и подавляем emit.  Частичный транскрипт НЕ должен утекать, когда
            # состояние приватности неизвестно.
            if self._privacy_getter is not None:
                try:
                    privacy_on = self._privacy_getter()
                except Exception as exc:
                    logger.warning(
                        "realtime privacy getter raised — failing safe (suppressing partial)",
                        extra={"error": type(exc).__name__, "session_id": self._session_id},
                    )
                    continue  # fail closed — emit подавлен
                if privacy_on:
                    logger.debug(
                        "RealtimePartialTranscriber: emit пропущен — privacy_mode активен (session=%s)",
                        self._session_id,
                    )
                    continue

            try:
                self._event_bus.emit(
                    _REALTIME_PARTIAL_TYPE,
                    {
                        "session_id": self._session_id,
                        "text": text,
                        "is_partial": True,
                        "ts": time.time(),
                    },
                )
            except Exception as exc:
                self._log_error("event_bus.emit упал", exc, error_count)
                error_count += 1
                if error_count >= _MAX_CONSECUTIVE_ERRORS:
                    break

        # Circuit breaker: если вышли из-за накопленных ошибок — сигнализируем.
        if error_count >= _MAX_CONSECUTIVE_ERRORS:
            logger.error(
                "RealtimePartialTranscriber отключён после %d последовательных ошибок "
                "(session=%s). Перезапустите запись для восстановления.",
                error_count,
                self._session_id,
                extra={"consecutive_errors": error_count, "session_id": self._session_id},
            )
            try:
                self._event_bus.emit(
                    _REALTIME_PARTIAL_DISABLED_TYPE,
                    {
                        "session_id": self._session_id,
                        "reason": "consecutive_errors",
                        "error_count": error_count,
                        "ts": time.time(),
                    },
                )
            except Exception:
                pass  # лог уже выведен выше; silently ignore emit failure

    def _log_error(self, message: str, exc: Exception, count: int) -> None:
        """Логирует ошибку на уровне DEBUG или WARNING в зависимости от частоты."""
        if count >= _ERROR_WARN_THRESHOLD:
            logger.warning("%s (session=%s): %s", message, self._session_id, exc)
        else:
            logger.debug("%s (session=%s): %s", message, self._session_id, exc)
