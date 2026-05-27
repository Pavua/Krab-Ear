"""Анализ качества аудио перед транскрипцией (pre-flight check).

AudioQualityAnalyzer оценивает входное аудио по ключевым метрикам:
RMS/peak уровни, SNR, клиппинг, тишина — и возвращает итоговую оценку.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from core.silence_detector import SILENCE_THRESHOLD_AMP

logger = logging.getLogger("KrabEar.AudioQuality")


def _safe_float(value, default=0.0):
    """Coerce value to finite float; return default on NaN/Inf/error.

    Restored by W1522 — W1107 commit silently removed this function along with
    `import math`, fully reverting W1442/W1017/W1103 NaN protection. NaN inputs
    produce invalid JSON that crashes Swift JSONDecoder (RFC 8259 violation).
    """
    try:
        result = float(value)
        if math.isfinite(result):
            return result
    except (TypeError, ValueError):
        pass
    return default

# ---------------------------------------------------------------------------
# Пороговые значения для оценки качества
# ---------------------------------------------------------------------------

_CLIPPING_THRESHOLD = 0.99      # амплитуда ≥ порога считается клиппингом
_SILENCE_FRAME_SIZE = 1024      # семплов в одном фрейме при анализе тишины
# Порог тишины импортирован из core.silence_detector (SILENCE_THRESHOLD_AMP = 0.01,
# соответствует -40 дБ). Ранее было захардкожено 0.001 (~-60 дБ) — исправлено в W885/F8.
_SILENCE_RMS_THRESHOLD = SILENCE_THRESHOLD_AMP
# W1510: порог noise floor для оценки SNR — концептуально отдельная константа от
# _SILENCE_RMS_THRESHOLD. После W1477 _SILENCE_RMS_THRESHOLD сменился с 0.001 → 0.01,
# что сломало SNR-оценку для типичных mic-амплитуд 0.02–0.14 (W1503 R1 HIGH regression):
# выражение `_SILENCE_RMS_THRESHOLD * 10` давало 0.1, помечая ВСЕ фреймы чистого сигнала
# как «noise floor» → SNR=0 dB → score="poor". Декаплинг устраняет coupling двух порогов.
_SNR_NOISE_FLOOR_THRESHOLD = 0.01  # амплитуда ниже которой фреймы считаются noise floor в SNR-оценке
_MIN_DURATION_SEC = 0.5         # минимальная длительность для полноценного анализа


@dataclass
class AudioQualityReport:
    """Результат анализа качества аудио."""

    rms_level: float          # 0-1: среднеквадратичный уровень сигнала
    peak_level: float         # 0-1: пиковая амплитуда
    snr_estimate_db: float    # оценка SNR в дБ
    clipping_ratio: float     # доля семплов в зоне клиппинга (0-1)
    silence_ratio: float      # доля тихих фреймов (0-1)
    duration_sec: float       # длительность записи в секундах
    quality_score: str        # "excellent" | "good" | "fair" | "poor"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rms_level": self.rms_level,
            "peak_level": self.peak_level,
            "snr_estimate_db": self.snr_estimate_db,
            "clipping_ratio": self.clipping_ratio,
            "silence_ratio": self.silence_ratio,
            "duration_sec": self.duration_sec,
            "quality_score": self.quality_score,
            "warnings": self.warnings,
        }


class AudioQualityAnalyzer:
    """Анализатор качества аудио для pre-flight проверки перед STT."""

    def analyze(self, audio_data: np.ndarray, sample_rate: int) -> AudioQualityReport:
        """Анализирует аудиоданные и возвращает отчёт о качестве.

        Args:
            audio_data: numpy-массив float32/float64, нормализованный в [-1, 1].
                        Многоканальные данные усредняются в моно автоматически.
            sample_rate: частота дискретизации в Гц.

        Returns:
            AudioQualityReport с метриками и итоговой оценкой.
        """
        if audio_data.ndim > 1:
            audio_data = audio_data.mean(axis=1)

        audio_data = audio_data.astype(np.float64)
        n_samples = len(audio_data)

        # Wave 64: stt.empty_audio_warning — push when audio frame is empty
        # (n_samples == 0) to surface the numpy RuntimeWarning that would otherwise
        # fire for 'Mean of empty slice' / 'invalid value encountered in divide'.
        if n_samples == 0:
            _error_bus = getattr(self, "_error_bus", None)
            if _error_bus is not None:
                try:
                    from backend.error_bus import KrabError
                    from backend.error_codes import ERROR_REGISTRY
                    from datetime import datetime, timezone
                    _entry = ERROR_REGISTRY.get("stt.empty_audio_warning", {})
                    _err = KrabError(
                        severity=_entry.get("severity", "warn"),
                        component="stt",
                        code="stt.empty_audio_warning",
                        message_user=_entry.get("user_msg_ru", ""),
                        message_debug="audio_quality.analyze: n_samples=0 (empty audio frame)",
                        timestamp=datetime.now(timezone.utc),
                        context={"sample_rate": sample_rate},
                        actionable=False,
                        action_id=None,
                    )
                    _error_bus.push(_err)
                except Exception:
                    pass

        # --- Длительность ---
        duration_sec = n_samples / max(sample_rate, 1)

        # --- RMS и пик ---
        # _safe_float guards against NaN/Inf from corrupt audio (W1017/W1442).
        rms_level = _safe_float(np.sqrt(np.mean(audio_data ** 2))) if n_samples > 0 else 0.0
        peak_level = _safe_float(np.max(np.abs(audio_data))) if n_samples > 0 else 0.0

        # --- Клиппинг ---
        clipping_samples = int(np.sum(np.abs(audio_data) >= _CLIPPING_THRESHOLD))
        clipping_ratio = clipping_samples / max(n_samples, 1)

        # --- Тишина (по фреймам) ---
        silence_ratio = self._compute_silence_ratio(audio_data)

        # --- SNR (оценка) ---
        snr_estimate_db = _safe_float(self._estimate_snr(audio_data, sample_rate))

        # --- Предупреждения ---
        warnings: list[str] = []
        if duration_sec < _MIN_DURATION_SEC:
            warnings.append(f"Очень короткая запись ({duration_sec:.2f}с)")
        if clipping_ratio > 0.01:
            warnings.append(
                f"Обнаружен клиппинг: {clipping_ratio * 100:.1f}% семплов"
            )
        if silence_ratio > 0.8:
            warnings.append(
                f"Высокая доля тишины: {silence_ratio * 100:.0f}% фреймов"
            )
        if rms_level < 0.002:
            warnings.append("Очень низкий уровень сигнала (возможно микрофон выключен)")
        if peak_level > 0.0 and rms_level / peak_level < 0.05:
            warnings.append("Большой dynamic range: возможно наличие щелчков или шума")

        # --- Итоговая оценка ---
        quality_score = self._score(snr_estimate_db, clipping_ratio, silence_ratio, rms_level)

        return AudioQualityReport(
            rms_level=round(rms_level, 6),
            peak_level=round(peak_level, 6),
            snr_estimate_db=round(snr_estimate_db, 2),
            clipping_ratio=round(clipping_ratio, 6),
            silence_ratio=round(silence_ratio, 4),
            duration_sec=round(duration_sec, 4),
            quality_score=quality_score,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _compute_silence_ratio(self, audio: np.ndarray) -> float:
        """Доля фреймов с RMS ниже порога тишины."""
        if len(audio) == 0:
            return 1.0
        n_frames = max(len(audio) // _SILENCE_FRAME_SIZE, 1)
        frames = np.array_split(audio, n_frames)
        silent = sum(
            1 for f in frames
            if len(f) > 0 and np.sqrt(np.mean(f ** 2)) < _SILENCE_RMS_THRESHOLD
        )
        return silent / len(frames)

    def _estimate_snr(self, audio: np.ndarray, sample_rate: int) -> float:
        """Оценка SNR методом статистики по фреймам.

        Разбивает аудио на фреймы и вычисляет RMS каждого.
        Если доля тихих фреймов велика — noise floor берётся из тихих фреймов
        (классический метод). Иначе (сигнал заполняет всё аудио) — оцениваем
        шум через среднеквадратичное отклонение огибающей (envelope variance),
        что соответствует «стационарный сигнал с небольшим наложенным шумом».
        """
        n = len(audio)
        if n < _SILENCE_FRAME_SIZE * 4:
            return 0.0

        n_frames = max(n // _SILENCE_FRAME_SIZE, 4)
        frames = np.array_split(audio, n_frames)
        frame_rms = np.array([
            np.sqrt(np.mean(f ** 2)) for f in frames if len(f) > 0
        ])

        if len(frame_rms) == 0:
            return 0.0

        signal_rms = float(np.sqrt(np.mean(audio ** 2)))
        if signal_rms < 1e-10:
            return 0.0

        # Если есть тихие фреймы — берём их как noise floor.
        # W1510: используем _SNR_NOISE_FLOOR_THRESHOLD вместо _SILENCE_RMS_THRESHOLD * 10,
        # чтобы decoupled константы — после W1477 (silence threshold 0.001→0.01) множитель
        # давал 0.1, что помечало ALL фреймы чистого сигнала 0.02–0.14 как noise floor.
        quiet_mask = frame_rms < _SNR_NOISE_FLOOR_THRESHOLD
        if np.sum(quiet_mask) >= 2:
            noise_rms = float(np.mean(frame_rms[quiet_mask]))
            if noise_rms < 1e-10:
                return 60.0
            snr = 20.0 * np.log10(signal_rms / noise_rms)
            return float(np.clip(snr, -20.0, 80.0))

        # Нет тихих фреймов: оцениваем шум через вариацию огибающей.
        # Для чистого сигнала CV мал → большой SNR; для зашумлённого CV велик.
        mean_rms = float(np.mean(frame_rms))
        std_rms = float(np.std(frame_rms))
        cv = std_rms / mean_rms if mean_rms > 1e-10 else 1.0

        # Эмпирическая формула: SNR ≈ -20*log10(cv) (обратная зависимость)
        # cv=0.01 → SNR≈40 dB, cv=0.1 → SNR≈20 dB, cv=1.0 → SNR≈0 dB
        cv_clamped = max(cv, 1e-4)
        snr = -20.0 * np.log10(cv_clamped)
        return float(np.clip(snr, -20.0, 80.0))

    def _score(
        self,
        snr_db: float,
        clipping_ratio: float,
        silence_ratio: float,
        rms_level: float,
    ) -> str:
        """Итоговая оценка качества по ключевым метрикам."""
        # Мгновенная деградация при клиппинге
        if clipping_ratio > 0.05:
            return "poor"

        # Почти полная тишина
        if silence_ratio > 0.9 or rms_level < 1e-6:
            return "poor"

        if snr_db >= 30 and clipping_ratio < 0.001 and silence_ratio < 0.5:
            return "excellent"
        if snr_db >= 20 and clipping_ratio < 0.01:
            return "good"
        if snr_db >= 10:
            return "fair"
        return "poor"


def analyze_file(path: str | Path, analyzer: Optional[AudioQualityAnalyzer] = None) -> AudioQualityReport:
    """Удобная функция: анализирует аудиофайл по пути.

    Требует установленного пакета soundfile.
    """
    import soundfile as sf

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Аудиофайл не найден: {path}")

    audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    if analyzer is None:
        analyzer = AudioQualityAnalyzer()
    return analyzer.analyze(audio_data, sample_rate)
