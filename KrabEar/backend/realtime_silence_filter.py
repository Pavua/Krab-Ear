"""Realtime silence filter для Krab Ear backend.

RealtimeSilenceFilter запускает фоновый поток во время записи, который
периодически анализирует аудиобуфер и помечает диапазоны тишины.
При финальной транскрибации помеченные диапазоны пропускаются, что
ускоряет STT и снижает количество галлюцинаций на тихих участках.

Фильтр НЕ изменяет буфер рекордера — только отслеживает silence_ranges
в виде (start_sec, end_sec) и публикует события через event_bus.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import TYPE_CHECKING

import numpy as np

from core.silence_constants import SILENCE_THRESHOLD_DB_PRESERVE_WHISPER
from core.silence_detector import SilenceDetector

if TYPE_CHECKING:
    from backend.recorder import AudioRecorder

logger = logging.getLogger("KrabEar.Backend.RealtimeSilenceFilter")

_DEFAULT_CHECK_SEC: float = 5.0
_DEFAULT_WINDOW_SEC: float = 10.0
_DEFAULT_MAX_SILENCE_SEC: float = 8.0
# W1018: STT-пути используют PRESERVE_WHISPER (-55 дБ) — сохраняет тихую речь и шёпот.
_DEFAULT_THRESHOLD_DB: float = SILENCE_THRESHOLD_DB_PRESERVE_WHISPER  # -55 dBFS


class RealtimeSilenceFilter:
    """Детектор тишины в реальном времени для длинных записей."""

    def __init__(
        self,
        recorder: AudioRecorder,
        settings: dict,
        event_bus_emit=None,
    ) -> None:
        self._recorder = recorder
        self._enabled: bool = bool(settings.get("realtime_silence_filter_enabled", False))

        # B1 (MED): clamp check_sec — raw<=0 makes wait() return immediately → busy loop.
        _raw_check = float(settings.get("rt_silence_check_sec", _DEFAULT_CHECK_SEC))
        if not math.isfinite(_raw_check):
            _raw_check = _DEFAULT_CHECK_SEC
        self._check_sec: float = max(0.5, _raw_check)

        # B2 (MED): clamp window_sec — raw<=0 copies full buffer every tick (O(n) waste).
        _raw_window = float(settings.get("rt_silence_window_sec", _DEFAULT_WINDOW_SEC))
        if not math.isfinite(_raw_window):
            _raw_window = _DEFAULT_WINDOW_SEC
        self._window_sec: float = max(1.0, _raw_window)

        # wave-1770 MED: guard NaN/Inf on rt_silence_max_sec (same pattern as _window_sec above).
        # Without this guard, float('inf') permanently suppresses silence detection;
        # float('nan') makes all comparisons unpredictable.
        _raw_max = float(settings.get("rt_silence_max_sec", _DEFAULT_MAX_SILENCE_SEC))
        if not math.isfinite(_raw_max):
            _raw_max = _DEFAULT_MAX_SILENCE_SEC
        self._max_silence_sec: float = max(0.5, min(60.0, _raw_max))

        # B3 (LOW): clamp threshold_db — huge positive value classifies all speech as silent.
        _raw_threshold = float(
            settings.get("realtime_silence_threshold_db", _DEFAULT_THRESHOLD_DB)
        )
        if not math.isfinite(_raw_threshold):
            _raw_threshold = _DEFAULT_THRESHOLD_DB
        self._threshold_db: float = max(-80.0, min(-10.0, _raw_threshold))
        self._emit = event_bus_emit

        self._detector = SilenceDetector()

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._silence_ranges: list[tuple[float, float]] = []
        self._checked_up_to_sec: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_running(self) -> bool:
        """Показывает, остался ли жив фоновый worker."""
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Запускает фоновый поток проверки тишины (идемпотентен)."""
        if not self._enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._silence_ranges = []
            self._checked_up_to_sec = 0.0
            self._thread = threading.Thread(
                target=self._worker,
                name="RealtimeSilenceFilter",
                daemon=True,
            )
            self._thread.start()
            logger.debug(
                "RealtimeSilenceFilter запущен (check=%.1fs window=%.1fs max_silence=%.1fs)",
                self._check_sec,
                self._window_sec,
                self._max_silence_sec,
            )

    def stop(self, timeout_sec: float | None = None) -> list[tuple[float, float]]:
        """Останавливает worker и возвращает накопленные silence_ranges.

        При таймауте живой handle сохраняется. Это не даёт последующему
        ``start()`` очистить общий Event и оживить прежний поток.
        """
        with self._lock:
            # set + capture линейны со start(): новый запуск не очистит Event
            # между запросом остановки и захватом текущего handle.
            self._stop_event.set()
            thread = self._thread
        timeout = (
            max(self._check_sec + 1.0, 2.0)
            if timeout_sec is None
            else max(0.0, float(timeout_sec))
        )
        if (
            thread is not None
            and thread.is_alive()
            and thread is not threading.current_thread()
        ):
            thread.join(timeout=timeout)

        with self._lock:
            if self._thread is thread and (
                thread is None or not thread.is_alive()
            ):
                self._thread = None
            ranges = list(self._silence_ranges)

        if thread is not None and thread.is_alive():
            logger.warning(
                "RealtimeSilenceFilter worker не завершился за %.1f с",
                timeout,
            )
        logger.debug("RealtimeSilenceFilter остановлен, %d диапазонов тишины", len(ranges))
        return ranges

    def get_silence_ranges(self) -> list[tuple[float, float]]:
        """Возвращает текущие накопленные silence_ranges без остановки потока."""
        with self._lock:
            return list(self._silence_ranges)

    def _worker(self) -> None:
        while not self._stop_event.wait(timeout=self._check_sec):
            try:
                self._check_once()
            except Exception:
                logger.exception("RealtimeSilenceFilter: ошибка при проверке тишины")

    def _check_once(self) -> None:
        if not self._recorder.is_recording:
            return

        sample_rate = getattr(self._recorder, "sample_rate", 16000)
        audio_window, total_duration = self._recorder.snapshot_audio(
            max_duration_sec=self._window_sec
        )

        if audio_window.size == 0 or total_duration < 1.0:
            return

        # W1769 FIX (correctness): диапазоны тишины ДОЛЖНЫ быть в "семпл-времени"
        # относительно итогового аудиобуфера, т.к. их потом потребляет
        # ``zero_silence_ranges`` через ``int(sec * sample_rate)`` — обнуление по
        # ИНДЕКСУ СЕМПЛОВ. Прежний код якорил окно к ``total_duration`` (wall-clock:
        # ``time.monotonic() - started_at``), а внутри окна ``detect_silence`` даёт
        # смещения в семпл-времени. При расхождении настенных часов и реально
        # буферизованного числа семплов (стол/задержка/потеря аудио-кадров) якорь
        # уезжал вперёд → обнуление попадало на РЕАЛЬНУЮ речь (или мимо тишины).
        #
        # Корректный якорь — хвост буфера в семплах: окно ``audio_window`` это
        # последние ``audio_window.size`` семплов записи, поэтому
        # ``window_start_sample = total_recorded_samples - audio_window.size``.
        # total_recorded_samples берём из O(1)-счётчика рекордера (семпл-время —
        # ИСТИНА). Wall-clock — только фоллбэк, когда счётчик недоступен (стаб без
        # ``_chunks_total_samples``): при дрейфе wall-clock ЗАВЫШАЕТ число семплов,
        # поэтому участвовать в ``max()`` он НЕ должен.
        # ES: anclamos las franjas en tiempo-de-muestra (contador real), no en reloj.
        total_samples = int(getattr(self._recorder, "_chunks_total_samples", 0) or 0)
        if total_samples <= 0:
            total_samples = int(total_duration * sample_rate)
        # Окно — подмножество буфера, поэтому суммарных семплов не может быть
        # меньше размера окна (защита от рассинхрона счётчика и снимка).
        total_samples = max(total_samples, audio_window.size)

        window_start_sample = max(0, total_samples - audio_window.size)
        window_start_sec = window_start_sample / sample_rate

        # Skip already-analyzed prefix: compute how far into the current window
        # we have already scanned and trim the audio array accordingly.
        # Курсор ``_checked_up_to_sec`` тоже в семпл-времени (см. advance ниже),
        # поэтому вся арифметика префикса остаётся в одних единицах (семплы).
        with self._lock:
            checked_up_to = self._checked_up_to_sec

        already_analyzed_in_window = max(0.0, checked_up_to - window_start_sec)
        skip_samples = int(already_analyzed_in_window * sample_rate)

        if skip_samples >= audio_window.size:
            # Nothing new to analyze yet.
            return

        analysis_audio = audio_window[skip_samples:]
        analysis_start_sec = window_start_sec + already_analyzed_in_window

        silence_regions = self._detector.detect_silence(
            analysis_audio, sample_rate, threshold_db=self._threshold_db
        )

        total_silence = sum(r.duration_sec for r in silence_regions)

        if total_silence < self._max_silence_sec:
            return

        # Advance cursor only after confirming significant silence (W1330 fix).
        # W1769: курсор продвигаем в СЕМПЛ-ВРЕМЕНИ (конец буфера), а не по wall-clock,
        # чтобы префикс-скип на следующем тике совпадал с якорем окна (семплы).
        analyzed_up_to_sec = window_start_sec + audio_window.size / sample_rate
        with self._lock:
            self._checked_up_to_sec = analyzed_up_to_sec

        new_ranges: list[tuple[float, float]] = []
        for region in silence_regions:
            if region.duration_sec < self._max_silence_sec:
                continue
            abs_start = analysis_start_sec + region.start_sec
            abs_end = analysis_start_sec + region.end_sec
            new_ranges.append((round(abs_start, 3), round(abs_end, 3)))

        if not new_ranges:
            return

        with self._lock:
            merged = _merge_ranges(self._silence_ranges + new_ranges)
            self._silence_ranges = merged

        logger.debug(
            "RealtimeSilenceFilter: total_silence=%.1fs, добавлено %d диапазонов",
            total_silence,
            len(new_ranges),
        )

        if self._emit is not None:
            try:
                self._emit(
                    "recording.silence_detected",
                    {
                        "total_silence_sec": round(total_silence, 2),
                        "ranges_count": len(new_ranges),
                        "recording_duration_sec": round(total_duration, 2),
                    },
                )
            except Exception:
                pass


def zero_silence_ranges(
    audio: np.ndarray,
    silence_ranges: list[tuple[float, float]],
    sample_rate: int = 16000,
) -> np.ndarray:
    """Обнуляет семплы в указанных диапазонах тишины (возвращает копию).

    Args:
        audio: numpy-массив float32 (1D).
        silence_ranges: список (start_sec, end_sec) для обнуления.
        sample_rate: частота дискретизации, Гц.

    Returns:
        Копия аудио с обнулёнными диапазонами тишины.
    """
    if not silence_ranges or audio.size == 0:
        return audio
    result = audio.copy()
    n_samples = result.shape[0]
    for start_sec, end_sec in silence_ranges:
        s = max(0, int(start_sec * sample_rate))
        e = min(n_samples, int(end_sec * sample_rate))
        if e > s:
            result[s:e] = 0
    return result


def _merge_ranges(
    ranges: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Сортирует и объединяет пересекающиеся/смежные диапазоны."""
    if not ranges:
        return []
    sorted_ranges = sorted(ranges, key=lambda r: r[0])
    merged: list[tuple[float, float]] = [sorted_ranges[0]]
    for s, e in sorted_ranges[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    return merged
