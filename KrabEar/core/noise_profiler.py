"""Профилирование шума в аудиозаписях.

NoiseProfiler анализирует фоновый шум: классифицирует тип окружения,
определяет уровень шума в дБ, оценивает SNR и даёт рекомендации по STT.
Используется только numpy — без внешних зависимостей.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from core.silence_detector import SILENCE_THRESHOLD_AMP

logger = logging.getLogger("KrabEar.NoiseProfiler")

# ---------------------------------------------------------------------------
# Параметры анализа
# ---------------------------------------------------------------------------

_FRAME_SIZE = 2048          # размер фрейма в семплах
_QUIET_PERCENTILE = 10      # нижний процентиль RMS считается «тихими» фреймами
# Единый порог тишины — импортируется из silence_detector (SSOT, -40 дБ).
_SILENCE_RMS_THRESHOLD = SILENCE_THRESHOLD_AMP  # RMS ниже порога → тишина
_SNR_STT_THRESHOLD = 15.0   # минимальный SNR для корректной работы STT (dB)
_REF_AMPLITUDE = 1.0        # референсная амплитуда для перевода в dBFS

# Пороги уровня шума по dBFS (примерные значения для разных сред)
_LEVEL_QUIET = -55.0        # quiet: noise floor < -55 dBFS
_LEVEL_OFFICE = -40.0       # office: -55 .. -40 dBFS
_LEVEL_STREET = -25.0       # street: -40 .. -25 dBFS
# выше -25 dBFS → crowd/music (зависит от спектра)

# Диапазоны частот для классификации спектра (в Гц)
_LOW_FREQ_MAX = 400         # низкие частоты: 0–400 Гц
_HIGH_FREQ_MIN = 4000       # высокие частоты: 4000+ Гц
# средние: 400–4000 Гц (речевой диапазон)


def _safe_float(v: float, default: float = 0.0) -> float:
    """Возвращает конечный float или *default* для NaN/Inf (wave-26).

    NaN/Inf во входном аудио (например, полностью NaN-массив) numpy
    распространяет в RMS → noise_level_db/snr_db → IPC-JSON, где
    ``json.dumps(..., allow_nan=False)`` падает на стороне Swift.
    Зеркалит паттерн ``metadata_enricher._sanitize_float`` (wave-25).
    """
    if not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    return float(v)


# ---------------------------------------------------------------------------
# Датаклассы
# ---------------------------------------------------------------------------

@dataclass
class NoiseProfile:
    """Результат анализа шума в аудиозаписи."""

    noise_type: str                     # "quiet" | "office" | "street" | "music" | "crowd"
    noise_level_db: float               # уровень шума в дБFS
    snr_db: float                       # оценка SNR в дБ
    frequency_profile: str              # "low_frequency" | "broadband" | "high_frequency"
    recommendations: list[str] = field(default_factory=list)
    suitable_for_stt: bool = True       # True если SNR > 15 dB

    def to_dict(self) -> dict:
        """Сериализует в словарь для JSON-ответа IPC.

        Числовые поля проходят через ``_safe_float`` (wave-26) как финальный
        барьер на IPC-границе: даже если ``NoiseProfile`` собран напрямую с
        NaN/Inf, ответ остаётся совместимым с ``json.dumps(allow_nan=False)``.
        """
        return {
            "noise_type": self.noise_type,
            "noise_level_db": _safe_float(self.noise_level_db, default=-120.0),
            "snr_db": _safe_float(self.snr_db, default=0.0),
            "frequency_profile": self.frequency_profile,
            "recommendations": self.recommendations,
            "suitable_for_stt": self.suitable_for_stt,
        }


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class NoiseProfiler:
    """Анализатор фонового шума в аудиозаписях.

    Алгоритм:
    1. Разбивает аудио на фреймы и вычисляет RMS каждого.
    2. Нижний процентиль RMS-фреймов — оценка noise floor (тихие моменты).
    3. Верхний квартиль RMS-фреймов — оценка сигнала (активная речь/звук).
    4. SNR = 20*log10(signal_rms / noise_rms).
    5. Спектральный анализ через FFT для классификации частотного профиля.
    6. Тип шума определяется по уровню шума и спектральному профилю.
    """

    def profile(self, audio: np.ndarray, sample_rate: int) -> NoiseProfile:
        """Анализирует аудиоданные и возвращает профиль шума.

        Args:
            audio: numpy-массив float32/float64 в диапазоне [-1, 1].
                   Многоканальное аудио автоматически усредняется в моно.
            sample_rate: частота дискретизации в Гц.

        Returns:
            NoiseProfile с классификацией шума и рекомендациями.
        """
        # Моно-конвертация
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        audio = np.asarray(audio, dtype=np.float64)

        n_samples = len(audio)

        # Пустое аудио — возвращаем профиль тишины
        if n_samples < _FRAME_SIZE:
            return self._silent_profile()

        # --- Фреймовый RMS ---
        frame_rms_values = self._compute_frame_rms(audio)

        # --- Noise floor (тихие фреймы) ---
        noise_rms = float(np.percentile(frame_rms_values, _QUIET_PERCENTILE))
        # wave-26 HIGH guard: NaN/Inf in the audio (e.g. an all-NaN array) propagates
        # through numpy RMS → percentile → dBFS, leaking NaN into noise_level_db
        # (and mis-driving noise_type/suitable_for_stt downstream). Sanitize to the
        # silent-floor sentinel BEFORE classification so every derived field stays
        # consistent and json.dumps(allow_nan=False) on the Swift side never raises.
        noise_level_db = _safe_float(self._rms_to_dbfs(noise_rms), default=-120.0)

        # --- Уровень сигнала (активные фреймы) ---
        signal_rms = float(np.percentile(frame_rms_values, 75))
        if not math.isfinite(signal_rms) or signal_rms < 1e-10:
            signal_rms = _safe_float(float(np.sqrt(np.mean(audio ** 2))), default=0.0)

        # --- SNR ---
        snr_db = _safe_float(
            self._compute_snr(signal_rms, noise_rms, audio, sample_rate), default=0.0
        )

        # --- Спектральный профиль ---
        freq_profile = self._classify_frequency_profile(audio, sample_rate)

        # --- Тип шума ---
        noise_type = self._classify_noise_type(noise_level_db, freq_profile, snr_db)

        # --- Пригодность для STT ---
        suitable_for_stt = snr_db >= _SNR_STT_THRESHOLD

        # --- Рекомендации ---
        recommendations = self._generate_recommendations(
            noise_type, noise_level_db, snr_db, freq_profile, suitable_for_stt
        )

        return NoiseProfile(
            noise_type=noise_type,
            noise_level_db=round(noise_level_db, 2),
            snr_db=round(snr_db, 2),
            frequency_profile=freq_profile,
            recommendations=recommendations,
            suitable_for_stt=suitable_for_stt,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _compute_frame_rms(self, audio: np.ndarray) -> np.ndarray:
        """Вычисляет RMS для каждого фрейма аудио."""
        n_frames = max(len(audio) // _FRAME_SIZE, 1)
        frames = np.array_split(audio, n_frames)
        return np.array([
            float(np.sqrt(np.mean(f ** 2))) if len(f) > 0 else 0.0
            for f in frames
        ])

    @staticmethod
    def _rms_to_dbfs(rms: float) -> float:
        """Переводит RMS в дБFS (полная шкала)."""
        if rms < 1e-12:
            return -120.0
        return float(20.0 * math.log10(rms / _REF_AMPLITUDE))

    @staticmethod
    def _compute_snr(
        signal_rms: float,
        noise_rms: float,
        audio: np.ndarray,
        sample_rate: int,
    ) -> float:
        """Вычисляет SNR в дБ.

        Алгоритм двухступенчатый:
        1. Если разница между noise_rms и signal_rms значительна (signal > 2× noise),
           используем классический метод — 20*log10(signal/noise).
        2. Иначе (непрерывный сигнал без тихих фреймов) — используем спектральный
           метод: соотношение энергии гармонических пиков к общей мощности спектра.
           Это хорошо работает для речи (тональный сигнал) vs белый шум.
        """
        if signal_rms < 1e-10:
            return 0.0
        if noise_rms < 1e-10:
            return 60.0

        ratio = signal_rms / noise_rms
        if ratio >= 2.0:
            # Классический метод: есть явно тихие фреймы
            snr = 20.0 * math.log10(ratio)
            return float(max(-20.0, min(80.0, snr)))

        # Спектральный метод: оцениваем SNR через FFT.
        # Для тонального сигнала (синусоида/голос) большая часть мощности сосредоточена
        # в узких спектральных пиках. Для шума — мощность распределена равномерно.
        # SNR_spectral ≈ 10*log10(peak_power / mean_floor_power)
        n = len(audio)
        if n < 512:
            return 0.0

        max_samples = min(n, sample_rate * 2)
        segment = audio[:max_samples]
        spectrum = np.abs(np.fft.rfft(segment)) ** 2  # мощность
        if spectrum.sum() < 1e-20:
            return 0.0

        # Сортируем по мощности и разделяем: топ 5% — «сигнал», остальные — «шум»
        sorted_power = np.sort(spectrum)[::-1]
        n_bins = len(sorted_power)
        top_k = max(1, n_bins // 20)  # 5% верхних бинов

        peak_mean = float(np.mean(sorted_power[:top_k]))
        floor_bins = sorted_power[top_k:]
        if len(floor_bins) == 0:
            return 0.0
        floor_mean = float(np.mean(floor_bins))

        if floor_mean < 1e-20:
            return 60.0

        snr = 10.0 * math.log10(peak_mean / floor_mean)
        return float(max(-20.0, min(80.0, snr)))

    def _classify_frequency_profile(self, audio: np.ndarray, sample_rate: int) -> str:
        """Классифицирует спектральный профиль аудио через FFT.

        Returns:
            "low_frequency" | "broadband" | "high_frequency"
        """
        # Берём центральный сегмент (до 2 с) для спектрального анализа
        max_samples = min(len(audio), sample_rate * 2)
        segment = audio[:max_samples]
        if len(segment) < 256:
            return "broadband"

        # FFT
        spectrum = np.abs(np.fft.rfft(segment))
        freqs = np.fft.rfftfreq(len(segment), d=1.0 / sample_rate)

        if len(freqs) == 0 or spectrum.sum() < 1e-10:
            return "broadband"

        # Энергия в трёх диапазонах
        low_mask = freqs <= _LOW_FREQ_MAX
        mid_mask = (freqs > _LOW_FREQ_MAX) & (freqs < _HIGH_FREQ_MIN)
        high_mask = freqs >= _HIGH_FREQ_MIN

        energy_low = float(np.sum(spectrum[low_mask] ** 2)) if low_mask.any() else 0.0
        energy_mid = float(np.sum(spectrum[mid_mask] ** 2)) if mid_mask.any() else 0.0
        energy_high = float(np.sum(spectrum[high_mask] ** 2)) if high_mask.any() else 0.0
        total = energy_low + energy_mid + energy_high

        if total < 1e-20:
            return "broadband"

        ratio_low = energy_low / total
        ratio_high = energy_high / total

        # Классификация по доминирующему диапазону
        if ratio_low > 0.6:
            return "low_frequency"
        if ratio_high > 0.5:
            return "high_frequency"
        return "broadband"

    @staticmethod
    def _classify_noise_type(
        noise_level_db: float,
        freq_profile: str,
        snr_db: float,
    ) -> str:
        """Классифицирует тип шума по уровню и спектральному профилю.

        Эвристические правила:
        - quiet: очень низкий noise floor
        - office: умеренный широкополосный шум (вентиляторы, клавиатура)
        - street: высокий шум с низкочастотными компонентами (трафик)
        - music: высокий шум с широкополосным ритмическим характером
        - crowd: высокий шум без выраженной частотной структуры (голоса)
        """
        if noise_level_db < _LEVEL_QUIET:
            return "quiet"

        if noise_level_db < _LEVEL_OFFICE:
            # Умеренный шум → скорее всего офис
            return "office"

        if noise_level_db < _LEVEL_STREET:
            # Повышенный шум
            if freq_profile == "low_frequency":
                return "street"  # транспортный шум
            return "office"  # офисный шум

        # Высокий уровень шума
        if freq_profile == "low_frequency":
            return "street"
        if freq_profile == "broadband":
            # Различаем музыку от толпы по SNR:
            # При музыке SNR может быть выше (периодический сигнал), при толпе — ниже
            if snr_db > 10:
                return "music"
            return "crowd"
        # high_frequency при высоком уровне → crowd (шипение, перекрикивание)
        return "crowd"

    @staticmethod
    def _generate_recommendations(
        noise_type: str,
        noise_level_db: float,
        snr_db: float,
        freq_profile: str,
        suitable_for_stt: bool,
    ) -> list[str]:
        """Формирует список рекомендаций на основе результатов анализа."""
        recs: list[str] = []

        if not suitable_for_stt:
            recs.append(
                f"SNR {snr_db:.1f} dB ниже рекомендуемого порога {_SNR_STT_THRESHOLD} dB — "
                "точность транскрипции может быть снижена"
            )

        if noise_type == "quiet":
            recs.append("Отличные условия для записи — шум минимален")

        elif noise_type == "office":
            recs.append("Умеренный офисный шум — рекомендуется направленный микрофон")
            if snr_db < 20:
                recs.append("Попробуйте использовать шумоподавление или гарнитуру")

        elif noise_type == "street":
            recs.append("Обнаружен уличный/транспортный шум — рекомендуется перейти в более тихое место")
            recs.append("Используйте гарнитуру с шумоподавлением для лучшего результата")
            if freq_profile == "low_frequency":
                recs.append("Низкочастотный шум (транспорт) — попробуйте high-pass фильтр")

        elif noise_type == "music":
            recs.append("Обнаружена фоновая музыка — может сильно снижать точность STT")
            recs.append("Рекомендуется выключить музыку перед записью")

        elif noise_type == "crowd":
            recs.append("Обнаружен шум толпы или многоголосие — высокий риск ошибок транскрипции")
            recs.append("Рекомендуется использовать направленный микрофон или перейти в тихое место")

        if noise_level_db > -20:
            recs.append(
                f"Очень высокий уровень шума ({noise_level_db:.1f} dBFS) — "
                "рассмотрите использование подавления шума"
            )

        return recs

    def _silent_profile(self) -> NoiseProfile:
        """Возвращает профиль для пустого/слишком короткого аудио."""
        return NoiseProfile(
            noise_type="quiet",
            noise_level_db=-120.0,
            snr_db=0.0,
            frequency_profile="broadband",
            recommendations=["Аудио слишком короткое для надёжного анализа шума"],
            suitable_for_stt=False,
        )
