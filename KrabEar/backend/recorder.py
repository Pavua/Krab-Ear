"""Локальная запись аудио для Krab Ear backend.

Сервис хранит данные чанками в памяти до команды stop, чтобы затем передать
единый numpy-массив в транскрибатор.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np

# sounddevice требует PortAudio native lib (libportaudio2 на Linux).
# На Ubuntu CI отсутствует → оборачиваем для test discovery.
try:
    import sounddevice as sd  # type: ignore
except Exception:
    sd = None  # type: ignore[assignment]

logger = logging.getLogger("KrabEar.Backend.Recorder")

_AUDIO_LEVEL_EMIT_INTERVAL_SEC = 0.033  # ~30 Hz для VU meter

# Максимальное количество семплов для защиты от утечки памяти.
# 4 часа @ 16 kHz = 230 400 000 семплов ≈ 880 МБ float32.
# При превышении запись автоматически останавливается с ошибкой audio.max_duration_reached.
MAX_RECORDING_SAMPLES = 16000 * 60 * 60 * 4  # 4 hours @ 16 kHz


class AudioRecorderStopTimeout(RuntimeError):
    """stop() не дождался выхода worker-потока за отведённый таймаут.

    Отличим от «нечего отдавать» (``None``): вызыватель обязан донести до
    пользователя громкий ``recorder_timeout``, а не тихий ``already_stopped``
    (F2, Fable-ревью 2026-07-22 — тихая полная потеря диктовки).
    """


class AudioRecorder:
    """Потокобезопасный рекордер c режимом start/stop."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        on_audio_level: Callable[[float], None] | None = None,
        device: "int | str | None" = None,
        max_recording_samples: int | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = int(self.sample_rate * 0.1)
        # Максимум семплов до авто-остановки. None → модульная константа MAX_RECORDING_SAMPLES.
        # Параметр нужен для тестов (tiny cap без выделения ~880 МБ).
        self._max_recording_samples: int = (
            max_recording_samples if max_recording_samples is not None else MAX_RECORDING_SAMPLES
        )
        self._device: "int | str | None" = device

        self._lock = threading.Lock()
        # wave-1770 MED (race): _lock is the DATA lock (protects _chunks/_is_recording)
        # and is released before stop()'s join() — the worker needs it to append chunks,
        # so holding it across join() would deadlock. That release window let a concurrent
        # start() slip in: it would see _is_recording=False, spawn a new worker, then the
        # still-running stop() would set _stop_event (killing the new recording) and null
        # the new _thread handle. _lifecycle_lock serialises start()/stop() AS WHOLE
        # operations; the worker never touches it, so holding it across join() is safe.
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._chunks: list[np.ndarray] = []
        self._chunks_total_samples: int = 0  # O(1) счётчик для get_duration_sec без concatenate
        self._is_recording = False
        self._started_at: float = 0.0
        self._on_audio_level = on_audio_level
        # W1670: буфер для аудио, собранного при авто-остановке по max-duration.
        # Устанавливается воркером при MAX_RECORDING_SAMPLES, очищается в start().
        self._pending_result: tuple[np.ndarray, float] | None = None
        # R1: continuous spill writer (duck-typed .append/.close/.failed), см.
        # backend/recording_spill.py. None = spill выключен для этой записи.
        self._spill = None

    @property
    def is_recording(self) -> bool:
        """Флаг активной записи."""
        with self._lock:
            return self._is_recording

    def start(self, spill: "object | None" = None) -> bool:
        """Запускает запись, если рекордер сейчас в idle состоянии.

        ``spill`` — опциональный открытый :class:`RecordingSpillWriter`
        (duck-typed .append/.close/.failed, R1). Recorder только дописывает
        и закрывает его; удаление (``discard()``) принадлежит вызывающей
        стороне (RecordingCoreService).
        """
        # wave-1770 MED: serialise the whole start against a concurrent stop() so the
        # data-lock release window inside stop() cannot interleave with a new start.
        with self._lifecycle_lock:
            with self._lock:
                # После таймаута stop()/abort() старый CFFI-worker может быть
                # жив, хотя публичный флаг уже False. Его handle намеренно
                # сохраняется: очистка общего Event оживила бы старый цикл и
                # запустила второй поток поверх него.
                if self._is_recording or (
                    self._thread is not None and self._thread.is_alive()
                ):
                    return False
                self._chunks = []
                self._chunks_total_samples = 0
                self._stop_event.clear()
                self._is_recording = True
                self._started_at = time.monotonic()
                self._pending_result = None  # W1670: сброс результата предыдущей авто-остановки
                self._spill = spill
                self._thread = threading.Thread(target=self._worker, daemon=True)
                self._thread.start()
                return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0) -> tuple[np.ndarray, float] | None:
        """Останавливает запись и возвращает (audio, duration).

        W1670: если запись уже завершена авто-остановкой по MAX_RECORDING_SAMPLES,
        возвращает накопленный аудио-буфер из _pending_result вместо None.
        Это предотвращает потерю до ~880 МБ аудио при превышении лимита длительности.

        R1: во всех ветках выхода, КРОМЕ таймаута (воркер ещё может дописывать),
        принадлежащий этой записи spill-writer закрывается (файлы остаются на
        диске — удаление принадлежит вызывающей стороне). close() зовётся
        строго вне ``self._lock`` (I/O под данным-локом — запрещённый класс
        W1652/F3).
        """
        # wave-1770 MED: hold _lifecycle_lock across the ENTIRE stop (incl. join) so a
        # concurrent start() cannot spawn a new worker during the data-lock release window.
        # The worker uses _lock (data), never _lifecycle_lock → join() here can't deadlock.
        with self._lifecycle_lock:
            spill_local = None
            early_return = False
            early_result: tuple[np.ndarray, float] | None = None
            with self._lock:
                thread = self._thread
                if not self._is_recording and (
                    thread is None or not thread.is_alive()
                ):
                    # W1670: авто-остановка уже сработала — вернуть сохранённый результат
                    pending = self._pending_result
                    if pending is not None:
                        self._pending_result = None
                        if self._thread is thread:
                            self._thread = None
                        spill_local = self._spill
                        self._spill = None
                        early_result = pending
                        early_return = True
                    # Живой handle после предыдущего таймаута обрабатывается
                    # ниже повторным join. Мёртвый handle с чанками означает,
                    # что worker завершился уже после того таймаута: аудио ещё
                    # нужно безопасно забрать, а не потерять.
                    elif thread is None or not self._chunks:
                        if self._thread is thread:
                            self._thread = None
                        spill_local = self._spill
                        self._spill = None
                        early_return = True
                    else:
                        self._is_recording = False
                else:
                    self._is_recording = False

            if not early_return:
                self._stop_event.set()
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=max(0.0, float(timeout_sec)))

                # Нельзя отдавать частичный буфер и терять handle, пока worker ещё
                # способен дописать чанк. Повторный stop() завершит сбор после его
                # фактического выхода. Исход обязан быть РАЗЛИЧИМЫМ от «нечего
                # отдавать»: None здесь превращался в тихий already_stopped и
                # пользователь молча терял диктовку (F2, Fable-ревью 2026-07-22).
                if thread is not None and thread.is_alive():
                    logger.warning(
                        "AudioRecorder worker не завершился за %.1f с при stop()",
                        timeout_sec,
                    )
                    # R1: воркер завис и может ещё дописывать — spill НЕ трогаем.
                    raise AudioRecorderStopTimeout(
                        f"AudioRecorder worker не завершился за {timeout_sec:.1f} с"
                    )

                with self._lock:
                    spill_local = self._spill
                    self._spill = None
                    pending = self._pending_result
                    if pending is not None:
                        self._pending_result = None
                        if self._thread is thread:
                            self._thread = None
                        self._started_at = 0.0
                        early_result = pending
                        early_return = True
                    else:
                        duration = max(0.0, time.monotonic() - self._started_at)
                        chunks = list(self._chunks)
                        self._chunks = []
                        self._chunks_total_samples = 0
                        if self._thread is thread:
                            self._thread = None
                        self._started_at = 0.0

        # R1 spill: строго ВНЕ self._lock (I/O под локом — запрещённый класс
        # W1652/F3). close() оставляет файлы на диске — discard() решает
        # вызывающая сторона (RecordingCoreService) после успешного персиста.
        if spill_local is not None:
            spill_local.close()
        if early_return:
            return early_result

        if not chunks:
            return np.array([], dtype=np.float32), duration

        audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)

        # Мягко отрезаем хвост записи, чтобы уменьшить риск захвата фонового аудио
        # в момент отпускания hotkey и переключения фокуса.
        trim_ms = max(0, int(trim_tail_ms))
        if trim_ms > 0:
            trim_samples = int((self.sample_rate * trim_ms) / 1000)
            if trim_samples > 0:
                if audio.size > trim_samples:
                    audio = audio[:-trim_samples]
                else:
                    audio = np.array([], dtype=np.float32)
        return audio, duration

    def abort(self, timeout_sec: float = 3.0) -> bool:
        """Остановить захват и отбросить аудио без сборки финального массива.

        Метод предназначен для shutdown процесса, когда транскрибация уже не
        начнётся. В отличие от :meth:`stop`, он не вызывает ``np.concatenate``
        и поэтому не удваивает пиковую память длинной записи. Идемпотентен и
        сериализован с ``start()``/``stop()`` тем же lifecycle-lock.

        R1: spill-writer закрывается (НЕ discard — файлы остаются на диске,
        это shutdown-путь и следующий старт восстановит аудио).
        """
        with self._lifecycle_lock:
            with self._lock:
                self._is_recording = False
                thread = self._thread

            self._stop_event.set()
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, float(timeout_sec)))

            if thread is not None and thread.is_alive():
                logger.warning(
                    "AudioRecorder worker не завершился за %.1f с при abort()",
                    timeout_sec,
                )
                # Handle и буфер нужны для безопасного повторного abort():
                # worker всё ещё может дописать данные после этого возврата.
                # R1: spill тоже не трогаем — воркер может ещё дописывать.
                return False

            # Очищаем только после join: worker при авто-лимите может временно
            # держать локальную копию чанков и записать _pending_result перед выходом.
            with self._lock:
                self._chunks = []
                self._chunks_total_samples = 0
                self._pending_result = None
                spill_local = self._spill
                self._spill = None
                if self._thread is thread:
                    self._thread = None
                self._started_at = 0.0
            # R1 spill: close() строго вне self._lock (I/O под локом запрещено).
            if spill_local is not None:
                spill_local.close()
            return True

    def set_device(self, device: "int | str | None") -> None:
        """Устанавливает аудиоустройство для следующей записи.

        Валидирует существование устройства при ненулевом значении.
        Изменение вступает в силу при следующем вызове start() — текущая
        запись не прерывается.

        Raises:
            ValueError: если устройство не найдено в списке sounddevice.
        """
        if device is not None and sd is not None:
            try:
                sd.query_devices(device, "input")
            except Exception as exc:
                raise ValueError(
                    f"AudioRecorder: устройство {device!r} не найдено: {exc}"
                ) from exc
        with self._lock:
            self._device = device

    def get_duration_sec(self) -> float:
        """Возвращает длительность текущей записи в секундах."""
        with self._lock:
            if not self._is_recording:
                return 0.0
            return max(0.0, time.monotonic() - self._started_at)

    def snapshot_audio(self, max_duration_sec: float = 12.0) -> tuple[np.ndarray, float]:
        """Возвращает срез последнего аудио для realtime preview без остановки записи.

        Оптимизация (W1327 F1): обходит чанки с конца и накапливает только нужный
        хвост — не конкатенирует весь буфер (который может быть >200 МБ при 1-часовой
        записи). Сложность O(n) по числу чанков в окне, а не O(N) по всем семплам.
        """
        with self._lock:
            duration = max(0.0, time.monotonic() - self._started_at) if self._is_recording else 0.0
            chunks = list(self._chunks)

        if not chunks:
            return np.array([], dtype=np.float32), duration

        if max_duration_sec <= 0:
            # Весь буфер запрошен — конкатенируем полностью
            audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
            return audio, duration

        max_samples = int(self.sample_rate * max_duration_sec)
        if max_samples <= 0:
            return np.array([], dtype=np.float32), duration

        # Обходим чанки с конца, накапливая семплы пока не наберём нужное количество.
        # Это избегает конкатенации 200+ МБ буфера только чтобы взять последние 12 с.
        tail_chunks: list[np.ndarray] = []
        collected = 0
        for chunk in reversed(chunks):
            flat = chunk.reshape(-1)
            tail_chunks.append(flat)
            collected += flat.size
            if collected >= max_samples:
                break

        tail_chunks.reverse()
        audio = np.concatenate(tail_chunks, axis=0).astype(np.float32)
        if audio.size > max_samples:
            audio = audio[-max_samples:]
        return audio, duration

    def snapshot_range(self, from_sec: float, to_sec: float) -> np.ndarray:
        """Срез сырого буфера по диапазону секунд ОТ НАЧАЛА записи.

        Для meeting-аккумулятора (C2a): непересекающиеся чанки по курсору —
        полный транскрипт без дедупа. O(число чанков) на скан + O(диапазона)
        на копирование; полной конкатенации буфера нет (урок snapshot_audio).
        Диапазон за пределами буфера обрезается; вырожденный → пустой массив.
        """
        if to_sec <= from_sec:
            return np.array([], dtype=np.float32)
        from_sample = max(0, int(from_sec * self.sample_rate))
        to_sample = int(to_sec * self.sample_rate)

        with self._lock:
            chunks = list(self._chunks)

        collected: list[np.ndarray] = []
        offset = 0
        for chunk in chunks:
            flat = chunk.reshape(-1)
            chunk_end = offset + flat.size
            if chunk_end <= from_sample:
                offset = chunk_end
                continue
            if offset >= to_sample:
                break
            start = max(0, from_sample - offset)
            end = min(flat.size, to_sample - offset)
            collected.append(flat[start:end])
            offset = chunk_end

        if not collected:
            return np.array([], dtype=np.float32)
        return np.concatenate(collected, axis=0).astype(np.float32)

    def snapshot_rms(self) -> float:
        """Возвращает RMS последнего записанного чанка (0.0-1.0) без остановки записи."""
        with self._lock:
            if not self._is_recording or not self._chunks:
                return 0.0
            last_chunk = self._chunks[-1]
        flat = last_chunk.reshape(-1).astype(np.float32)
        if flat.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(flat ** 2)))
        return max(0.0, min(1.0, rms))

    def _worker(self) -> None:
        """Фоновый цикл чтения чанков из микрофона."""
        with self._lock:
            device = self._device
            spill = self._spill
        try:
            stream_kwargs: dict = {
                "samplerate": self.sample_rate,
                "channels": self.channels,
                "dtype": "float32",
                "blocksize": self.chunk_size,
            }
            if device is not None:
                stream_kwargs["device"] = device
            with sd.InputStream(**stream_kwargs) as stream:
                last_level_emit_at = 0.0
                while not self._stop_event.is_set():
                    data, overflowed = stream.read(self.chunk_size)
                    # PortAudio read может разблокироваться уже после abort().
                    # Такой последний чанк нельзя добавлять или собирать в
                    # pending_result: shutdown требует только отбросить аудио.
                    if self._stop_event.is_set():
                        break
                    if overflowed:
                        logger.warning("Переполнение аудиобуфера во время записи")
                        self._push_buffer_overflow_error()
                    _max_duration_exceeded = False
                    with self._lock:
                        chunk_samples = data.reshape(-1).size
                        if self._chunks_total_samples + chunk_samples > self._max_recording_samples:
                            self._is_recording = False
                            logger.warning(
                                "Достигнут лимит длительности записи (%d сек)",
                                self._max_recording_samples // self.sample_rate,
                                extra={"max_samples": self._max_recording_samples},
                            )
                            _max_duration_exceeded = True
                            # W1670: собрать накопленные чанки в финальный массив и
                            # сохранить в _pending_result, пока держим лок.
                            # Это гарантирует, что stop() вернёт аудио вместо None,
                            # и немедленно освобождает ~880 МБ из _chunks.
                            duration = max(0.0, time.monotonic() - self._started_at)
                            chunks_copy = self._chunks
                            self._chunks = []
                            self._chunks_total_samples = 0
                        else:
                            self._chunks.append(data.copy())
                            self._chunks_total_samples += chunk_samples
                    # R1 spill: строго ВНЕ self._lock (I/O под локом — запретный
                    # класс W1652/F3). Ошибки диска гасятся внутри append().
                    if spill is not None and not _max_duration_exceeded:
                        spill.append(data)
                    # Push error OUTSIDE the lock to avoid lock-order deadlock:
                    # error_bus._lock → event_bus.emit() → SSE callbacks that may
                    # call recorder.is_recording / snapshot_rms (which re-acquire
                    # self._lock). threading.Lock is not re-entrant.
                    if _max_duration_exceeded:
                        # Assemble audio outside the lock (CPU work, no lock needed).
                        if chunks_copy:
                            audio = np.concatenate(chunks_copy, axis=0).reshape(-1).astype(np.float32)
                        else:
                            audio = np.array([], dtype=np.float32)
                        with self._lock:
                            self._pending_result = (audio, duration)
                        # R1: авто-лимит останавливает запись здесь же — закрыть
                        # spill сразу (stop()/abort() уже не увидят этот _spill,
                        # т.к. worker завершится раньше следующего stop() под
                        # тем же lifecycle_lock; файлы остаются для восстановления).
                        if spill is not None:
                            spill.close()
                        self._push_max_duration_error()
                        break
                    if self._on_audio_level is not None:
                        now = time.monotonic()
                        if now - last_level_emit_at >= _AUDIO_LEVEL_EMIT_INTERVAL_SEC:
                            last_level_emit_at = now
                            flat = data.reshape(-1).astype(np.float32)
                            rms = float(np.sqrt(np.mean(flat ** 2))) if flat.size > 0 else 0.0
                            rms_clamped = max(0.0, min(1.0, rms))
                            try:
                                self._on_audio_level(rms_clamped)
                            except Exception:
                                logger.debug("Ошибка в on_audio_level callback", exc_info=True)
        except Exception:
            logger.exception("Ошибка в потоке аудиозаписи")
        finally:
            with self._lock:
                self._is_recording = False

    def _push_max_duration_error(self) -> None:
        """Push audio.max_duration_reached to error bus. Never raises.

        W1652 (F3 fix): вызывается из _worker ПОСЛЕ освобождения self._lock во избежание
        deadlock: error_bus._lock → event_bus.emit() → SSE callback → recorder.is_recording.
        """
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from datetime import datetime, timezone
            max_hours = self._max_recording_samples // (self.sample_rate * 3600)
            err = KrabError(
                severity="warn",
                component="audio",
                code="audio.max_duration_reached",
                message_user=f"Запись превысила максимальную длительность ({max_hours} ч) и была автоматически остановлена",
                message_debug=f"MAX_RECORDING_SAMPLES={self._max_recording_samples} exceeded",
                timestamp=datetime.now(timezone.utc),
                context={"max_samples": self._max_recording_samples, "max_hours": max_hours},
                actionable=False,
                action_id=None,
            )
            error_bus.push(err)
        except Exception:
            logger.exception("AudioRecorder: error_bus.push (max_duration_reached) failed")

    def _push_buffer_overflow_error(self) -> None:
        """Push audio.buffer_overflow to error bus. Never raises.

        Wave 60: late-injected _error_bus attribute (set by BackendService).
        The error_codes registry has dedupe_seconds=5 so spam is suppressed.
        """
        error_bus = getattr(self, "_error_bus", None)
        if error_bus is None:
            return
        try:
            from backend.error_bus import KrabError
            from backend.error_codes import ERROR_REGISTRY
            from datetime import datetime, timezone
            entry = ERROR_REGISTRY.get("audio.buffer_overflow", {})
            err = KrabError(
                severity=entry.get("severity", "warn"),
                component="audio",
                code="audio.buffer_overflow",
                message_user=entry.get("user_msg_ru", "Аудиобуфер переполнен"),
                message_debug="sounddevice stream.read overflowed=True",
                timestamp=datetime.now(timezone.utc),
                context={},
                actionable=entry.get("actionable", False),
                action_id=entry.get("action_id"),
            )
            error_bus.push(err)
        except Exception:
            logger.exception("AudioRecorder: error_bus.push failed")
