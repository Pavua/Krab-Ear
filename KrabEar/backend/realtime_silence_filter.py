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
        self._check_sec: float = float(settings.get("rt_silence_check_sec", _DEFAULT_CHECK_SEC))
        self._window_sec: float = float(settings.get("rt_silence_window_sec", _DEFAULT_WINDOW_SEC))
        self._max_silence_sec: float = float(settings.get("rt_silence_max_sec", _DEFAULT_MAX_SILENCE_SEC))
        self._threshold_db: float = float(
            settings.get("realtime_silence_threshold_db", _DEFAULT_THRESHOLD_DB)
        )
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

    def stop(self) -> list[tuple[float, float]]:
        """Останавливает фоновый поток, возвращает накопленные silence_ranges."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=max(self._check_sec + 1.0, 2.0))

        with self._lock:
            ranges = list(self._silence_ranges)

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

        window_start_sec = max(0.0, total_duration - self._window_sec)

        # Skip already-analyzed prefix: compute how far into the current window
        # we have already scanned and trim the audio array accordingly.
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

        # Update _checked_up_to_sec to the end of the current window so the
        # next tick skips all audio we just processed.
        with self._lock:
            self._checked_up_to_sec = total_duration

        if total_silence < self._max_silence_sec:
            return

        # Advance cursor only after confirming there is significant silence to
        # record — on the fast path (no silence) the cursor is NOT advanced so
        # the next tick can re-examine the same window with fresh audio appended.
        with self._lock:
            self._checked_up_to_sec = total_duration

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
