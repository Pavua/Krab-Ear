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

    def fingerprint(self, audio: np.ndarray, sample_rate: int) -> str | None:
        """Генерирует компактный SHA-256 хеш из аудио-признаков.

        Args:
            audio: numpy-массив формы (N,) или (channels, N). При многоканальном
                   вводе берётся среднее по каналам.
            sample_rate: частота дискретизации в Гц (используется для нормализации
                         спектральных частот).

        Returns:
            Строка SHA-256 hex-дайджеста длиной 64 символа, или None если аудио
            содержит NaN/Inf значения (не поддаётся надёжному фингерпринтингу,
            не должно быть засчитано как дубликат).
        """
        mono = self._to_mono_float32(audio)
        if mono is None:
            return None
        features = self._extract_features(mono, sample_rate)
        return self._hash_features(features)

    def equals(self, fp1: str | None, fp2: str | None) -> bool:
        """Проверяет точное совпадение двух фингерпринтов.

        SHA-256 — криптографический хеш. Лавинный эффект делает расстояние
        Хэмминга между любыми двумя разными хешами статистически бессмысленным
        (~50% совпадающих бит). Перцептивное сходство («похожие») НЕ
        поддерживается — только точное совпадение (W1063).

        Args:
            fp1: SHA-256 hex-строка от :meth:`fingerprint`.
            fp2: SHA-256 hex-строка от :meth:`fingerprint`.

        Returns:
            True, если фингерпринты идентичны (аудио одинаково); False иначе.
        """
        if not fp1 or not fp2:
            return False
        return fp1 == fp2

    def compare(self, fp1: str, fp2: str) -> float:
        """Сравнивает два фингерпринта и возвращает сходство [0.0, 1.0].

        .. deprecated::
            После W1063 этот метод является deprecated shim. SHA-256 Hamming
            distance статистически бессмысленна (~50% для любых неидентичных
            входов из-за лавинного эффекта). Используйте :meth:`equals`.
            Возвращает 1.0 при точном совпадении и 0.0 во всех остальных случаях.

        Args:
            fp1: SHA-256 hex-строка от :meth:`fingerprint`.
            fp2: SHA-256 hex-строка от :meth:`fingerprint`.

        Returns:
            1.0 если идентично, 0.0 иначе (binary — не непрерывная шкала).
        """
        import warnings as _warnings  # noqa: PLC0415
        _warnings.warn(
            "AudioFingerprinter.compare() is deprecated; use equals() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # W1063: SHA-256 Hamming distance is statistically meaningless.
        # This shim preserves backwards-compatible float return type only.
        return 1.0 if self.equals(fp1, fp2) else 0.0

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
        # W1063: use equals() — threshold is irrelevant for SHA-256 exact match
        return self.equals(fp1, fp2)

    # ── Внутренние методы ────────────────────────────────────────────────────

    @staticmethod
    def _to_mono_float32(audio: np.ndarray) -> np.ndarray | None:
        """Приводит аудио к форме (N,) float32 с нормализацией амплитуды.

        Returns:
            Нормализованный массив float32 или None если аудио содержит
            NaN/Inf значения (вызывающий должен вернуть None fingerprint,
            чтобы не допустить ложных совпадений дубликатов).
        """
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

        # Пустой массив после reshape — возвращаем безопасный дефолт (1 тихий сэмпл).
        # np.max на массиве нулевой длины бросает ValueError — защищаем здесь.
        if arr.size == 0:
            return np.zeros(1, dtype=np.float32)

        # Проверка на NaN/Inf ДО нормализации — такие данные нельзя надёжно
        # хешировать (любые NaN-данные дают те же признаки, что и тишина,
        # что ведёт к ложным дубликатам).
        if not np.all(np.isfinite(arr)):
            return None

        # Нормализация по максимальной амплитуде для инвариантности к громкости
        peak = np.max(np.abs(arr))
        if peak > 1e-7:
            arr = arr / peak
        return arr

    def _extract_features(self, mono: np.ndarray, sample_rate: int) -> list[float]:
        """Извлекает вектор признаков: [sc_mean, sc_std, zcr_mean, zcr_std].

        Возвращает список из 4 float-значений.
        """
        # sample_rate=0 вызывает ZeroDivisionError в rfftfreq(d=1.0/sr).
        # Используем max(sr, 1) чтобы получить безопасный (хотя неточный)
        # результат вместо краша; при sr=0 все частоты будут 0 Гц.
        safe_sr = max(int(sample_rate), 1)

        n = len(mono)
        if n < self._window_size:
            # Дополняем нулями, чтобы иметь хотя бы один фрейм
            mono = np.pad(mono, (0, self._window_size - n))
            n = self._window_size

        num_frames = n // self._window_size
        spectral_centroids: list[float] = []
        zcr_values: list[float] = []

        freqs = np.fft.rfftfreq(self._window_size, d=1.0 / safe_sr)

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
