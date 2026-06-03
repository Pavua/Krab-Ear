"""Wave-26 регрессии: NaN/Inf не должны утекать в IPC-JSON из модулей
тайминга/темпа/шума.

Класс багов (зеркало wave-25 metadata_enricher): доменный анализ возвращает
dict с float-полями, и хотя бы один путь производит Infinity или NaN
(деление на 0.0, Inf-длительность, all-NaN аудио). Swift сериализует ответ
через ``json.dumps(..., allow_nan=False)``, который падает на не-конечных
значениях. Эти тесты фиксируют, что результат всегда round-trip-безопасен.

Покрывает:
- FIX B1 (HIGH) — word_timing.analyze: fallback-сегмент без isfinite-guard
  (Inf-end → Infinity в avg_word_duration_ms; два Inf-end → CRASH в
  statistics.stdev "inf or nan encountered in data").
- FIX B2 (MED)  — speech_pace.analyze: duration_sec=Inf/NaN проходит `<=0`
  guard (Inf>0; NaN-сравнения False) → Inf/NaN в WPM/CPM/duration_sec и
  ошибочная категория "very_fast".
- FIX B3 (MED)  — noise_profiler.profile: all-NaN аудио → NaN в noise_level_db
  (numpy распространяет NaN), ошибочный noise_type="music"/suitable=True.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.word_timing import WordTimingAnalyzer  # noqa: E402
from core.speech_pace import SpeechPaceAnalyzer  # noqa: E402
from core.noise_profiler import NoiseProfiler, NoiseProfile  # noqa: E402


def _assert_json_safe(test: unittest.TestCase, payload: dict, label: str = "") -> None:
    """Падает, если payload содержит NaN/Inf (как сделал бы Swift json.dumps)."""
    try:
        json.dumps(payload, allow_nan=False)
    except ValueError as exc:  # pragma: no cover - explicit failure path
        test.fail(f"{label}: NaN/Inf утёк в IPC-JSON ({exc}); payload={payload}")


def _assert_all_floats_finite(test: unittest.TestCase, payload: dict, label: str = "") -> None:
    """Проверяет, что все float-значения в payload конечны."""
    for key, value in payload.items():
        if isinstance(value, float):
            test.assertTrue(
                math.isfinite(value),
                f"{label}: поле {key!r} не конечно (={value})",
            )


# ───────────────────────────── B1: word_timing ─────────────────────────────


class WordTimingNanGuardTest(unittest.TestCase):
    """FIX B1 (HIGH) — analyze_word_timing с не-конечными таймстемпами."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_fallback_segment_inf_end_no_infinity(self) -> None:
        """Fallback-сегмент с end=Inf не утекает Infinity в avg_word_duration_ms."""
        result = self.analyzer.analyze([{"start": 0.0, "end": float("inf")}]).as_dict()
        _assert_json_safe(self, result, "fallback inf-end")
        _assert_all_floats_finite(self, result, "fallback inf-end")

    def test_fallback_segment_nan_end_no_nan(self) -> None:
        """Fallback-сегмент с end=NaN не утекает NaN."""
        result = self.analyzer.analyze([{"start": 0.0, "end": float("nan")}]).as_dict()
        _assert_json_safe(self, result, "fallback nan-end")
        _assert_all_floats_finite(self, result, "fallback nan-end")

    def test_two_inf_end_segments_no_crash(self) -> None:
        """Два Inf-end сегмента раньше роняли statistics.stdev — теперь безопасно.

        Это самый опасный путь: до фикса analyze() кидал
        ValueError('inf or nan encountered in data'), обрушивая IPC-хендлер.
        """
        result = self.analyzer.analyze(
            [
                {"start": 0.0, "end": float("inf")},
                {"start": 1.0, "end": float("inf")},
            ]
        ).as_dict()
        _assert_json_safe(self, result, "two inf-end")
        _assert_all_floats_finite(self, result, "two inf-end")

    def test_word_level_inf_still_filtered(self) -> None:
        """Word-level Inf-таймстемп фильтруется (уже было), JSON безопасен."""
        result = self.analyzer.analyze(
            [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "words": [{"word": "a", "start": 0.0, "end": float("inf")}],
                }
            ]
        ).as_dict()
        _assert_json_safe(self, result, "word-level inf")
        _assert_all_floats_finite(self, result, "word-level inf")

    def test_zero_duration_segment_no_infinity(self) -> None:
        """Сегмент нулевой длительности (start==end) не даёт Infinity."""
        result = self.analyzer.analyze([{"start": 5.0, "end": 5.0}]).as_dict()
        _assert_json_safe(self, result, "zero-duration")
        _assert_all_floats_finite(self, result, "zero-duration")

    def test_normal_input_unchanged(self) -> None:
        """Регрессионная защита: нормальный ввод даёт ожидаемые значения."""
        result = self.analyzer.analyze(
            [
                {
                    "start": 0.0,
                    "end": 2.0,
                    "words": [
                        {"word": "Hello", "start": 0.0, "end": 0.4},
                        {"word": "world", "start": 0.5, "end": 0.9},
                    ],
                }
            ]
        ).as_dict()
        _assert_json_safe(self, result, "normal")
        self.assertAlmostEqual(result["avg_word_duration_ms"], 400.0, places=1)
        self.assertAlmostEqual(result["avg_pause_duration_ms"], 100.0, places=1)
        self.assertEqual(result["hesitation_count"], 0)


