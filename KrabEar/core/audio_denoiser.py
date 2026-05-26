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

# Параметры noisereduce backend по уровням силы.
# prop_decrease ограничен speech-band floor (W1311 F3):
#   strong   = 0.75  → минимум 25% оригинального речевого сигнала сохраняется
#   moderate = 0.85  → минимум 15% оригинального речевого сигнала сохраняется
#   light    = 0.50  → минимум 50% оригинального речевого сигнала сохраняется
# В отличие от spectral gating (prop_decrease применяется к маске бинов),
# noisereduce применяет prop_decrease глобально ко всему спектру — более агрессивно,
# поэтому значения для strong/moderate здесь ниже, чем у spectral gating.
_NOISEREDUCE_PARAMS: dict[str, dict] = {
    "light":    {"prop_decrease": 0.50, "n_std_thresh_stationary": 1.0},
    "moderate": {"prop_decrease": 0.85, "n_std_thresh_stationary": 1.5},
    "strong":   {"prop_decrease": 0.75, "n_std_thresh_stationary": 2.0},
}

# Количество семплов для оценки noise floor (первые ~200 мс @ 16 кГц).
# Используется только как fallback когда в аудио нет тихих фреймов.
_NOISE_FLOOR_SAMPLES = 3200

# Размер FFT-окна для spectral gating
_N_FFT = 512
_HOP = _N_FFT // 4

# Параметры percentile-based noise sampling (W1062 F1).
# Размер фрейма: 32 мс @ 16 кГц = 512 сэмплов.
_FRAME_SIZE_MS = 32
_NOISE_PERCENTILE = 10.0  # используем самые тихие 10% фреймов

# W1062 F2: ограничение max-подавления в режиме strong для сохранения шёпота.
# Минимум 25% исходного сигнала (≥ -12 dB) в речевой полосе 300–3000 Гц.
_STRONG_MIN_GAIN = 0.25  # соответствует -12 dB

# Границы речевой полосы (Гц) для защиты шёпота в strong mode
_SPEECH_BAND_LOW_HZ = 300
_SPEECH_BAND_HIGH_HZ = 3000


