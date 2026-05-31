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

# W1322: параметры noisereduce бэкенда по уровням силы.
# prop_decrease ограничен speech-band floor (W1311 F3):
# В отличие от spectral gating, noisereduce применяет prop_decrease глобально
# ко всему спектру — более агрессивно, поэтому значения ниже, чем в _STRENGTH_PARAMS.
_NOISEREDUCE_PARAMS: dict[str, dict] = {
    "light":    {"prop_decrease": 0.5,  "stationary": True,  "freq_mask_smooth_hz": 500},
    "moderate": {"prop_decrease": 0.75, "stationary": True,  "freq_mask_smooth_hz": 500},
    "strong":   {"prop_decrease": 0.95, "stationary": False, "freq_mask_smooth_hz": 250, "min_attenuation_db": -12.0},  # W1322: floor at -12dB
}

# Количество семплов для оценки noise floor (первые ~200 мс @ 16 кГц).
# Используется только как fallback когда в аудио нет тихих фреймов.
_NOISE_FLOOR_SAMPLES = 3200

# Размер FFT-окна для spectral gating
_N_FFT = 512
_HOP = _N_FFT // 4

# Размер фрейма для RMS-анализа при выборе тихих фреймов (10 мс @ 16 кГц)
_RMS_FRAME_SIZE = 160

# Минимальная доля тихих фреймов при строгом сравнении (<) — если меньше, fallback
_MIN_QUIET_FRACTION = 0.05

# W1062 F2 / W1080: ограничение max-подавления в режиме strong для сохранения речи.
# Минимум 25% исходного сигнала (≥ -12 dB) в речевой полосе 300–3000 Гц.
# Восстановлено в W1718 (body-revert W1071 удалил эти константы и cap-логику).
_STRONG_MIN_GAIN = 0.25  # соответствует -12 dB

# Границы речевой полосы (Гц) для защиты речи в strong mode (W1080)
_SPEECH_BAND_LOW_HZ = 300
_SPEECH_BAND_HIGH_HZ = 3000


def _percentile_noise_clip(
    audio: np.ndarray,
    percentile: float = 10.0,
) -> np.ndarray | None:
    """Выбирает фреймы с наименьшим RMS как noise clip для оценки шума.

    W1062 F1 / W1320: делит аудио на фреймы по ``_RMS_FRAME_SIZE`` сэмплов,
    вычисляет RMS каждого фрейма, затем выбирает фреймы со значением RMS строго
    меньше p-го перцентиля. Это корректно обрабатывает равномерно-громкое аудио
    (HVAC, гул, толпа), где ``<=`` выбирал бы 100% фреймов как «тихие».

    Fallback-поведение:
    - Если строгий ``<`` даёт < ``_MIN_QUIET_FRACTION`` всех фреймов →
      fallback к ``<=`` с предупреждением в лог.
    - Если строгий ``<`` даёт **ноль** фреймов (все RMS одинаковы) →
      возвращает ``None`` с предупреждением. Вызывающий код пропускает деноизинг.

    Args:
        audio: 1-D float64 массив аудиосигнала.
        percentile: перцентиль для определения порога «тихих» фреймов (по умолчанию 10).

    Returns:
        Конкатенация «тихих» фреймов как noise clip, или ``None`` если подходящих
        фреймов нет (uniform audio → denoising should be skipped).
    """
    frame_size = _RMS_FRAME_SIZE
    n_frames = len(audio) // frame_size
    if n_frames == 0:
        # Аудио слишком короткое — используем весь сигнал
        return audio

    frames = audio[:n_frames * frame_size].reshape(n_frames, frame_size)
    rms_per_frame = np.sqrt(np.mean(frames ** 2, axis=1))

    threshold = float(np.percentile(rms_per_frame, percentile))

    # W1311 F1 FIX: строгое < вместо <= чтобы не захватить 100% фреймов
    # на равномерно-громком аудио (HVAC, гул, толпа)
    quiet_mask_strict = rms_per_frame < threshold
    quiet_mask_nonstrict = rms_per_frame <= threshold

    n_quiet_strict = int(np.sum(quiet_mask_strict))
    n_quiet_nonstrict = int(np.sum(quiet_mask_nonstrict))
    n_total = len(rms_per_frame)

    if n_quiet_nonstrict == n_total:
        # Все фреймы удовлетворяют <=, т.е. все RMS одинаковы.
        # Это uniform audio (HVAC, гул) — деноизинг опасен, возвращаем None.
        logger.warning(
            "[Denoiser] _percentile_noise_clip: все %d фреймов имеют одинаковый RMS "
            "(uniform audio) — denoising будет пропущен (возвращаем None)",
            n_total,
        )
        return None

    # Если строгий < даёт 0 фреймов, но нестрогий <= даёт < n_total —
    # значит тихие фреймы существуют (самые тихие равны threshold), используем <=.
    if n_quiet_strict == 0:
        quiet_mask = quiet_mask_nonstrict
    elif n_quiet_strict < n_total * _MIN_QUIET_FRACTION:
        # Меньше 5% тихих фреймов при строгом < — нестандартный сигнал.
        # Fallback к нестрогому <=, предупреждаем.
        logger.warning(
            "[Denoiser] _percentile_noise_clip: строгий < выбрал только %d/%d фреймов "
            "(%.1f%% < min %.1f%%) — fallback к <=",
            n_quiet_strict, n_total,
            100.0 * n_quiet_strict / n_total,
            100.0 * _MIN_QUIET_FRACTION,
        )
        quiet_mask = quiet_mask_nonstrict
    else:
        quiet_mask = quiet_mask_strict

    return frames[quiet_mask].ravel()