# ───────────────────────────── B2: speech_pace ─────────────────────────────


class SpeechPaceNanGuardTest(unittest.TestCase):
    """FIX B2 (MED) — analyze с не-конечной длительностью."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    def test_inf_duration_returns_safe_dict(self) -> None:
        """duration_sec=Inf раньше проходил `<=0` guard (Inf>0) → Inf в выводе."""
        result = self.analyzer.analyze("hello world foo", float("inf")).as_dict()
        _assert_json_safe(self, result, "inf duration")
        _assert_all_floats_finite(self, result, "inf duration")
        self.assertEqual(result["duration_sec"], 0.0)
        self.assertEqual(result["words_per_minute"], 0.0)

    def test_nan_duration_returns_safe_dict(self) -> None:
        """duration_sec=NaN проходил `<=0` (NaN-сравнения False) → NaN в WPM/CPM."""
        result = self.analyzer.analyze("hello world", float("nan")).as_dict()
        _assert_json_safe(self, result, "nan duration")
        _assert_all_floats_finite(self, result, "nan duration")
        self.assertEqual(result["duration_sec"], 0.0)

    def test_nan_duration_not_misclassified_very_fast(self) -> None:
        """До фикса NaN-длительность давала pace_category='very_fast' (NaN<th False)."""
        result = self.analyzer.analyze("hello world", float("nan")).as_dict()
        self.assertEqual(result["pace_category"], "slow")

    def test_negative_duration_safe(self) -> None:
        """Отрицательная длительность по-прежнему безопасна."""
        result = self.analyzer.analyze("hello world", -3.0).as_dict()
        _assert_json_safe(self, result, "negative duration")
        _assert_all_floats_finite(self, result, "negative duration")

    def test_normal_duration_unchanged(self) -> None:
        """Регрессия: нормальная длительность даёт корректный WPM."""
        result = self.analyzer.analyze("hello world foo", 3.0).as_dict()
        _assert_json_safe(self, result, "normal 3s")
        self.assertEqual(result["word_count"], 3)
        self.assertAlmostEqual(result["words_per_minute"], 60.0, places=1)
        self.assertEqual(result["duration_sec"], 3.0)


# ──────────────────────────── B3: noise_profiler ───────────────────────────


class NoiseProfilerNanGuardTest(unittest.TestCase):
    """FIX B3 (MED) — profile с NaN/Inf в аудиоданных."""

    SR = 16000

    def setUp(self) -> None:
        self.profiler = NoiseProfiler()

    def test_all_nan_audio_no_nan_in_result(self) -> None:
        """all-NaN массив раньше утекал NaN в noise_level_db."""
        audio = np.full(8192, np.nan, dtype=np.float64)
        result = self.profiler.profile(audio, self.SR).to_dict()
        _assert_json_safe(self, result, "all-NaN audio")
        _assert_all_floats_finite(self, result, "all-NaN audio")

    def test_all_inf_audio_no_inf_in_result(self) -> None:
        """all-Inf массив также не утекает Inf."""
        audio = np.full(8192, np.inf, dtype=np.float64)
        result = self.profiler.profile(audio, self.SR).to_dict()
        _assert_json_safe(self, result, "all-Inf audio")
        _assert_all_floats_finite(self, result, "all-Inf audio")

    def test_mixed_nan_audio_no_nan_in_result(self) -> None:
        """Частично NaN-аудио (реальный + NaN-сегмент) тоже безопасно."""
        rng = np.random.RandomState(0)
        audio = (rng.standard_normal(8192) * 0.1).astype(np.float64)
        audio[100:300] = np.nan
        result = self.profiler.profile(audio, self.SR).to_dict()
        _assert_json_safe(self, result, "mixed-NaN audio")
        _assert_all_floats_finite(self, result, "mixed-NaN audio")

    def test_nan_audio_safe_classification(self) -> None:
        """NaN-аудио → безопасный sentinel: тихо + непригодно для STT.

        До фикса NaN noise_level_db ошибочно классифицировался как 'music'
        с suitable_for_stt=True.
        """
        audio = np.full(8192, np.nan, dtype=np.float64)
        profile = self.profiler.profile(audio, self.SR)
        self.assertEqual(profile.noise_level_db, -120.0)
        self.assertEqual(profile.snr_db, 0.0)
        self.assertFalse(profile.suitable_for_stt)

    def test_to_dict_sanitizes_directly_constructed_nan(self) -> None:
        """to_dict() — финальный барьер: NaN в напрямую собранном профиле чистится."""
        profile = NoiseProfile(
            noise_type="office",
            noise_level_db=float("nan"),
            snr_db=float("inf"),
            frequency_profile="broadband",
            recommendations=[],
            suitable_for_stt=True,
        )
        result = profile.to_dict()
        _assert_json_safe(self, result, "direct-construct NaN")
        _assert_all_floats_finite(self, result, "direct-construct NaN")

    def test_normal_audio_unchanged(self) -> None:
        """Регрессия: нормальный тон даёт конечные, осмысленные значения."""
        t = np.linspace(0, 1, self.SR, endpoint=False)
        tone = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float64)
        result = self.profiler.profile(tone, self.SR).to_dict()
        _assert_json_safe(self, result, "normal tone")
        _assert_all_floats_finite(self, result, "normal tone")
        self.assertIn(result["noise_type"], {"quiet", "office", "street", "music", "crowd"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
