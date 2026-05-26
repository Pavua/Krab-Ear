"""AudioAnalyticsService — audio quality, VAD, speech pace, waveform.

Extracted из BackendService Wave 73 (sister refactor to CallSessionService PR #420).
Pattern: thin facade, delegates к existing core analyzers.

Handlers (8):
  - handle_analyze_audio_quality   — pre-flight качество аудиофайла
  - handle_analyze_silence         — участки тишины, speech_ratio
  - handle_analyze_quality_trends  — тренды качества за N дней
  - handle_analyze_word_timing     — ритм речи по Whisper word timestamps
  - handle_get_audio_info          — метаданные аудиофайла (duration, sample_rate, …)
  - handle_get_waveform            — waveform-данные для GUI-визуализации
  - handle_profile_noise           — фоновый шум: тип, уровень, SNR, рекомендации
  - handle_check_audio_duplicate   — фингерпринтинг дубликатов (два PCM-буфера)
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from backend.observability import add_breadcrumb

logger = logging.getLogger("KrabEar.Backend.AudioAnalytics")


class AudioAnalyticsService:
    """IPC-обработчики аудио-аналитики, вынесенные из BackendService."""

    def __init__(
        self,
        *,
        audio_converter: Any,
        quality_trends: Any,
        audio_fingerprinter: Any,
        word_timing_analyzer: Any,
        store: Any,
    ) -> None:
        """
        Args:
            audio_converter:     AudioConverter — метаданные и конвертация.
            quality_trends:      QualityTrendAnalyzer — тренды качества.
            audio_fingerprinter: AudioFingerprinter   — фингерпринт/сравнение.
            word_timing_analyzer: WordTimingAnalyzer  — ритм речи.
            store:               StateStore           — доступ к истории (для trends).
        """
        self._audio_converter = audio_converter
        self._quality_trends = quality_trends
        self._audio_fingerprinter = audio_fingerprinter
        self._word_timing_analyzer = word_timing_analyzer
        self._store = store

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def handle_analyze_audio_quality(self, params: dict[str, Any]) -> dict[str, Any]:
        """Pre-flight анализ качества аудиофайла перед транскрипцией.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)

        Returns:
            Словарь с метриками качества: rms_level, peak_level, snr_estimate_db,
            clipping_ratio, silence_ratio, duration_sec, quality_score, warnings.
        """
        from core.audio_quality import analyze_file

        _t0 = time.monotonic()
        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        try:
            report = analyze_file(file_path)
            result = report.to_dict()
            add_breadcrumb(
                category="audio_analytics",
                message="analyze_audio_quality",
                level="info",
                data={
                    "ok": True,
                    "quality_score": result.get("quality_score"),
                    "warning_count": len(result.get("warnings") or []),
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return result
        except Exception as exc:
            add_breadcrumb(
                category="audio_analytics",
                message="analyze_audio_quality",
                level="error",
                data={
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    def handle_analyze_silence(self, params: dict[str, Any]) -> dict[str, Any]:
        """Обнаруживает участки тишины в аудиофайле.

        Params:
            file_path (str): путь к аудиофайлу.
            threshold_db (float, optional): порог тишины в дБ (по умолчанию -40).

        Returns:
            Словарь с silence_regions, speech_ratio, total_silence_sec, duration_sec.
        """
        from core.silence_detector import analyze_silence_file

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        threshold_db = float(params.get("threshold_db", -40.0))
        return analyze_silence_file(file_path, threshold_db=threshold_db)

    def handle_analyze_quality_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует тренды качества распознавания за последние N дней."""
        _t0 = time.monotonic()
        days = int(params.get("days", 30))
        try:
            try:
                with self._store._lock():
                    items = self._store._load_active_items_unlocked()
            except Exception:
                items = []
            report = self._quality_trends.analyze_trends(items, days=days)
            result = {
                "daily_confidence": report.daily_confidence,
                "overall_trend": report.overall_trend,
                "trend_slope": report.trend_slope,
                "best_day": report.best_day,
                "worst_day": report.worst_day,
                "confidence_distribution": report.confidence_distribution,
            }
            add_breadcrumb(
                category="audio_analytics",
                message="analyze_quality_trends",
                level="info",
                data={
                    "ok": True,
                    "days": days,
                    "item_count": len(items),
                    "overall_trend": report.overall_trend,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return result
        except Exception as exc:
            add_breadcrumb(
                category="audio_analytics",
                message="analyze_quality_trends",
                level="error",
                data={
                    "ok": False,
                    "days": days,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    def handle_analyze_word_timing(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует ритм речи по пословным таймстемпам Whisper.

        Params:
            segments: list[dict] — список сегментов Whisper (с полем 'words' или без).

        Возвращает TimingReport в виде словаря.
        """
        segments = params.get("segments")
        if not isinstance(segments, list):
            raise ValueError("Параметр 'segments' должен быть списком")
        report = self._word_timing_analyzer.analyze(segments)
        return report.as_dict()

    def handle_get_audio_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Возвращает метаданные аудиофайла."""
        path = str(params.get("path", "")).strip()
        if not path:
            raise ValueError("Параметр 'path' обязателен")
        info = self._audio_converter.get_audio_info(path)
        return {
            "duration": info.duration,
            "sample_rate": info.sample_rate,
            "channels": info.channels,
            "format": info.format,
            "size_mb": info.size_mb,
        }

    def handle_get_waveform(self, params: dict[str, Any]) -> dict[str, Any]:
        """Генерирует waveform-данные из аудиофайла для GUI-визуализации.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)
            num_points (int, optional): количество точек waveform (по умолчанию 200).

        Returns:
            Словарь с полями: points, duration_sec, sample_rate, peak_amplitude, rms_amplitude.
        """
        from core.waveform_generator import WaveformGenerator

        _t0 = time.monotonic()
        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        num_points = int(params.get("num_points", 200))
        try:
            gen = WaveformGenerator()
            wf = gen.generate_from_file(file_path, num_points=num_points)
            result = {
                "points": wf.points,
                "duration_sec": wf.duration_sec,
                "sample_rate": wf.sample_rate,
                "peak_amplitude": wf.peak_amplitude,
                "rms_amplitude": wf.rms_amplitude,
            }
            add_breadcrumb(
                category="audio_analytics",
                message="get_waveform",
                level="info",
                data={
                    "ok": True,
                    "num_points": num_points,
                    "duration_sec": wf.duration_sec,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            return result
        except Exception as exc:
            add_breadcrumb(
                category="audio_analytics",
                message="get_waveform",
                level="error",
                data={
                    "ok": False,
                    "num_points": num_points,
                    "error_type": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - _t0) * 1000),
                },
            )
            raise

    def handle_profile_noise(self, params: dict[str, Any]) -> dict[str, Any]:
        """Профилирует фоновый шум в аудиофайле.

        Params:
            file_path (str): путь к аудиофайлу (WAV, FLAC, MP3 и т.д.)

        Returns:
            Словарь с полями: noise_type, noise_level_db, snr_db,
            frequency_profile, recommendations, suitable_for_stt.
        """
        from core.noise_profiler import NoiseProfiler

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")

        import soundfile as sf  # lazy import

        path = Path(file_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Аудиофайл не найден: {path}")

        audio_data, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
        profiler = NoiseProfiler()
        result = profiler.profile(audio_data, sample_rate)
        return result.to_dict()

    def handle_check_audio_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет, являются ли два аудио-сигнала дубликатами по фингерпринту.

        Параметры (params):
          - audio1: list[float] — первый аудио-сигнал (PCM float32).
          - audio2: list[float] — второй аудио-сигнал (PCM float32).
          - sample_rate: int — частота дискретизации (по умолчанию 16000).
          - threshold: float — порог сходства [0..1] (по умолчанию 0.95).

        Возвращает dict с ключами:
          fingerprint1, fingerprint2, similarity, is_duplicate.
        """
        audio1_raw = params.get("audio1")
        audio2_raw = params.get("audio2")
        if audio1_raw is None or audio2_raw is None:
            raise RuntimeError("audio1 и audio2 обязательны")

        sample_rate = int(params.get("sample_rate", 16000))
        threshold = float(params.get("threshold", 0.95))

        audio1 = np.asarray(audio1_raw, dtype=np.float32)
        audio2 = np.asarray(audio2_raw, dtype=np.float32)

        fp1 = self._audio_fingerprinter.fingerprint(audio1, sample_rate)
        fp2 = self._audio_fingerprinter.fingerprint(audio2, sample_rate)
        # W1063: use equals() — SHA-256 Hamming distance is statistically meaningless.
        # similarity is 1.0 for exact match, 0.0 otherwise (binary).
        is_exact_match = self._audio_fingerprinter.equals(fp1, fp2)
        similarity = 1.0 if is_exact_match else 0.0

        return {
            "fingerprint1": fp1,
            "fingerprint2": fp2,
            "similarity": round(similarity, 6),
            "is_duplicate": is_exact_match or threshold <= 0.0,
        }
