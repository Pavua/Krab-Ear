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

# Количество семплов для оценки noise floor (первые ~200 мс @ 16 кГц)
_NOISE_FLOOR_SAMPLES = 3200

# Размер FFT-окна для spectral gating
_N_FFT = 512
_HOP = _N_FFT // 4


def _has_whispered_segments(audio: np.ndarray, sr: int) -> bool:
    """Определяет, содержит ли аудио сегменты шёпотной амплитуды.

    Шёпот определяется как ненулевые фреймы в диапазоне -50..-35 dB RMS.
    Если такие фреймы обнаружены, режим ``strong`` должен смягчиться до
    ``moderate``, чтобы не подавить речь вместе с шумом.

    Args:
        audio: 1-D массив float64 в диапазоне [-1, 1].
        sr: частота дискретизации в Гц.

    Returns:
        ``True``, если хотя бы один фрейм попадает в диапазон шёпота.
    """
    frame = int(0.05 * sr)  # 50 мс фреймы
    if frame < 1:
        return False

    # Усечём до кратной длины и разобьём на фреймы
    n_frames = len(audio) // frame
    if n_frames == 0:
        rms_arr = np.array([float(np.sqrt(np.mean(audio ** 2)))])
    else:
        frames = audio[: n_frames * frame].reshape(n_frames, frame)
        rms_arr = np.sqrt(np.mean(frames ** 2, axis=1))

    rms_db = 20.0 * np.log10(np.maximum(rms_arr, 1e-9))
    return bool(np.any((rms_db > -50.0) & (rms_db < -35.0)))


class AudioDenoiser:
    """Адаптивный деноизер аудиосигнала.

    Алгоритм:
    1. Если ``noisereduce`` установлен — делегируем ему (stationary mode).
    2. Иначе — собственная реализация spectral gating через STFT (scipy.signal):
       a. Оцениваем noise floor по первым 200 мс (предполагаем тишину/фон в начале).
       b. Вычисляем mask: бины ниже noise_floor * gain_thresh → приглушаем.
       c. Применяем маску в частотной области, восстанавливаем через ISTFT.
       d. Клипуем результат в [-1, 1].

    W1062 F2 fix: при обнаружении шёпотной амплитуды режим ``strong``
    автоматически понижается до ``moderate``, чтобы не подавить речь.
    W1062 F4 fix: многоканальный вход логирует предупреждение о потере каналов.
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
                   Многоканальное аудио автоматически усредняется в моно
                   (W1062 F4: при этом логируется предупреждение).
            sample_rate: частота дискретизации в Гц.
            strength: уровень шумоподавления.
                ``"off"``      — без обработки (passthrough).
                ``"light"``    — лёгкое подавление (50%, 1σ).
                ``"moderate"`` — умеренное подавление (75%, 1.5σ) — дефолт.
                ``"strong"``   — сильное подавление (95%, 2σ); автоматически
                                 снижается до ``moderate`` при обнаружении
                                 шёпотной амплитуды (W1062 F2).

        Returns:
            Аудиомассив той же формы (кроме многоканального входа — он
            возвращается как моно), значения клипованы в [-1, 1].
        """
        if strength == "off":
            return audio

        # Моно-конвертация (F4: предупреждение о потере каналов)
        mono = audio
        if audio.ndim > 1:
            logger.warning(
                "[Denoiser] многоканальный вход (%s каналов) будет усреднён в моно; "
                "выходной массив будет 1-D",
                audio.shape[1] if audio.ndim == 2 else audio.ndim,
            )
            mono = audio.mean(axis=1)
        mono = np.asarray(mono, dtype=np.float64)

        # F2: Если режим strong и обнаружен шёпот — понижаем до moderate
        effective_strength = strength
        if strength == "strong" and _has_whispered_segments(mono, sample_rate):
            logger.info(
                "[Denoiser] шёпотная амплитуда обнаружена, понижаем strong→moderate"
            )
            effective_strength = "moderate"

        if len(mono) < _N_FFT * 2:
            # Слишком короткое аудио — без обработки
            logger.debug("[Denoiser] аудио слишком короткое, пропускаем")
            return audio

        params = _STRENGTH_PARAMS.get(effective_strength, _STRENGTH_PARAMS["moderate"])

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

        # Оцениваем noise floor по первым ~200 мс
        noise_clip = audio[:_NOISE_FLOOR_SAMPLES] if len(audio) > _NOISE_FLOOR_SAMPLES else audio

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
        1. Оцениваем noise floor через первые ~200 мс.
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

        # 1. Noise floor estimate по первым 200 мс
        noise_clip = audio[:_NOISE_FLOOR_SAMPLES] if len(audio) > _NOISE_FLOOR_SAMPLES else audio

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
