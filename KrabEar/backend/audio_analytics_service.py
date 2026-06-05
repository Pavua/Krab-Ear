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
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from core.silence_constants import (  # W1333: shared threshold constants  # noqa: F401
    SILENCE_THRESHOLD_DB,
    SILENCE_THRESHOLD_AMP,
)

logger = logging.getLogger("KrabEar.Backend.AudioAnalytics")


# ---------------------------------------------------------------------------
# Path allowlist for audio read handlers (W1736)
# ---------------------------------------------------------------------------
# These handlers are read-only, but soundfile/ffmpeg can still exfiltrate
# content or metadata from sensitive files.  Apply the same allowed-roots
# policy used by RecordingCoreService.handle_transcribe_paths.


def _validate_audio_read_path(p: str, data_dir: Path | None = None) -> Path:
    """Raise ValueError if *p* resolves outside the audio-read allowlist.

    Allowed roots: data_dir (if provided), home, /tmp, tempdir.

    Returns:
        The resolved Path (safe to open without re-deriving — avoids TOCTOU).
    """
    resolved = Path(p).expanduser().resolve()
    allowed: list[Path] = [
        Path.home().resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).resolve(),
    ]
    if data_dir is not None:
        allowed.append(data_dir.resolve())

    if any(resolved == root or resolved.is_relative_to(root) for root in allowed):
        return resolved

    raise ValueError(
        f"audio analytics: путь {resolved!s} находится за пределами разрешённых директорий. "
        f"Разрешённые корни: {[str(r) for r in allowed]}"
    )


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
        settings_get: Any = None,
    ) -> None:
        """
        Args:
            audio_converter:     AudioConverter — метаданные и конвертация.
            quality_trends:      QualityTrendAnalyzer — тренды качества.
            audio_fingerprinter: AudioFingerprinter   — фингерпринт/сравнение.
            word_timing_analyzer: WordTimingAnalyzer  — ритм речи.
            store:               StateStore           — доступ к истории (для trends).
            settings_get:        callable(key, default) → Any — runtime settings lookup
                                 (передаётся как BackendService._get_runtime_setting).
                                 Используется для privacy_mode_enabled guard.
        """
        self._audio_converter = audio_converter
        self._quality_trends = quality_trends
        self._audio_fingerprinter = audio_fingerprinter
        self._word_timing_analyzer = word_timing_analyzer
        self._store = store
        self._settings_get = settings_get or (lambda k, d: d)
        # W1736: resolve data_dir once so _validate_audio_read_path can use it.
        _data_dir = getattr(store, "data_dir", None)
        self._data_dir: Path | None = Path(_data_dir).resolve() if _data_dir else None

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

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")
        _validate_audio_read_path(file_path, self._data_dir)  # W1736

        report = analyze_file(file_path)
        return report.to_dict()

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
        _validate_audio_read_path(file_path, self._data_dir)  # W1736

        # D1 MED: clamp threshold_db to prevent OverflowError in 10**(db/20) inside
        # silence_detector when callers supply extreme values (e.g. threshold_db=99999).
        import math
        raw_tdb = params.get("threshold_db", SILENCE_THRESHOLD_DB)
        threshold_db = float(raw_tdb) if raw_tdb is not None else SILENCE_THRESHOLD_DB
        if not math.isfinite(threshold_db):
            threshold_db = SILENCE_THRESHOLD_DB
        threshold_db = max(-80.0, min(0.0, threshold_db))
        return analyze_silence_file(file_path, threshold_db=threshold_db)

    def handle_analyze_quality_trends(self, params: dict[str, Any]) -> dict[str, Any]:
        """Анализирует тренды качества распознавания за последние N дней.

        Privacy guard (wave-35 C1): когда privacy_mode_enabled=True возвращает
        {'ok': False, 'reason': 'privacy_mode_active'} — тренды раскрывают паттерны
        записей (дни/время активности, качество) без доступа к тексту.
        """
        # wave-35 C1: privacy gate — quality trends reveal recording patterns
        # (active days, confidence levels) derived from history; must be blocked.
        if self._settings_get("privacy_mode_enabled", False):
            return {"ok": False, "reason": "privacy_mode_active"}

        # wave-1770 HIGH: clamp days to prevent OverflowError in timedelta arithmetic.
        # timedelta(days=999_999_999) raises OverflowError; cap at 365.
        days = max(1, min(365, int(params.get("days", 30))))
        try:
            with self._store._lock():
                items = self._store._load_active_items_unlocked()
        except Exception:
            items = []
        report = self._quality_trends.analyze_trends(items, days=days)
        return {
            "daily_confidence": report.daily_confidence,
            "overall_trend": report.overall_trend,
            "trend_slope": report.trend_slope,
            "best_day": report.best_day,
            "worst_day": report.worst_day,
            "confidence_distribution": report.confidence_distribution,
        }

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
        _validate_audio_read_path(path, self._data_dir)  # W1736
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

        file_path = params.get("file_path", "")
        if not file_path:
            raise ValueError("Параметр file_path обязателен")
        _validate_audio_read_path(file_path, self._data_dir)  # W1736

        # W18: clamp num_points to prevent CPU/memory DoS via huge values
        # (e.g. 10_000_000 → ~80 MB + ~9 s blocking the IPC thread).
        # Valid GUI range is 1–2000; defence-in-depth also lives in WaveformGenerator.
        num_points = max(1, min(int(params.get("num_points", 200)), 2000))
        gen = WaveformGenerator()
        wf = gen.generate_from_file(file_path, num_points=num_points)
        return {
            "points": wf.points,
            "duration_sec": wf.duration_sec,
            "sample_rate": wf.sample_rate,
            "peak_amplitude": wf.peak_amplitude,
            "rms_amplitude": wf.rms_amplitude,
        }

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
        resolved = _validate_audio_read_path(file_path, self._data_dir)  # W1736 — returns resolved Path

        import soundfile as sf  # lazy import

        # W1736 Fix 3: read through the SAME resolved path the validator checked
        # (avoid TOCTOU: expanduser() without resolve() could follow a late symlink swap)
        if not resolved.exists():
            raise FileNotFoundError(f"Аудиофайл не найден: {resolved}")

        audio_data, sample_rate = sf.read(str(resolved), dtype="float32", always_2d=False)
        profiler = NoiseProfiler()
        result = profiler.profile(audio_data, sample_rate)
        return result.to_dict()

    def handle_check_audio_duplicate(self, params: dict[str, Any]) -> dict[str, Any]:
        """Проверяет, являются ли два аудио-сигнала дубликатами по фингерпринту.

        Параметры (params):
          - audio1: list[float] — первый аудио-сигнал (PCM float32).
          - audio2: list[float] — второй аудио-сигнал (PCM float32).
          - sample_rate: int — частота дискретизации (по умолчанию 16000).
          - threshold: float — DEPRECATED, игнорируется (W1125/W1063: SHA-256
            Hamming distance бессмысленна; используется точное совпадение).

        Возвращает dict с ключами:
          - fingerprint1: str — SHA-256 фингерпринт первого аудио.
          - fingerprint2: str — SHA-256 фингерпринт второго аудио.
          - is_duplicate: bool — True если фингерпринты идентичны.
          - similarity: float — DEPRECATED backwards-compat поле: 1.0 если
            дубликат, 0.0 иначе. Клиенты должны использовать is_duplicate.
        """
        audio1_raw = params.get("audio1")
        audio2_raw = params.get("audio2")
        if audio1_raw is None or audio2_raw is None:
            raise RuntimeError("audio1 и audio2 обязательны")

        sample_rate = int(params.get("sample_rate", 16000))
        # threshold parameter is ignored post-W1063: exact-match only

        audio1 = np.asarray(audio1_raw, dtype=np.float32)
        audio2 = np.asarray(audio2_raw, dtype=np.float32)

        fp1 = self._audio_fingerprinter.fingerprint(audio1, sample_rate)
        fp2 = self._audio_fingerprinter.fingerprint(audio2, sample_rate)
        # W1125/W1063: use equals() — compare() was returning binary 0.0/1.0
        # after W1078 shim anyway; switch to correct bool API directly.
        is_exact_match = self._audio_fingerprinter.equals(fp1, fp2)

        return {
            "fingerprint1": fp1,
            "fingerprint2": fp2,
            "is_duplicate": is_exact_match,
            # Backwards-compat: keep similarity field but populate from bool
            # so existing clients reading float still get meaningful 0/1 value.
            "similarity": 1.0 if is_exact_match else 0.0,
        }
