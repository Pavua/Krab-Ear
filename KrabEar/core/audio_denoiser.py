"""Адаптивное шумоподавление для STT-пайплайна Krab Ear.

AudioDenoiser применяет шумоподавление только тогда, когда SNR аудиосигнала
ниже заданного порога. Если установлен пакет ``noisereduce`` — используется он
(более качественный результат); иначе применяется встроенный алгоритм
спектрального вычитания (spectral gating) на базе scipy/numpy.

Пример использования::

    from core.audio_denoiser import AudioDenoiser
    from core.noise_profiler import NoiseProfiler

    profiler = NoiseProfiler()
    denoiser = AudioDenoiser()

    profile = profiler.profile(audio, sample_rate)
    if profile.snr_db < threshold:
        audio = denoiser.denoise(audio, sample_rate, strength="moderate")
"""

from __future__ import annotations

import logging
from typing import Literal

import numpy as np

logger = logging.getLogger("KrabEar.AudioDenoiser")

# Уровни силы шумоподавления
DenoisStrength = Literal["off", "light", "moderate", "strong"]

# Параметры spectral gating по уровням силы
_STRENGTH_PARAMS: dict[str, dict] = {
    "light":    {"prop_decrease": 0.5, "n_std_thresh_stationary": 1.0},
    "moderate": {"prop_decrease": 0.75, "n_std_thresh_stationary": 1.5},
    "strong":   {"prop_decrease": 0.95, "n_std_thresh_stationary": 2.0},
}

# Размер окна оценки noise floor в миллисекундах
_NOISE_FLOOR_WINDOW_MS = 200

# Количество семплов в окне noise floor @ 16 кГц (дефолтная частота STT)
_NOISE_FLOOR_SAMPLES = 3200

# Размер FFT-окна для spectral gating
_N_FFT = 512
_HOP = _N_FFT // 4


def _find_noise_window(audio: np.ndarray, sample_rate: int, window_ms: int = _NOISE_FLOOR_WINDOW_MS) -> int:
    """Находит начало самого тихого окна в аудиосигнале для оценки noise floor.

    Вместо использования фиксированных первых 200 мс (которые могут содержать
    речь, если пользователь начал говорить сразу после хоткея), находим окно
    с минимальным RMS. Это защищает от ситуации, когда речевые гармоники
    попадают в noise reference и подавляются.

    Args:
        audio: float64 моно-аудиомассив.
        sample_rate: частота дискретизации в Гц.
        window_ms: размер окна в миллисекундах.

    Returns:
        Индекс начала тишайшего окна (в семплах).
    """
    window = int(window_ms * sample_rate / 1000)
    if len(audio) < window:
        return 0

    n_windows = len(audio) // window
    rms_per = np.array([
        np.sqrt(np.mean(audio[i * window:(i + 1) * window] ** 2))
        for i in range(n_windows)
    ])
    quietest_idx = int(np.argmin(rms_per))
    start = quietest_idx * window
    logger.debug(
        "[Denoiser] noise window: idx=%d start=%d rms=%.4f (из %d окон)",
        quietest_idx, start, rms_per[quietest_idx], n_windows,
    )
    return start


