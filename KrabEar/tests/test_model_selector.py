"""Тесты для SmartModelSelector (core/model_selector.py).

Покрывает:
- Выбор модели в preview-режиме
- Выбор balanced при явном quality=balanced
- Выбор max для коротких записей quality=max
- Выбор balanced при длинной записи + высокой нагрузке
- Выбор max при quality=max без ограничений
- Fallback-поведение
- get_available_models: структура и кэш
- estimate_latency: корректность формулы
"""

from __future__ import annotations
from core.config import settings
from core.model_selector import (
    SmartModelSelector,
    ModelSelection,
    _PREVIEW_MAX_SEC,
    _SHORT_MAX_SEC,
    _LONG_MIN_SEC,
    _HIGH_LOAD_THRESHOLD,
    _RTF_BALANCED,
    _LATENCY_OVERHEAD_MS,
    _MODELS_CACHE_TTL,
)

import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestSmartModelSelectorPreviewMode(unittest.TestCase):
    """Тест 1: превью-режим всегда выбирает balanced."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_preview_flag_selects_balanced(self) -> None:
        """is_preview=True → balanced независимо от quality."""
        sel = self.selector.select_model(
            duration_sec=20.0, quality="max", is_preview=True
        )
        self.assertIsInstance(sel, ModelSelection)
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")
        self.assertIn("preview", sel.reason.lower())

    def test_very_short_duration_selects_balanced(self) -> None:
        """duration < 3с без is_preview → также balanced (правило 1)."""
        sel = self.selector.select_model(
            duration_sec=_PREVIEW_MAX_SEC - 0.1, quality="max", is_preview=False
        )
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")

    def test_preview_overrides_max_quality(self) -> None:
        """Превью приоритетнее quality=max."""
        sel = self.selector.select_model(
            duration_sec=60.0, quality="max", is_preview=True
        )
        self.assertEqual(sel.quality_tier, "balanced")


class TestSmartModelSelectorBalancedQuality(unittest.TestCase):
    """Тест 2: quality=balanced всегда выбирает balanced."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_balanced_quality_short_recording(self) -> None:
        sel = self.selector.select_model(
            duration_sec=5.0, quality="balanced", is_preview=False
        )
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")

    def test_balanced_quality_long_recording(self) -> None:
        sel = self.selector.select_model(
            duration_sec=120.0, quality="balanced", is_preview=False
        )
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")

    def test_balanced_quality_case_insensitive(self) -> None:
        """quality регистронезависимо."""
        sel = self.selector.select_model(
            duration_sec=30.0, quality="BALANCED", is_preview=False
        )
        self.assertEqual(sel.quality_tier, "balanced")


class TestSmartModelSelectorMaxShort(unittest.TestCase):
    """Тест 3: quality=max + короткая запись → max."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_max_quality_short_recording_selects_max(self) -> None:
        """quality=max + duration < 10с → max model."""
        sel = self.selector.select_model(
            duration_sec=7.0, quality="max", is_preview=False
        )
        self.assertEqual(sel.quality_tier, "max")
        # max_name — первый кандидат из model_max_list
        self.assertEqual(sel.model_name, settings.model_max_list[0])
        self.assertIn("max", sel.reason.lower())

    def test_max_quality_exactly_at_short_threshold(self) -> None:
        """Запись ровно 10с — НЕ считается «короткой» (порог строгий <)."""
        sel = self.selector.select_model(
            duration_sec=_SHORT_MAX_SEC, quality="max", is_preview=False
        )
        # При duration == 10.0 правило 3 не срабатывает (условие duration < 10.0)
        # Должно сработать правило 5 (max без ограничений)
        self.assertEqual(sel.quality_tier, "max")


class TestSmartModelSelectorLongHighLoad(unittest.TestCase):
    """Тест 4: длинная запись + высокая нагрузка → balanced."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_long_recording_high_load_selects_balanced(self) -> None:
        """duration > 60с + system_load >= 0.75 → balanced."""
        sel = self.selector.select_model(
            duration_sec=_LONG_MIN_SEC + 10.0,
            quality="max",
            is_preview=False,
            system_load=_HIGH_LOAD_THRESHOLD,
        )
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")
        self.assertIn("system load", sel.reason.lower())

    def test_long_recording_low_load_selects_max(self) -> None:
        """duration > 60с + низкая нагрузка → max (правило 5)."""
        sel = self.selector.select_model(
            duration_sec=90.0,
            quality="max",
            is_preview=False,
            system_load=0.3,
        )
        self.assertEqual(sel.quality_tier, "max")

    def test_short_recording_high_load_selects_max(self) -> None:
        """Короткая запись + высокая нагрузка → правило 3 (max + short), нагрузка не влияет."""
        sel = self.selector.select_model(
            duration_sec=7.0,
            quality="max",
            is_preview=False,
            system_load=0.95,
        )
        self.assertEqual(sel.quality_tier, "max")


