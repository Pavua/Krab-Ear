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

Ошибки не прерывают цикл — логируются на уровне debug (первые 5 — warning).
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


class RealtimePartialTranscriber:
    """Фоновый поток, эмитирующий частичные транскрибации через event_bus.

    Args:
        transcriber: экземпляр ``Transcriber`` (должен иметь ``transcribe_preview``).
        recorder: экземпляр ``AudioRecorder`` (должен иметь ``snapshot_audio``).
        event_bus: объект шины событий (должен иметь ``emit(type_str, dict)``).
        interval_sec: интервал между preview STT (секунды).
        buffer_sec: длина буфера для preview (секунды).
    """

    def __init__(
        self,
        transcriber: Any,
        recorder: Any,
        event_bus: Any,
        interval_sec: float = 3.0,
        buffer_sec: float = 8.0,
    ) -> None:
        self._transcriber = transcriber
        self._recorder = recorder
        self._event_bus = event_bus
        self._interval_sec = max(0.1, float(interval_sec))
        self._buffer_sec = max(1.0, float(buffer_sec))

        self._stop_event = threading.Event()
        self._stop_requested: bool = False
        self._thread: threading.Thread | None = None
        self._session_id: str = ""
        self._sample_rate: int = 16000

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """True если фоновый поток запущен и не остановлен."""
        return self._thread is not None and self._thread.is_alive()

    def start(self, session_id: str, sample_rate: int = 16000) -> None:
        """Запустить поток частичной транскрибации.

        Idempotent: если поток уже запущен — ничего не делает.

        Defensive guard: если transcriber не имеет метода transcribe_preview
        (например, _FakeTranscriber в тестах), thread не запускается. Иначе
        worker loop ловит AttributeError бесконечно и спамит логи (на CI это
        приводит к 10-min job timeout).
        """
        if self.is_running:
            return
        if not callable(getattr(self._transcriber, "transcribe_preview", None)):
            logger.info(
                "RealtimePartialTranscriber отключён: transcriber %s не имеет метода transcribe_preview",
                type(self._transcriber).__name__,
            )
            return
        self._session_id = session_id
        self._sample_rate = sample_rate
        self._stop_event.clear()
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._worker,
            name="RealtimePartialTranscriber",
            daemon=True,
        )
        self._thread.start()
        logger.debug(
            "RealtimePartialTranscriber запущен: session=%s interval=%.1fs buffer=%.1fs",
            session_id,
            self._interval_sec,
            self._buffer_sec,
        )

    def stop(self, timeout_sec: float = 30.0) -> None:
        """Остановить поток и дождаться его завершения.

        Idempotent: безопасно вызывать если поток не запущен.

        Таймаут увеличен до 30s чтобы покрыть случай когда mlx_lock удерживается
        финальным STT-вызовом (бюджет STT timeout = 30s). При истечении таймаута
        пишется предупреждение и self._thread обнуляется, чтобы следующий start()
        не отказал с «поток уже запущен».
        """
        self._stop_requested = True
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
            if self._thread.is_alive():
                logger.warning(
                    "realtime_partial worker did not stop within 30s"
                    " — daemon thread may emit stale partials"
                    " (session=%s)",
                    self._session_id,
                )
        self._thread = None
        logger.debug("RealtimePartialTranscriber остановлен: session=%s", self._session_id)

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

            try:
                audio, duration_sec = self._recorder.snapshot_audio(
                    max_duration_sec=self._buffer_sec
                )
            except Exception as exc:
                self._log_error("snapshot_audio упал", exc, error_count)
                error_count += 1
                continue

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
                continue

            text = (result.get("text") or "").strip() if isinstance(result, dict) else ""
            if not text:
                continue

            last_transcribed_duration = duration_sec
            error_count = 0  # сбрасываем счётчик при успехе

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

    def _log_error(self, message: str, exc: Exception, count: int) -> None:
        """Логирует ошибку на уровне DEBUG или WARNING в зависимости от частоты."""
        if count >= _ERROR_WARN_THRESHOLD:
            logger.warning("%s (session=%s): %s", message, self._session_id, exc)
        else:
            logger.debug("%s (session=%s): %s", message, self._session_id, exc)
