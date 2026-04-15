"""Детектор речевой активности (VAD) для Krab Ear.

VoiceActivityDetector определяет участки речи и тишины в аудио
на основе энергии сигнала (RMS) с адаптивным порогом и гистерезисом.
Не требует внешних зависимостей.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("KrabEar.VAD")

# Минимальный RMS во избежание log(0)
_RMS_FLOOR = 1e-9


def _rms_to_db(rms: float) -> float:
    """Переводит RMS в дБ."""
    return 20.0 * np.log10(max(rms, _RMS_FLOOR))


@dataclass
class SpeechSegment:
    """Участок речи в аудиозаписи."""

    start_sec: float
    end_sec: float
    duration_sec: float
    energy_db: float

    def to_dict(self) -> dict:
        return {
            "start_sec": round(self.start_sec, 4),
            "end_sec": round(self.end_sec, 4),
            "duration_sec": round(self.duration_sec, 4),
            "energy_db": round(self.energy_db, 2),
        }


@dataclass
class VADResult:
    """Результат обнаружения речевой активности."""

    speech_segments: list[SpeechSegment] = field(default_factory=list)
    speech_ratio: float = 0.0          # доля речи [0, 1]
    total_speech_sec: float = 0.0
    total_silence_sec: float = 0.0

    def to_dict(self) -> dict:
        return {
            "speech_segments": [s.to_dict() for s in self.speech_segments],
            "speech_segment_count": len(self.speech_segments),
            "speech_ratio": round(self.speech_ratio, 4),
            "total_speech_sec": round(self.total_speech_sec, 4),
            "total_silence_sec": round(self.total_silence_sec, 4),
        }


class VoiceActivityDetector:
    """Детектор речевой активности на основе адаптивного порога RMS.

    Алгоритм:
    1. Делит аудио на фреймы заданной длительности.
    2. Вычисляет RMS (энергию) каждого фрейма.
    3. Строит адаптивный порог: медиана RMS тихих 20% фреймов + margin_db.
    4. Применяет гистерезис: N подряд идущих речевых фреймов открывают сегмент,
       M подряд идущих тихих фреймов закрывают его.
    """

    def __init__(
        self,
        margin_db: float = 10.0,
        onset_frames: int = 3,
        offset_frames: int = 5,
        quiet_percentile: float = 20.0,
        min_speech_duration_sec: float = 0.05,
    ) -> None:
        """
        Args:
            margin_db: добавка к медиане тихих фреймов для порога (дБ).
            onset_frames: сколько подряд идущих «речевых» фреймов нужно
                          для начала сегмента речи (hysteresis on).
            offset_frames: сколько подряд идущих «тихих» фреймов нужно
                           для завершения сегмента (hysteresis off).
            quiet_percentile: процент «тихих» фреймов для вычисления
                              базового шума (0–50).
            min_speech_duration_sec: минимальная длина речевого сегмента
                                     в секундах; более короткие отбрасываются.
        """
        self.margin_db = float(margin_db)
        self.onset_frames = max(1, int(onset_frames))
        self.offset_frames = max(1, int(offset_frames))
        self.quiet_percentile = float(np.clip(quiet_percentile, 1.0, 49.9))
        self.min_speech_duration_sec = float(min_speech_duration_sec)

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def detect(
        self,
        audio: np.ndarray,
        sample_rate: int,
        frame_ms: int = 30,
    ) -> VADResult:
        """Обнаруживает участки речи в аудио.

        Args:
            audio: numpy-массив float32/float64, нормализованный в [-1, 1].
                   Многоканальные данные усредняются в моно.
            sample_rate: частота дискретизации в Гц.
            frame_ms: длина фрейма анализа в миллисекундах (по умолчанию 30).

        Returns:
            VADResult с временными метками речевых сегментов и статистикой.
        """
        if sample_rate <= 0:
            logger.warning("VAD: некорректная частота дискретизации %d", sample_rate)
            return VADResult()

        audio = self._to_mono(audio)
        if len(audio) == 0:
            return VADResult()

        frame_size = max(1, int(sample_rate * frame_ms / 1000))
        frame_rms = self._compute_frame_rms(audio, frame_size)

        if len(frame_rms) == 0:
            return VADResult()

        threshold_rms = self._adaptive_threshold(frame_rms)
        is_speech = frame_rms >= threshold_rms

        # Применяем гистерезис
        speech_frames = self._apply_hysteresis(is_speech)

        # Строим сегменты речи
        segments = self._build_segments(
            speech_frames=speech_frames,
            frame_rms=frame_rms,
            frame_size=frame_size,
            sample_rate=sample_rate,
            total_samples=len(audio),
        )

        # Статистика
        total_sec = len(audio) / sample_rate
        total_speech_sec = sum(s.duration_sec for s in segments)
        total_silence_sec = max(0.0, total_sec - total_speech_sec)
        speech_ratio = total_speech_sec / total_sec if total_sec > 0 else 0.0

        return VADResult(
            speech_segments=segments,
            speech_ratio=float(np.clip(speech_ratio, 0.0, 1.0)),
            total_speech_sec=total_speech_sec,
            total_silence_sec=total_silence_sec,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _to_mono(audio: np.ndarray) -> np.ndarray:
        """Конвертирует многоканальное аудио в моно."""
        if audio.ndim > 1:
            return audio.mean(axis=1).astype(np.float32)
        return audio.astype(np.float32)

    @staticmethod
    def _compute_frame_rms(audio: np.ndarray, frame_size: int) -> np.ndarray:
        """Вычисляет RMS для каждого фрейма."""
        n_samples = len(audio)
        n_frames = max(n_samples // frame_size, 1)
        frames = np.array_split(audio, n_frames)
        rms_values = []
        for f in frames:
            if len(f) > 0:
                rms = float(np.sqrt(np.mean(f.astype(np.float64) ** 2)))
            else:
                rms = 0.0
            rms_values.append(rms)
        return np.array(rms_values, dtype=np.float64)

    def _adaptive_threshold(self, frame_rms: np.ndarray) -> float:
        """Вычисляет адаптивный порог: медиана тихих фреймов + margin_db.

        Берём самые тихие quiet_percentile% фреймов как оценку шума,
        затем добавляем margin_db для разделения речи/шума.
        """
        cutoff_idx = max(1, int(len(frame_rms) * self.quiet_percentile / 100.0))
        sorted_rms = np.sort(frame_rms)
        quiet_rms = sorted_rms[:cutoff_idx]
        noise_rms = float(np.median(quiet_rms))
        noise_db = _rms_to_db(noise_rms)
        threshold_db = noise_db + self.margin_db
        # Переводим порог обратно в RMS
        threshold_rms = 10.0 ** (threshold_db / 20.0)

        # Ограничение: если сигнал содержит только «громкие» фреймы
        # (нет настоящей тишины), порог не должен превышать медиану всех фреймов.
        # Это предотвращает ложную классификацию непрерывного тона как тишины.
        # Порог применяется только когда медиана достаточно велика (> -60 дБ),
        # чтобы не принимать подлинную тишину за речь.
        _MIN_SPEECH_RMS = 10.0 ** (-60.0 / 20.0)  # ≈ 0.001 (-60 dB)
        median_rms = float(np.median(frame_rms))
        if median_rms > _MIN_SPEECH_RMS:
            threshold_rms = min(threshold_rms, median_rms)
        return threshold_rms

    def _apply_hysteresis(self, is_speech: np.ndarray) -> np.ndarray:
        """Применяет гистерезис к бинарной последовательности речь/тишина.

        onset_frames: N подряд идущих «речевых» фреймов → начало сегмента.
        offset_frames: M подряд идущих «тихих» фреймов → конец сегмента.
        """
        n = len(is_speech)
        result = np.zeros(n, dtype=bool)
        in_speech = False
        consecutive_speech = 0
        consecutive_silence = 0

        for i in range(n):
            if is_speech[i]:
                consecutive_speech += 1
                consecutive_silence = 0
                if not in_speech and consecutive_speech >= self.onset_frames:
                    # Помечаем фреймы начала (onset)
                    start = max(0, i - self.onset_frames + 1)
                    result[start: i + 1] = True
                    in_speech = True
                elif in_speech:
                    result[i] = True
            else:
                consecutive_silence += 1
                consecutive_speech = 0
                if in_speech:
                    if consecutive_silence < self.offset_frames:
                        # Продолжаем речевой сегмент (ждём N тихих фреймов)
                        result[i] = True
                    else:
                        # Сбрасываем trailing silence
                        trail_start = i - self.offset_frames + 1
                        if trail_start >= 0:
                            result[trail_start: i + 1] = False
                        in_speech = False
                        consecutive_silence = 0

        return result

    def _build_segments(
        self,
        speech_frames: np.ndarray,
        frame_rms: np.ndarray,
        frame_size: int,
        sample_rate: int,
        total_samples: int,
    ) -> list[SpeechSegment]:
        """Строит список SpeechSegment из бинарной маски речевых фреймов."""
        segments: list[SpeechSegment] = []
        n = len(speech_frames)
        in_speech = False
        seg_start_frame = 0
        seg_rms_sum = 0.0
        seg_frame_count = 0

        for i in range(n):
            if speech_frames[i] and not in_speech:
                in_speech = True
                seg_start_frame = i
                seg_rms_sum = float(frame_rms[i])
                seg_frame_count = 1
            elif speech_frames[i] and in_speech:
                seg_rms_sum += float(frame_rms[i])
                seg_frame_count += 1
            elif not speech_frames[i] and in_speech:
                in_speech = False
                seg = self._make_segment(
                    start_frame=seg_start_frame,
                    end_frame=i,
                    frame_size=frame_size,
                    sample_rate=sample_rate,
                    total_samples=total_samples,
                    mean_rms=seg_rms_sum / max(seg_frame_count, 1),
                )
                if seg.duration_sec >= self.min_speech_duration_sec:
                    segments.append(seg)

        # Закрываем последний сегмент если запись кончается речью
        if in_speech:
            seg = self._make_segment(
                start_frame=seg_start_frame,
                end_frame=n,
                frame_size=frame_size,
                sample_rate=sample_rate,
                total_samples=total_samples,
                mean_rms=seg_rms_sum / max(seg_frame_count, 1),
            )
            if seg.duration_sec >= self.min_speech_duration_sec:
                segments.append(seg)

        return segments

    @staticmethod
    def _make_segment(
        start_frame: int,
        end_frame: int,
        frame_size: int,
        sample_rate: int,
        total_samples: int,
        mean_rms: float,
    ) -> SpeechSegment:
        """Создаёт SpeechSegment из номеров фреймов."""
        start_sample = start_frame * frame_size
        end_sample = min(end_frame * frame_size, total_samples)
        start_sec = start_sample / sample_rate
        end_sec = end_sample / sample_rate
        duration_sec = end_sec - start_sec
        energy_db = _rms_to_db(mean_rms)
        return SpeechSegment(
            start_sec=start_sec,
            end_sec=end_sec,
            duration_sec=duration_sec,
            energy_db=energy_db,
        )
