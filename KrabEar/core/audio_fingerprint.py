"""AudioFingerprinter — аудио-фингерпринтинг для обнаружения дублирующихся аудиозаписей.

Генерирует компактный хеш из спектральных признаков аудио (спектральный центроид
и частота пересечений нуля) и сравнивает отпечатки для выявления дубликатов.

Зависимости: только numpy (без внешних библиотек).
"""

from __future__ import annotations

import hashlib
import struct
from typing import Sequence

import numpy as np


# Параметр окна анализа: ~23 мс при 16 кГц
_DEFAULT_WINDOW_SIZE = 512
# Количество частотных бинов, используемых для центроида
_SPECTRAL_BINS = 256


class AudioFingerprinter:
    """Генерирует и сравнивает аудио-фингерпринты для обнаружения дубликатов.

    Алгоритм:
      1. Аудио разбивается на фреймы фиксированной длины.
      2. Для каждого фрейма вычисляется спектральный центроид (через FFT)
         и частота пересечений нуля (Zero Crossing Rate).
      3. Среднее и стандартное отклонение признаков квантуются в 8-битные значения.
      4. Квантованные значения сериализуются и хешируются SHA-256.
    """

    def __init__(self, window_size: int = _DEFAULT_WINDOW_SIZE) -> None:
        self._window_size = window_size

    # ── Публичный API ────────────────────────────────────────────────────────

    def fingerprint(self, audio: np.ndarray, sample_rate: int) -> str:
        """Генерирует компактный SHA-256 хеш из аудио-признаков.

        Args:
            audio: numpy-массив формы (N,) или (channels, N). При многоканальном
                   вводе берётся среднее по каналам.
            sample_rate: частота дискретизации в Гц (используется для нормализации
                         спектральных частот).

        Returns:
            Строка SHA-256 hex-дайджеста длиной 64 символа.
        """
        mono = self._to_mono_float32(audio)
        features = self._extract_features(mono, sample_rate)
        return self._hash_features(features)

    def compare(self, fp1: str, fp2: str) -> float:
        """Сравнивает два фингерпринта и возвращает сходство [0.0, 1.0].

        Точное совпадение возвращает 1.0, разные строки SHA-256 сравниваются
        побайтово (число совпадающих битов Хэмминга, нормализованное).

        Args:
            fp1: SHA-256 hex-строка от :meth:`fingerprint`.
            fp2: SHA-256 hex-строка от :meth:`fingerprint`.

        Returns:
            Значение сходства: 1.0 — идентично, 0.0 — максимально различно.
        """
        if not fp1 or not fp2:
            return 0.0
        if fp1 == fp2:
            return 1.0

        try:
            bytes1 = bytes.fromhex(fp1)
            bytes2 = bytes.fromhex(fp2)
        except ValueError:
            return 0.0

        if len(bytes1) != len(bytes2):
            return 0.0

        # Число совпадающих бит (дополнение расстояния Хэмминга)
        total_bits = len(bytes1) * 8
        differing_bits = sum(bin(b1 ^ b2).count("1") for b1, b2 in zip(bytes1, bytes2))
        return (total_bits - differing_bits) / total_bits

    def is_duplicate_audio(
        self,
        audio1: np.ndarray,
        audio2: np.ndarray,
        sample_rate: int = 16000,
        threshold: float = 0.95,
    ) -> bool:
        """Проверяет, являются ли два аудио-массива дубликатами.

        Args:
            audio1: первый аудио-массив.
            audio2: второй аудио-массив.
            sample_rate: частота дискретизации (одинакова для обоих массивов).
            threshold: порог сходства [0..1], по умолчанию 0.95.

        Returns:
            True, если сходство фингерпринтов >= threshold.
        """
        fp1 = self.fingerprint(audio1, sample_rate)
        fp2 = self.fingerprint(audio2, sample_rate)
        return self.compare(fp1, fp2) >= threshold

    # ── Внутренние методы ────────────────────────────────────────────────────

    @staticmethod
    def _to_mono_float32(audio: np.ndarray) -> np.ndarray:
        """Приводит аудио к форме (N,) float32 с нормализацией амплитуды."""
        arr = np.asarray(audio, dtype=np.float32)
        if arr.ndim == 0:
            return np.zeros(1, dtype=np.float32)
        if arr.ndim == 2:
            # (channels, samples) или (samples, channels) — берём меньшую размерность как каналы
            if arr.shape[0] < arr.shape[1]:
                arr = arr.mean(axis=0)
            else:
                arr = arr.mean(axis=1)
        elif arr.ndim > 2:
            arr = arr.reshape(-1)

        # Нормализация по максимальной амплитуде для инвариантности к громкости
        peak = np.max(np.abs(arr))
        if peak > 1e-7:
            arr = arr / peak
        return arr

    def _extract_features(self, mono: np.ndarray, sample_rate: int) -> list[float]:
        """Извлекает вектор признаков: [sc_mean, sc_std, zcr_mean, zcr_std].

        Возвращает список из 4 float-значений.
        """
        n = len(mono)
        if n < self._window_size:
            # Дополняем нулями, чтобы иметь хотя бы один фрейм
            mono = np.pad(mono, (0, self._window_size - n))
            n = self._window_size

        num_frames = n // self._window_size
        spectral_centroids: list[float] = []
        zcr_values: list[float] = []

        freqs = np.fft.rfftfreq(self._window_size, d=1.0 / sample_rate)

        for i in range(num_frames):
            frame = mono[i * self._window_size: (i + 1) * self._window_size]

            # Спектральный центроид
            magnitude = np.abs(np.fft.rfft(frame))
            mag_sum = magnitude.sum()
            if mag_sum > 1e-10:
                centroid = float(np.dot(freqs, magnitude) / mag_sum)
            else:
                centroid = 0.0
            spectral_centroids.append(centroid)

            # Частота пересечений нуля (нормализована к [0, 1])
            zcr = float(np.mean(np.abs(np.diff(np.sign(frame)))) / 2.0)
            zcr_values.append(zcr)

        sc_arr = np.array(spectral_centroids, dtype=np.float64)
        zcr_arr = np.array(zcr_values, dtype=np.float64)

        return [
            float(sc_arr.mean()),
            float(sc_arr.std()),
            float(zcr_arr.mean()),
            float(zcr_arr.std()),
        ]

    @staticmethod
    def _hash_features(features: Sequence[float]) -> str:
        """Квантует признаки в 16-бит unsigned int и возвращает SHA-256 hex."""
        # Нормализуем к [0, 65535] — используем фиксированные диапазоны
        # Спектральный центроид: 0–8000 Гц (покрывает 16 кГц Nyquist)
        # ZCR: 0–1 (уже нормализован)
        scales = [8000.0, 8000.0, 1.0, 1.0]  # по паре mean/std для SC и ZCR

        quantized: list[int] = []
        for value, scale in zip(features, scales):
            # Клипируем к [0, scale], масштабируем к [0, 65535]
            clamped = max(0.0, min(float(value), scale))
            q = int(round(clamped / scale * 65535))
            quantized.append(q)

        # Сериализуем как 4 × uint16 big-endian
        raw = struct.pack(">4H", *quantized)
        return hashlib.sha256(raw).hexdigest()
