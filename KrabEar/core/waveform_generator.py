"""Генератор данных аудиовизуализации для Krab Ear.

WaveformGenerator понижает дискретизацию аудио до num_points бинов,
каждый бин = максимальная абсолютная амплитуда окна.
Используется GUI-слоем для отображения waveform-графика.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger("KrabEar.Core.WaveformGenerator")

# ---------------------------------------------------------------------------
# DoS guard constants (W18)
# ---------------------------------------------------------------------------
# Upper bound on num_points that callers may request.  Values above this
# threshold trigger a linear CPU loop of length num_points and an
# np.linspace allocation of the same size — both scale linearly with the
# value, making unbounded input a straightforward CPU/memory DoS vector.
_MAX_NUM_POINTS: int = 100_000

# Maximum total sample *frames* (samples × channels) that
# generate_from_file will load into RAM.  At float32 (4 bytes) this is
# ~400 MB for a single read; the transient peak (abs + mean copies) is
# ~3×, so we cap well below the point at which the 36 GB machine OOM-kills
# the backend.  3 h / 48 kHz / stereo ≈ 1_036_800_000 frames — far above
# this cap, so the gate fires before any allocation.
_MAX_FILE_FRAMES: int = 100_000_000  # ~34 min mono 48 kHz, ~17 min stereo


# ---------------------------------------------------------------------------
# Numeric safety guard (W1539 F4 / W1442 pattern)
# ---------------------------------------------------------------------------
# numpy can produce NaN/Inf from corrupt/all-zero audio (e.g. NaN input → NaN
# RMS, Inf from log(0) in dB conversions). Both are NOT valid JSON (RFC 8259)
# — Swift's JSONDecoder rejects them, silently blanking the waveform UI.

def _safe_float(v: float, default: float = 0.0) -> float:
    """Coerce NaN/Inf/non-numeric numpy result to a finite default value."""
    if not isinstance(v, (int, float)):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return float(v)


@dataclass
class WaveformData:
    """Нормализованные данные waveform для визуализации.

    Attributes:
        points: Список амплитуд в диапазоне [0.0, 1.0] (num_points значений).
        duration_sec: Длительность аудио в секундах.
        sample_rate: Частота дискретизации исходного аудио (Гц).
        peak_amplitude: Максимальная абсолютная амплитуда исходного сигнала.
        rms_amplitude: Среднеквадратичная амплитуда исходного сигнала.
    """

    points: list[float] = field(default_factory=list)
    duration_sec: float = 0.0
    sample_rate: int = 16000
    peak_amplitude: float = 0.0
    rms_amplitude: float = 0.0


class WaveformGenerator:
    """Генерирует waveform-данные из numpy-аудио или аудиофайла."""

    # ── Public API ──────────────────────────────────────────────────────

    def generate_waveform(
        self,
        audio: np.ndarray,
        sample_rate: int,
        num_points: int = 200,
    ) -> WaveformData:
        """Генерирует WaveformData из numpy-массива.

        Args:
            audio: 1D или 2D numpy-массив (float или int). Если 2D — усредняются каналы.
            sample_rate: Частота дискретизации (Гц).
            num_points: Количество точек waveform (бинов).

        Returns:
            WaveformData с нормализованными точками [0, 1].
        """
        if num_points < 1:
            raise ValueError(f"num_points должен быть >= 1, получено: {num_points}")
        if num_points > _MAX_NUM_POINTS:
            raise ValueError(
                f"num_points превышает допустимый максимум {_MAX_NUM_POINTS}: получено {num_points}"
            )
        if sample_rate <= 0:
            raise ValueError(f"sample_rate должен быть > 0, получено: {sample_rate}")

        data = self._prepare_mono_float(audio)

        if data.size == 0:
            return WaveformData(
                points=[0.0] * num_points,
                duration_sec=0.0,
                sample_rate=sample_rate,
                peak_amplitude=0.0,
                rms_amplitude=0.0,
            )

        duration_sec = float(data.size) / float(sample_rate)
        peak_amplitude = _safe_float(float(np.abs(data).max()))
        raw_rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
        rms_amplitude = _safe_float(raw_rms)

        points = self._downsample_to_bins(data, num_points, peak_amplitude)

        return WaveformData(
            points=points,
            duration_sec=duration_sec,
            sample_rate=sample_rate,
            peak_amplitude=peak_amplitude,
            rms_amplitude=rms_amplitude,
        )

    def generate_from_file(
        self,
        path: str,
        num_points: int = 200,
    ) -> WaveformData:
        """Читает аудиофайл и генерирует WaveformData.

        Args:
            path: Путь к аудиофайлу (WAV, FLAC, OGG, MP3 и др.).
            num_points: Количество точек waveform (бинов).

        Returns:
            WaveformData с нормализованными точками [0, 1].

        Raises:
            FileNotFoundError: Файл не найден.
            RuntimeError: Ошибка чтения файла.
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"Аудиофайл не найден: {path}")

        # W18: gate on file size *before* loading.  Use soundfile.info() when
        # available (O(1) header read, no PCM allocation).  Fall back to a raw
        # byte-count check (float32 = 4 bytes/sample) as a secondary guard so
        # we never load a file whose decompressed PCM exceeds _MAX_FILE_FRAMES.
        try:
            import soundfile as sf
            try:
                info = sf.info(str(file_path))
                total_frames = info.frames * info.channels
                if total_frames > _MAX_FILE_FRAMES:
                    logger.warning(
                        "generate_from_file: файл превышает лимит (%d > %d frames×ch), "
                        "waveform не загружается",
                        total_frames,
                        _MAX_FILE_FRAMES,
                        extra={"path": str(file_path)},
                    )
                    return WaveformData(
                        points=[],
                        duration_sec=0.0,
                        sample_rate=0,
                        peak_amplitude=0.0,
                        rms_amplitude=0.0,
                    )
            except Exception:
                # soundfile.info() failed (e.g. MP3 probe unsupported) — fall
                # back to a conservative byte-level heuristic.  At float32
                # (4 bytes/sample) a raw PCM file of _MAX_FILE_FRAMES frames
                # would be 4×_MAX_FILE_FRAMES bytes.  Compressed formats are
                # smaller on disk, so we use 2× overhead as a safety margin.
                try:
                    file_bytes = os.path.getsize(str(file_path))
                    byte_budget = _MAX_FILE_FRAMES * 4 * 2  # float32, 2× overhead
                    if file_bytes > byte_budget:
                        logger.warning(
                            "generate_from_file: размер файла %d байт превышает бюджет %d байт",
                            file_bytes,
                            byte_budget,
                            extra={"path": str(file_path)},
                        )
                        return WaveformData(
                            points=[],
                            duration_sec=0.0,
                            sample_rate=0,
                            peak_amplitude=0.0,
                            rms_amplitude=0.0,
                        )
                except OSError:
                    pass  # If we can't stat, proceed; read will fail if truly huge

            data, sample_rate = sf.read(str(file_path), always_2d=False, dtype="float32")
        except (FileNotFoundError, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Не удалось прочитать аудиофайл {path}: {exc}") from exc

        return self.generate_waveform(
            audio=np.asarray(data, dtype=np.float32),
            sample_rate=int(sample_rate),
            num_points=num_points,
        )

    # ── Private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _prepare_mono_float(audio: np.ndarray) -> np.ndarray:
        """Приводит аудио к 1D float32. Многоканальное — усредняем по каналам."""
        try:
            data = np.asarray(audio, dtype=np.float32)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Невалидный аудиобуфер: {exc}") from exc

        if data.ndim == 0:
            return np.array([], dtype=np.float32)
        if data.ndim == 1:
            return data
        if data.ndim == 2:
            # (samples, channels) или (channels, samples) — предполагаем (samples, channels)
            return data.mean(axis=1).astype(np.float32)
        # Для 3D+ сворачиваем до 1D
        return data.reshape(-1).astype(np.float32)

    @staticmethod
    def _downsample_to_bins(
        data: np.ndarray,
        num_points: int,
        peak_amplitude: float,
    ) -> list[float]:
        """Разбивает data на num_points бинов, каждый = max(|amplitudes|) в бине.

        Нормализует результат в [0, 1] относительно peak_amplitude.
        """
        total_samples = data.size
        if total_samples == 0 or peak_amplitude == 0.0:
            return [0.0] * num_points

        abs_data = np.abs(data)

        # Если семплов меньше чем точек — повторяем через linspace индексы
        if total_samples <= num_points:
            indices = np.round(
                np.linspace(0, total_samples - 1, num_points)
            ).astype(int)
            raw_points = abs_data[indices].tolist()
        else:
            # Разбиваем на num_points равных окон, берём max в каждом
            bin_edges = np.linspace(0, total_samples, num_points + 1, dtype=np.float64)
            raw_points: list[float] = []
            for i in range(num_points):
                start = int(bin_edges[i])
                end = int(bin_edges[i + 1])
                if end <= start:
                    end = start + 1
                end = min(end, total_samples)
                raw_points.append(float(abs_data[start:end].max()))

        # Нормализуем: [0, 1]
        return [min(1.0, v / peak_amplitude) for v in raw_points]