def _speech_band_bins(sample_rate: int) -> tuple[int, int]:
    """Возвращает индексы FFT-бинов для речевой полосы 300–3000 Гц.

    W1080: вспомогательная функция для cap-логики в _denoise_spectral_gating
    (режим strong ограничивает подавление в этой полосе до _STRONG_MIN_GAIN).
    """
    bin_low = int(round(_SPEECH_BAND_LOW_HZ * _N_FFT / sample_rate))
    bin_high = int(round(_SPEECH_BAND_HIGH_HZ * _N_FFT / sample_rate))
    max_bin = _N_FFT // 2
    bin_low = max(0, min(bin_low, max_bin))
    bin_high = max(0, min(bin_high, max_bin))
    return bin_low, bin_high


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
       a. W1062 F1 / W1320: оцениваем noise floor по тихим фреймам (10-й перцентиль RMS).
          Fallback на первые 200 мс если тихих фреймов нет.
       b. Вычисляем mask: бины ниже noise_floor * gain_thresh → приглушаем.
       c. W1080: для режима 'strong' ограничиваем подавление в речевой полосе
          (300–3000 Гц) до минимум 25% сигнала (≥ -12 dB), чтобы речь не терялась.
       d. Применяем маску в частотной области, восстанавливаем через ISTFT.
       e. Клипуем результат в [-1, 1].
       f. W1718 BUG3: безопасно приводим к исходному dtype (rescale перед cast для int).

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
            audio: numpy float32/float64/int16/int32 массив.
                   Float-массивы ожидаются в диапазоне [-1, 1].
                   Integer-массивы автоматически нормализуются через np.iinfo.
                   Многоканальное аудио автоматически усредняется в моно
                   (W1062 F4: при этом логируется предупреждение).
            sample_rate: частота дискретизации в Гц.
            strength: уровень шумоподавления.
                ``"off"``      — без обработки (passthrough).
                ``"light"``    — лёгкое подавление (50%, 1σ).
                ``"moderate"`` — умеренное подавление (75%, 1.5σ) — дефолт.
                ``"strong"``   — сильное подавление (95%, 2σ); автоматически
                                 снижается до ``moderate`` при обнаружении
                                 шёпотной амплитуды (W1062 F2); речевая полоса
                                 защищена floor -12 dB (W1080).

        Returns:
            Аудиомассив той же формы (кроме многоканального входа — он
            возвращается как моно), значения клипованы в [-1, 1] (float)
            или в допустимый диапазон dtype (integer).
        """
        if strength == "off":
            return audio

        orig_dtype = audio.dtype

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

        # W1718 BUG3: нормализуем integer-вход в [-1, 1] перед обработкой
        int_input = np.issubdtype(orig_dtype, np.integer)
        if int_input:
            iinfo = np.iinfo(orig_dtype)
            mono = mono / float(iinfo.max)

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
        nr_params = _NOISEREDUCE_PARAMS.get(effective_strength)

        if self._has_noisereduce:
            denoised = self._denoise_noisereduce(mono, sample_rate, params, nr_params=nr_params)
        else:
            denoised = self._denoise_spectral_gating(mono, sample_rate, params, effective_strength)

        # Клипуем в [-1, 1]
        denoised = np.clip(denoised, -1.0, 1.0)

        # W1718 BUG3: безопасно приводим к исходному dtype.
        # Для integer-dtypes rescale обратно в полный диапазон перед cast,
        # иначе [-1, 1] float64 → all-zeros int16.
        if int_input:
            iinfo = np.iinfo(orig_dtype)
            denoised = (denoised * float(iinfo.max)).astype(orig_dtype)
        else:
            denoised = denoised.astype(orig_dtype)

        return denoised

    # ------------------------------------------------------------------
    # noisereduce backend
    # ------------------------------------------------------------------

    @staticmethod
    def _denoise_noisereduce(
        audio: np.ndarray,
        sample_rate: int,
        params: dict,
        nr_params: dict | None = None,
    ) -> np.ndarray:
        """Шумоподавление через пакет noisereduce.

        W1062 F1 / W1320: использует percentile-based noise sampling вместо
        фиксированных первых 200 мс.

        Args:
            audio: 1-D float64 аудиомассив в [-1, 1].
            sample_rate: частота дискретизации в Гц.
            params: параметры spectral gating (prop_decrease, n_std_thresh_stationary).
            nr_params: параметры noisereduce бэкенда (_NOISEREDUCE_PARAMS[strength]).
                       Если None — строится из params с дефолтами.
        """
        import noisereduce as nr  # type: ignore

        # W1062 F1 / W1320: percentile-based noise sampling (не первые 200 мс)
        noise_clip = _percentile_noise_clip(audio)

        # None → uniform audio: пропускаем деноизинг
        if noise_clip is None:
            logger.warning(
                "[Denoiser] noisereduce: ноль тихих фреймов — denoising пропущен (uniform audio)"
            )
            return audio

        if nr_params is not None:
            # W1322: используем _NOISEREDUCE_PARAMS (включая min_attenuation_db для strong)
            call_kwargs: dict = {
                "y": audio,
                "sr": sample_rate,
                "y_noise": noise_clip,
                "prop_decrease": nr_params["prop_decrease"],
                "stationary": nr_params.get("stationary", True),
            }
            if "freq_mask_smooth_hz" in nr_params:
                call_kwargs["freq_mask_smooth_hz"] = nr_params["freq_mask_smooth_hz"]
            if "min_attenuation_db" in nr_params:
                call_kwargs["min_attenuation_db"] = nr_params["min_attenuation_db"]
        else:
            call_kwargs = {
                "y": audio,
                "sr": sample_rate,
                "y_noise": noise_clip,
                "prop_decrease": params["prop_decrease"],
                "stationary": True,
                "n_std_thresh_stationary": params["n_std_thresh_stationary"],
            }

        result = nr.reduce_noise(**call_kwargs)
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
        1. W1062 F1 / W1320: оцениваем noise floor по тихим фреймам (10-й перцентиль RMS).
           Fallback → пропуск деноизинга при uniform audio.
        2. Вычисляем STFT всего сигнала.
        3. Для каждого бина: если амплитуда < noise_threshold * factor → подавляем.
        4. W1080: для 'strong' ограничиваем подавление в речевой полосе ≥ -12 dB.
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

        # 1. W1062 F1 / W1320: Noise floor estimate по тихим фреймам (10-й перцентиль RMS)
        noise_clip = _percentile_noise_clip(audio)

        # None → uniform audio: пропускаем деноизинг
        if noise_clip is None:
            logger.warning(
                "[Denoiser] spectral_gating: ноль тихих фреймов — denoising пропущен (uniform audio)"
            )
            return audio

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

        # 4. W1080: Для 'strong' ограничиваем подавление в речевой полосе (300–3000 Гц).
        #    Гарантируем минимальный коэффициент _STRONG_MIN_GAIN (0.25 = -12 dB).
        #    Восстановлено в W1718 (body-revert W1071 удалил эту защиту).
        if strength == "strong":
            bin_low, bin_high = _speech_band_bins(sample_rate)
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