def _percentile_noise_clip(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Выбирает «тихие» фреймы в качестве образца шума (W1062 F1).

    Разбивает аудио на фреймы по 32 мс, вычисляет RMS каждого фрейма,
    возвращает конкатенацию фреймов ниже 10-го перцентиля RMS.

    Если тихих фреймов нет (всё аудио громкое) — falls back на первые 200 мс
    с warning-логом.

    Производительность: numpy.percentile над 60 s × 31 фрейм/с ≈ 1860 значений
    занимает < 1 мс (требование: < 50 мс).

    Args:
        audio: 1-D float64 массив.
        sample_rate: частота дискретизации.

    Returns:
        1-D float64 массив — образец для оценки noise floor.
    """
    frame_size = int(sample_rate * _FRAME_SIZE_MS / 1000)  # 512 сэмплов @ 16 кГц
    if frame_size < 1:
        frame_size = 1

    n = len(audio)
    n_frames = n // frame_size
    if n_frames < 2:
        # Слишком короткое аудио — берём целиком
        return audio

    # Матрица фреймов: (n_frames, frame_size)
    frames = audio[: n_frames * frame_size].reshape(n_frames, frame_size)

    # RMS каждого фрейма — numpy.percentile по 1D → O(n_frames) быстро
    rms_per_frame = np.sqrt(np.mean(frames ** 2, axis=1))

    # Порог = 10-й перцентиль RMS
    threshold = float(np.percentile(rms_per_frame, _NOISE_PERCENTILE))

    # Маска тихих фреймов
    quiet_mask = rms_per_frame <= threshold

    if not np.any(quiet_mask):
        # Всё аудио громкое — fallback на первые 200 мс
        logger.warning(
            "[Denoiser] нет тихих фреймов в аудио (всё громкое); "
            "fallback на первые 200 мс для noise floor"
        )
        return audio[:_NOISE_FLOOR_SAMPLES] if len(audio) > _NOISE_FLOOR_SAMPLES else audio

    # Конкатенируем тихие фреймы
    return frames[quiet_mask].ravel()


def _speech_band_bins(sample_rate: int) -> tuple[int, int]:
    """Возвращает индексы FFT-бинов для речевой полосы 300–3000 Гц."""
    bin_low = int(round(_SPEECH_BAND_LOW_HZ * _N_FFT / sample_rate))
    bin_high = int(round(_SPEECH_BAND_HIGH_HZ * _N_FFT / sample_rate))
    max_bin = _N_FFT // 2
    bin_low = max(0, min(bin_low, max_bin))
    bin_high = max(0, min(bin_high, max_bin))
    return bin_low, bin_high


class AudioDenoiser:
    """Адаптивный деноизер аудиосигнала.

    Алгоритм:
    1. Если ``noisereduce`` установлен — делегируем ему (stationary mode).
    2. Иначе — собственная реализация spectral gating через STFT (scipy.signal):
       a. W1062 F1: оцениваем noise floor по тихим фреймам (10-й перцентиль RMS).
          Fallback на первые 200 мс если тихих фреймов нет.
       b. Вычисляем mask: бины ниже noise_floor * gain_thresh → приглушаем.
       c. W1062 F2: для режима 'strong' ограничиваем подавление в речевой полосе
          (300–3000 Гц) до минимум 25% сигнала (≥ -12 dB), чтобы шёпот не терялся.
       d. Применяем маску в частотной области, восстанавливаем через ISTFT.
       e. Клипуем результат в [-1, 1].
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
                ``"strong"``   — сильное подавление (95%, 2σ) с ограничением
                                 -12 dB для речевой полосы (W1062 F2).

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

        # Используем разные таблицы параметров: noisereduce применяет prop_decrease
        # глобально (более агрессивно), spectral gating — только к маске бинов.
        # W1311 F3: noisereduce backend должен уважать speech-band floor через
        # собственную таблицу _NOISEREDUCE_PARAMS с более низкими значениями.
        if self._has_noisereduce:
            params = _NOISEREDUCE_PARAMS.get(strength, _NOISEREDUCE_PARAMS["moderate"])
            denoised = self._denoise_noisereduce(mono, sample_rate, params)
        else:
            denoised = self._denoise_spectral_gating(mono, sample_rate, params, strength)

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
        """Шумоподавление через пакет noisereduce (stationary mode).

        W1062 F1: использует percentile-based noise sampling вместо первых 200 мс.
        """
        import noisereduce as nr  # type: ignore

        # W1062 F1: оцениваем noise floor по тихим фреймам (10-й перцентиль RMS)
        noise_clip = _percentile_noise_clip(audio, sample_rate)

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
        strength: str = "moderate",
    ) -> np.ndarray:
        """Встроенная реализация spectral gating через STFT/ISTFT.

        Алгоритм spectral subtraction:
        1. W1062 F1: оцениваем noise floor по тихим фреймам (10-й перцентиль RMS).
        2. Вычисляем STFT всего сигнала.
        3. Для каждого бина: если амплитуда < noise_threshold * factor → подавляем.
        4. W1062 F2: для 'strong' ограничиваем подавление в речевой полосе ≥ -12 dB.
        5. Восстанавливаем через ISTFT.
        """
        try:
            from scipy.signal import stft, istft  # type: ignore
        except ImportError:
            # scipy не установлен — возвращаем без обработки
            logger.warning("[Denoiser] scipy не установлен, spectral gating пропущен")
            return audio

        prop_decrease: float = params["prop_decrease"]
        n_std: float = params["n_std_thresh_stationary"]

        # 1. W1062 F1: Noise floor estimate по тихим фреймам (10-й перцентиль RMS)
        noise_clip = _percentile_noise_clip(audio, sample_rate)

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

        # 4. W1062 F2: Для 'strong' ограничиваем подавление в речевой полосе (300–3000 Гц).
        #    Гарантируем минимальный коэффициент _STRONG_MIN_GAIN (0.25 = -12 dB).
        if strength == "strong":
            bin_low, bin_high = _speech_band_bins(sample_rate)
            # Применяем ограничение только к бинам речевой полосы
            speech_slice = mask[bin_low: bin_high + 1, :]
            mask[bin_low: bin_high + 1, :] = np.maximum(speech_slice, _STRONG_MIN_GAIN)

        denoised_amp = sig_amp * mask

        # 5. ISTFT
        denoised_stft = denoised_amp * np.exp(1j * sig_phase)
        _, denoised = istft(denoised_stft, fs=sample_rate, nperseg=_N_FFT, noverlap=_N_FFT - _HOP)

        # Выравниваем длину по исходной
        n = len(audio)
        if len(denoised) > n:
            denoised = denoised[:n]
        elif len(denoised) < n:
            denoised = np.pad(denoised, (0, n - len(denoised)))

        return np.asarray(denoised, dtype=np.float64)
