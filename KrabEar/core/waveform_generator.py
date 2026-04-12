"""Генератор данных аудиовизуализации для Krab Ear.

WaveformGenerator понижает дискретизацию аудио до num_points бинов,
каждый бин = максимальная абсолютная амплитуда окна.
Используется GUI-слоем для отображения waveform-графика.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("KrabEar.Core.WaveformGenerator")


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
        peak_amplitude = float(np.abs(data).max())
        rms_amplitude = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))

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

        try:
            import soundfile as sf
            data, sample_rate = sf.read(str(file_path), always_2d=False, dtype="float32")
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