class AudioDenoiser:
    """Адаптивный деноизер аудиосигнала.

    Алгоритм:
    1. Если ``noisereduce`` установлен — делегируем ему (stationary mode).
    2. Иначе — собственная реализация spectral gating через STFT (scipy.signal):
       a. Оцениваем noise floor по тишайшему 200-мс окну (W1062 F1 fix: ранее
          всегда брались первые 200 мс, что приводило к подавлению речи, если
          пользователь начинал говорить сразу после нажатия хоткея).
       b. Вычисляем mask: бины ниже noise_floor * gain_thresh → приглушаем.
       c. Применяем маску в частотной области, восстанавливаем через ISTFT.
       d. Клипуем результат в [-1, 1].
    """

    def __init__(self) -> None:
        self._has_noisereduce = self._check_noisereduce()

    @staticmethod
    def _check_noisereduce() -> bool:
        """Проверяет наличие пакета noisereduce (optional dep)."""
        try:
            import noisereduce  # noqa: F401
            return True
        except ImportError:
            return False

    def denoise(
        self,
        audio: np.ndarray,
        sample_rate: int,
        strength: DenoisStrength = "moderate",
    ) -> np.ndarray:
        """Применяет шумоподавление к аудиосигналу.

        Args:
            audio: numpy float32/float64 массив в диапазоне [-1, 1].
                   Многоканальное аудио автоматически усредняется в моно.
            sample_rate: частота дискретизации в Гц.
            strength: уровень шумоподавления.
                ``"off"``      — без обработки (passthrough).
                ``"light"``    — лёгкое подавление (50%, 1σ).
                ``"moderate"`` — умеренное подавление (75%, 1.5σ) — дефолт.
                ``"strong"``   — сильное подавление (95%, 2σ).

        Returns:
            Аудиомассив той же формы, значения клипованы в [-1, 1].
        """
        if strength == "off":
            return audio

        # Моно-конвертация
        mono = audio
        if audio.ndim > 1:
            mono = audio.mean(axis=1)
        mono = np.asarray(mono, dtype=np.float64)

        if len(mono) < _N_FFT * 2:
            # Слишком короткое аудио — без обработки
            logger.debug("[Denoiser] аудио слишком короткое, пропускаем")
            return audio

        params = _STRENGTH_PARAMS.get(strength, _STRENGTH_PARAMS["moderate"])

        if self._has_noisereduce:
            denoised = self._denoise_noisereduce(mono, sample_rate, params)
        else:
            denoised = self._denoise_spectral_gating(mono, sample_rate, params)

        # Клипуем в [-1, 1]
        denoised = np.clip(denoised, -1.0, 1.0)

        # Восстанавливаем оригинальную форму (если был многоканальный — остаётся моно)
        return denoised.astype(audio.dtype)

    # ------------------------------------------------------------------
    # noisereduce backend
    # ------------------------------------------------------------------

    @staticmethod
    def _denoise_noisereduce(
        audio: np.ndarray,
        sample_rate: int,
        params: dict,
    ) -> np.ndarray:
        """Шумоподавление через пакет noisereduce (stationary mode)."""
        import noisereduce as nr  # type: ignore

        # Оцениваем noise floor по тишайшему 200-мс окну (W1062 F1 fix)
        window = int(_NOISE_FLOOR_WINDOW_MS * sample_rate / 1000)
        noise_start = _find_noise_window(audio, sample_rate, _NOISE_FLOOR_WINDOW_MS)
        noise_clip = audio[noise_start:noise_start + window] if len(audio) > window else audio

        result = nr.reduce_noise(
            y=audio,
            sr=sample_rate,
            y_noise=noise_clip,
            prop_decrease=params["prop_decrease"],
            stationary=True,
            n_std_thresh_stationary=params["n_std_thresh_stationary"],
        )
        return np.asarray(result, dtype=np.float64)

    # ------------------------------------------------------------------
    # Spectral gating (встроенный fallback)
    # ------------------------------------------------------------------

    @staticmethod
    def _denoise_spectral_gating(
        audio: np.ndarray,
        sample_rate: int,
        params: dict,
    ) -> np.ndarray:
        """Встроенная реализация spectral gating через STFT/ISTFT.

        Алгоритм spectral subtraction:
        1. Оцениваем noise floor через тишайшее 200-мс окно (W1062 F1 fix).
        2. Вычисляем STFT всего сигнала.
        3. Для каждого бина: если амплитуда < noise_threshold * factor → подавляем.
        4. Восстанавливаем через ISTFT.
        """
        try:
            from scipy.signal import stft, istft  # type: ignore
        except ImportError:
            # scipy не установлен — возвращаем без обработки
            logger.warning("[Denoiser] scipy не установлен, spectral gating пропущен")
            return audio

        prop_decrease: float = params["prop_decrease"]
        n_std: float = params["n_std_thresh_stationary"]

        # 1. Noise floor estimate по тишайшему 200-мс окну (W1062 F1 fix)
        window = int(_NOISE_FLOOR_WINDOW_MS * sample_rate / 1000)
        noise_start = _find_noise_window(audio, sample_rate, _NOISE_FLOOR_WINDOW_MS)
        noise_clip = audio[noise_start:noise_start + window] if len(audio) > window else audio

        _, _, noise_stft = stft(noise_clip, fs=sample_rate, nperseg=_N_FFT, noverlap=_N_FFT - _HOP)
        noise_amp = np.abs(noise_stft)
        noise_mean = np.mean(noise_amp, axis=1, keepdims=True)   # (freq, 1)
        noise_std = np.std(noise_amp, axis=1, keepdims=True)     # (freq, 1)
        noise_thresh = noise_mean + n_std * noise_std             # (freq, 1) порог

        # 2. STFT всего сигнала
        freqs, times, sig_stft = stft(audio, fs=sample_rate, nperseg=_N_FFT, noverlap=_N_FFT - _HOP)
        sig_amp = np.abs(sig_stft)
        sig_phase = np.angle(sig_stft)

        # 3. Spectral mask: бины ниже порога → уменьшаем пропорционально
        mask = np.where(sig_amp >= noise_thresh, 1.0, 1.0 - prop_decrease)
        denoised_amp = sig_amp * mask

        # 4. ISTFT
        denoised_stft = denoised_amp * np.exp(1j * sig_phase)
        _, denoised = istft(denoised_stft, fs=sample_rate, nperseg=_N_FFT, noverlap=_N_FFT - _HOP)

        # Выравниваем длину по исходной
        n = len(audio)
        if len(denoised) > n:
            denoised = denoised[:n]
        elif len(denoised) < n:
            denoised = np.pad(denoised, (0, n - len(denoised)))

        return np.asarray(denoised, dtype=np.float64)