class TestSmartModelSelectorMaxQualityGeneral(unittest.TestCase):
    """Тест 5: quality=max без ограничений → max."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_max_quality_medium_recording(self) -> None:
        """30с, max, нет нагрузки → max."""
        sel = self.selector.select_model(
            duration_sec=30.0, quality="max", is_preview=False, system_load=0.0
        )
        self.assertEqual(sel.quality_tier, "max")
        self.assertEqual(sel.model_name, settings.model_max_list[0])

    def test_max_quality_long_low_load(self) -> None:
        """90с, max, нагрузка 0.5 (< порога) → max."""
        sel = self.selector.select_model(
            duration_sec=90.0, quality="max", is_preview=False, system_load=0.5
        )
        self.assertEqual(sel.quality_tier, "max")


class TestSmartModelSelectorFallback(unittest.TestCase):
    """Тест 6: неизвестный quality → fallback balanced."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_unknown_quality_falls_back_to_balanced(self) -> None:
        sel = self.selector.select_model(
            duration_sec=20.0, quality="super_hd", is_preview=False
        )
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)
        self.assertEqual(sel.quality_tier, "balanced")
        self.assertIn("fallback", sel.reason.lower())

    def test_empty_quality_falls_back_to_balanced(self) -> None:
        sel = self.selector.select_model(
            duration_sec=20.0, quality="", is_preview=False
        )
        self.assertEqual(sel.quality_tier, "balanced")


