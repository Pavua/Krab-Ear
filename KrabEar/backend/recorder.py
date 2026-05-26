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


class AudioRecorder:
    """Потокобезопасный рекордер c режимом start/stop."""

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        on_audio_level: Callable[[float], None] | None = None,
        device: "int | str | None" = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = int(self.sample_rate * 0.1)
        self._device: "int | str | None" = device

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._chunks: list[np.ndarray] = []
        self._is_recording = False
        self._started_at: float = 0.0
        self._on_audio_level = on_audio_level

    @property
    def is_recording(self) -> bool:
        """Флаг активной записи."""
        with self._lock:
            return self._is_recording

    def start(self) -> bool:
        """Запускает запись, если рекордер сейчас в idle состоянии."""
        with self._lock:
            if self._is_recording:
                return False
            self._chunks = []
            self._stop_event.clear()
            self._is_recording = True
            self._started_at = time.monotonic()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout_sec: float = 3.0, trim_tail_ms: int = 0) -> tuple[np.ndarray, float] | None:
        """Останавливает запись и возвращает (audio, duration)."""
        with self._lock:
            if not self._is_recording:
                return None
            self._is_recording = False
            thread = self._thread

        self._stop_event.set()
        if thread is not None:
            thread.join(timeout=timeout_sec)

        with self._lock:
            duration = max(0.0, time.monotonic() - self._started_at)
            chunks = list(self._chunks)
            self._chunks = []
            self._thread = None

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
        """Возвращает срез последнего аудио для realtime preview без остановки записи."""
        with self._lock:
            duration = max(0.0, time.monotonic() - self._started_at) if self._is_recording else 0.0
            chunks = list(self._chunks)

        if not chunks:
            return np.array([], dtype=np.float32), duration

        audio = np.concatenate(chunks, axis=0).reshape(-1).astype(np.float32)
        if max_duration_sec > 0:
            max_samples = int(self.sample_rate * max_duration_sec)
            if max_samples > 0 and audio.size > max_samples:
                audio = audio[-max_samples:]
        return audio, duration

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
                    if overflowed:
                        logger.warning("Переполнение аудиобуфера во время записи")
                        self._push_buffer_overflow_error()
                    with self._lock:
                        self._chunks.append(data.copy())
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