class TestGetAvailableModels(unittest.TestCase):
    """Тест 7: get_available_models возвращает корректные данные и кэш работает."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_returns_list_with_at_least_one_model(self) -> None:
        models = self.selector.get_available_models()
        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)

    def test_balanced_model_present(self) -> None:
        models = self.selector.get_available_models()
        names = [m["name"] for m in models]
        self.assertIn(settings.MODEL_BALANCED, names)

    def test_model_has_required_fields(self) -> None:
        models = self.selector.get_available_models()
        for m in models:
            self.assertIn("name", m)
            self.assertIn("tier", m)
            self.assertIn("description", m)
            self.assertIn("rtf", m)
            self.assertIn("is_default", m)

    def test_balanced_model_is_default(self) -> None:
        models = self.selector.get_available_models()
        balanced = next((m for m in models if m["name"] == settings.MODEL_BALANCED), None)
        self.assertIsNotNone(balanced)
        self.assertTrue(balanced["is_default"])

    def test_cache_returns_same_object(self) -> None:
        """Повторный вызов возвращает кэшированные данные (тот же список)."""
        models1 = self.selector.get_available_models()
        models2 = self.selector.get_available_models()
        self.assertIs(models1, models2)

    def test_cache_expires(self) -> None:
        """После истечения TTL кэш пересчитывается."""
        models1 = self.selector.get_available_models()
        # Вручную подменяем timestamp кэша на прошлое
        old_ts, old_data = self.selector._models_cache
        self.selector._models_cache = (old_ts - _MODELS_CACHE_TTL - 1.0, old_data)
        models2 = self.selector.get_available_models()
        # Объекты разные (пересчитан), но содержимое то же
        self.assertIsNot(models1, models2)
        self.assertEqual(
            [m["name"] for m in models1],
            [m["name"] for m in models2],
        )


class TestEstimateLatency(unittest.TestCase):
    """Тест 8: estimate_latency вычисляет корректные значения."""

    def setUp(self) -> None:
        self.selector = SmartModelSelector()

    def test_balanced_latency_formula(self) -> None:
        """RTF_BALANCED × dur × 1000 + overhead."""
        dur = 10.0
        expected = dur * _RTF_BALANCED * 1000.0 + _LATENCY_OVERHEAD_MS
        got = self.selector.estimate_latency(settings.MODEL_BALANCED, dur)
        self.assertAlmostEqual(got, expected, places=3)

    def test_max_latency_higher_than_balanced(self) -> None:
        """Для той же длительности max-модель медленнее balanced."""
        dur = 20.0
        lat_balanced = self.selector.estimate_latency(settings.MODEL_BALANCED, dur)
        lat_max = self.selector.estimate_latency(settings.model_max_list[0], dur)
        self.assertGreater(lat_max, lat_balanced)

    def test_zero_duration_returns_overhead_only(self) -> None:
        """Нулевая длительность → только фиксированный overhead."""
        lat = self.selector.estimate_latency(settings.MODEL_BALANCED, 0.0)
        self.assertAlmostEqual(lat, _LATENCY_OVERHEAD_MS, places=3)

    def test_negative_duration_clamped_to_overhead(self) -> None:
        """Отрицательная длительность → overhead (max(0, dur))."""
        lat = self.selector.estimate_latency(settings.MODEL_BALANCED, -5.0)
        self.assertAlmostEqual(lat, _LATENCY_OVERHEAD_MS, places=3)

    def test_unknown_model_uses_max_rtf(self) -> None:
        """Неизвестная модель использует пессимистичный RTF (как max)."""
        dur = 10.0
        lat_unknown = self.selector.estimate_latency("some-unknown-model", dur)
        lat_max = self.selector.estimate_latency(settings.model_max_list[0], dur)
        self.assertAlmostEqual(lat_unknown, lat_max, places=3)

    def test_latency_scales_linearly(self) -> None:
        """Задержка линейно зависит от длительности (без учёта overhead точно)."""
        lat_10 = self.selector.estimate_latency(settings.MODEL_BALANCED, 10.0)
        lat_20 = self.selector.estimate_latency(settings.MODEL_BALANCED, 20.0)
        # lat_20 - lat_10 = RTF × 10 × 1000
        delta = lat_20 - lat_10
        expected_delta = 10.0 * _RTF_BALANCED * 1000.0
        self.assertAlmostEqual(delta, expected_delta, places=3)

    def test_selection_latency_matches_estimate(self) -> None:
        """estimated_latency_ms в ModelSelection совпадает с estimate_latency."""
        dur = 25.0
        sel = self.selector.select_model(
            duration_sec=dur, quality="balanced", is_preview=False
        )
        expected = self.selector.estimate_latency(sel.model_name, dur)
        self.assertAlmostEqual(sel.estimated_latency_ms, expected, places=3)


class TestModelSelectionDataclass(unittest.TestCase):
    """Тест корректности структуры ModelSelection."""

    def test_dataclass_fields(self) -> None:
        sel = ModelSelection(
            model_name="test-model",
            reason="test reason",
            estimated_latency_ms=500.0,
            quality_tier="balanced",
        )
        self.assertEqual(sel.model_name, "test-model")
        self.assertEqual(sel.reason, "test reason")
        self.assertEqual(sel.estimated_latency_ms, 500.0)
        self.assertEqual(sel.quality_tier, "balanced")


class TestConcurrentSelect(unittest.TestCase):
    """test_concurrent_select: select_model безопасен при параллельных вызовах."""

    def test_concurrent_select(self) -> None:
        """Множество потоков одновременно вызывают select_model — нет исключений."""
        selector = SmartModelSelector()
        results: list[ModelSelection] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                quality = "max" if i % 2 == 0 else "balanced"
                sel = selector.select_model(
                    duration_sec=float(10 + i),
                    quality=quality,
                    is_preview=(i % 5 == 0),
                    system_load=0.1 * (i % 10),
                )
                with lock:
                    results.append(sel)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Exceptions in threads: {errors}")
        self.assertEqual(len(results), 20)
        for sel in results:
            self.assertIsInstance(sel, ModelSelection)
            self.assertIn(sel.quality_tier, ("balanced", "max"))


class TestOverrideViaSettings(unittest.TestCase):
    """test_override_via_settings: select_model respects patched settings values."""

    def test_override_model_balanced(self) -> None:
        """Патч settings.MODEL_BALANCED меняет выбранное имя модели."""
        fake_balanced = "whisper-tiny-patched"
        with patch.object(settings, "MODEL_BALANCED", fake_balanced):
            selector = SmartModelSelector()
            sel = selector.select_model(
                duration_sec=5.0, quality="balanced", is_preview=False
            )
        self.assertEqual(sel.model_name, fake_balanced)
        self.assertEqual(sel.quality_tier, "balanced")

    def test_override_model_max_list(self) -> None:
        """Патч MODEL_MAX_CANDIDATES меняет имя max-модели в результате."""
        # model_max_list — это @property, патчим через MODEL_MAX_CANDIDATES
        fake_max = "whisper-large-custom"
        original = settings.MODEL_MAX_CANDIDATES
        try:
            settings.__dict__  # ensure mutable access
            object.__setattr__(settings, "MODEL_MAX_CANDIDATES", fake_max)
            selector = SmartModelSelector()
            sel = selector.select_model(
                duration_sec=7.0, quality="max", is_preview=False
            )
            self.assertEqual(sel.model_name, fake_max)
            self.assertEqual(sel.quality_tier, "max")
        finally:
            object.__setattr__(settings, "MODEL_MAX_CANDIDATES", original)

    def test_unknown_lang_defaults_to_balanced(self) -> None:
        """Нераспознанное значение quality → fallback balanced (имитирует unknown lang)."""
        selector = SmartModelSelector()
        sel = selector.select_model(
            duration_sec=30.0, quality="zh", is_preview=False
        )
        self.assertEqual(sel.quality_tier, "balanced")
        self.assertEqual(sel.model_name, settings.MODEL_BALANCED)


if __name__ == "__main__":
    unittest.main()
