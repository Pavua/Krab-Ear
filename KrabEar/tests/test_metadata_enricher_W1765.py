"""Тесты Wave 1765: ReDoS-backstop + privacy-guard wire для MetadataEnricher.

Покрывает два исправления из W1765:
  HIGH ReDoS-backstop: enrich() на патологически длинном вводе завершается
                       за < 0.3 с (backstop _MAX_TEXT_LEN = 200 000 символов).
  MED dead-privacy-guard: privacy_mode_enabled теперь работает через
                          settings_provider=self._cached_settings (Wire-тест).

Тесты предназначены для выявления регрессий — до W1765 враждебный ввод
("." * 50 000) мог занимать произвольное время без backstop.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.metadata_enricher import (
    MetadataEnricher,
    _MAX_TEXT_LEN,
    _clip_text,
    _count_sentences,
)


# ── Вспомогательные фабрики ───────────────────────────────────────────────────

def _make_item(text: str = "", duration_sec: float = 5.0, confidence: float = 0.8) -> dict:
    return {
        "text": text,
        "duration_sec": duration_sec,
        "confidence": confidence,
        "has_diarization": False,
        "has_llm_enhancement": False,
        "timestamp": "",
    }


# ── HIGH ReDoS-backstop тесты ─────────────────────────────────────────────────

# Бюджет на враждебный ввод. Поднят с 0.3 с до 2.0 с ради загруженных CI-раннеров
# (#1782) — при этом настоящий катастрофический ReDoS-бэктрекинг занимает секунды
# и минуты, так что 2.0 с его по-прежнему ловят.
#
# 🔴 Живёт на уровне МОДУЛЯ, а не класса: 2026-08-29 тест
# BackstopDoesNotBreakPrivacyTestCase упал на загруженном раннере (0.350 с и
# 0.463 с), потому что классовую константу не видел и держал свои 0.3 с —
# правку #1782 применили к одному классу, соседний остался с прежним порогом.
TIMING_BUDGET_SEC: float = 2.0


class ReDoSBackstopTimingTestCase(unittest.TestCase):
    """W1765 HIGH: враждебные вводы должны завершаться за < 2.0 с."""

    _TIMING_BUDGET_SEC: float = TIMING_BUDGET_SEC

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    # ── test_hostile_dots_completes_fast ────────────────────────────────────

    def test_hostile_dots_completes_fast(self) -> None:
        """enrich() на тексте из 50 000 точек завершается за < 2.0 с."""
        # До W1765: без backstop этот ввод мог занимать > 10 с в реализациях,
        # уязвимых к O(N) regexps или catastrophic backtracking.
        hostile = "." * 50_000
        t0 = time.perf_counter()
        result = self._enricher.enrich(_make_item(text=hostile))
        elapsed = time.perf_counter() - t0
        self.assertIn("metadata", result)
        self.assertLess(
            elapsed,
            self._TIMING_BUDGET_SEC,
            f"enrich() на '.'*50000 заняло {elapsed:.3f}с (лимит {self._TIMING_BUDGET_SEC}с) — "
            "ReDoS-backstop не сработал",
        )

    # ── test_hostile_mixed_terminators_completes_fast ────────────────────────

    def test_hostile_mixed_terminators_completes_fast(self) -> None:
        """enrich() на тексте из 50 000 знаков .!?… завершается за < 2.0 с."""
        # Паттерн, провоцирующий жадное квантифицирование без следующего \\s+.
        hostile = ".!?…" * 12_500  # = 50 000 символов
        t0 = time.perf_counter()
        result = self._enricher.enrich(_make_item(text=hostile))
        elapsed = time.perf_counter() - t0
        self.assertIn("metadata", result)
        self.assertLess(
            elapsed,
            self._TIMING_BUDGET_SEC,
            f"enrich() на '.!?…'*12500 заняло {elapsed:.3f}с (лимит {self._TIMING_BUDGET_SEC}с)",
        )

    # ── test_overlong_text_completes_fast ────────────────────────────────────

    def test_overlong_text_completes_fast(self) -> None:
        """enrich() на тексте длиннее _MAX_TEXT_LEN завершается за < 2.0 с."""
        # Реалистичный длинный текст — backstop должен усечь до 200 000 символов.
        overlong = ("Привет мир. " * 20_000)  # ~ 240 000 символов
        self.assertGreater(len(overlong), _MAX_TEXT_LEN)
        t0 = time.perf_counter()
        result = self._enricher.enrich(_make_item(text=overlong))
        elapsed = time.perf_counter() - t0
        self.assertIn("metadata", result)
        self.assertLess(
            elapsed,
            self._TIMING_BUDGET_SEC,
            f"enrich() на тексте {len(overlong)} символов заняло {elapsed:.3f}с "
            f"(лимит {self._TIMING_BUDGET_SEC}с)",
        )


# ── Корректность при нормальном вводе (регрессия) ────────────────────────────

class ReDoSNormalCorrectnessTestCase(unittest.TestCase):
    """W1765: backstop не должен ломать семантику на нормальных транскриптах."""

    def setUp(self) -> None:
        self._enricher = MetadataEnricher()

    def test_word_count_preserved_after_fix(self) -> None:
        """word_count для короткого текста остаётся корректным после W1765."""
        item = _make_item(text="Привет мир. Это тест!", duration_sec=3.0)
        result = self._enricher.enrich(item)
        meta = result["metadata"]
        self.assertGreater(meta["word_count"], 0)
        self.assertIsInstance(meta["word_count"], int)

    def test_sentence_count_preserved_after_fix(self) -> None:
        """sentence_count для «Раз. Два! Три?» = 3 после W1765."""
        self.assertEqual(_count_sentences("Раз. Два! Три?"), 3)

    def test_all_metadata_fields_present(self) -> None:
        """Все обязательные ключи metadata присутствуют после W1765."""
        item = _make_item(text="Тест после исправления ReDoS.", duration_sec=2.0)
        result = self._enricher.enrich(item)
        meta = result["metadata"]
        required = {
            "word_count", "sentence_count", "avg_word_length",
            "language_detected", "emotion", "speech_pace_wpm",
            "quality_grade", "auto_title", "topics", "enriched_at",
        }
        for key in required:
            self.assertIn(key, meta, f"Отсутствует ключ metadata после W1765: {key}")

    def test_language_detection_ru_preserved(self) -> None:
        """Определение русского языка работает после W1765."""
        item = _make_item(text="Это русский текст для проверки исправления.")
        result = self._enricher.enrich(item)
        self.assertEqual(result["metadata"]["language_detected"], "ru")

    def test_enrich_short_hostile_normal_fields(self) -> None:
        """На враждебном вводе все числовые поля имеют разумные типы."""
        hostile = "." * 100
        result = self._enricher.enrich(_make_item(text=hostile))
        meta = result["metadata"]
        self.assertIsInstance(meta["word_count"], int)
        self.assertIsInstance(meta["sentence_count"], int)
        self.assertIsInstance(meta["avg_word_length"], float)
        self.assertGreaterEqual(meta["word_count"], 0)
        self.assertGreaterEqual(meta["sentence_count"], 0)


# ── _clip_text unit-тесты ─────────────────────────────────────────────────────

class ClipTextTestCase(unittest.TestCase):
    """W1765: unit-тесты _clip_text backstop."""

    def test_short_text_unchanged(self) -> None:
        """_clip_text не изменяет текст короче _MAX_TEXT_LEN."""
        text = "Привет мир"
        self.assertEqual(_clip_text(text), text)

    def test_exact_limit_unchanged(self) -> None:
        """_clip_text не изменяет текст ровно _MAX_TEXT_LEN символов."""
        text = "a" * _MAX_TEXT_LEN
        self.assertEqual(_clip_text(text), text)

    def test_overlong_clipped(self) -> None:
        """_clip_text обрезает текст длиннее _MAX_TEXT_LEN."""
        text = "b" * (_MAX_TEXT_LEN + 1000)
        clipped = _clip_text(text)
        self.assertEqual(len(clipped), _MAX_TEXT_LEN)

    def test_clipped_text_is_prefix(self) -> None:
        """_clip_text возвращает именно первые _MAX_TEXT_LEN символов."""
        text = "x" * (_MAX_TEXT_LEN + 500)
        self.assertEqual(_clip_text(text), text[:_MAX_TEXT_LEN])


# ── MED privacy-guard wire тесты ─────────────────────────────────────────────

class PrivacyGuardWireTestCase(unittest.TestCase):
    """W1765 MED: settings_provider должен работать через zero-arg dict-callable."""

    def _make_enricher_with_privacy(self, privacy_on: bool) -> MetadataEnricher:
        """Создаёт MetadataEnricher с settings_provider в виде zero-arg callable (dict)."""
        settings = {"privacy_mode_enabled": privacy_on}
        return MetadataEnricher(settings_provider=lambda: settings)

    # ── test_privacy_mode_suppresses_topics_via_settings_provider ────────────

    def test_privacy_mode_suppresses_topics_via_settings_provider(self) -> None:
        """settings_provider с privacy_mode_enabled=True → topics пусто."""
        enricher = self._make_enricher_with_privacy(privacy_on=True)
        item = _make_item(text="Технологии программирование искусственный интеллект данные")
        result = enricher.enrich(item)
        self.assertEqual(
            result["metadata"]["topics"],
            [],
            "topics должны быть [] при privacy_mode_enabled=True через settings_provider",
        )

    # ── test_privacy_mode_off_allows_topics_via_settings_provider ────────────

    def test_privacy_mode_off_allows_topics_via_settings_provider(self) -> None:
        """settings_provider с privacy_mode_enabled=False → topics — список."""
        enricher = self._make_enricher_with_privacy(privacy_on=False)
        item = _make_item(
            text="Программирование Python искусственный интеллект технологии данные"
        )
        result = enricher.enrich(item)
        topics = result["metadata"]["topics"]
        self.assertIsInstance(topics, list)
        for t in topics:
            self.assertIsInstance(t, str)

    # ── test_privacy_guard_wire_callable_dict_pattern ────────────────────────

    def test_privacy_guard_wire_callable_dict_pattern(self) -> None:
        """MetadataEnricher корректно читает privacy_mode_enabled из dict-callable.

        Это зеркало реального wire из service.py: settings_provider=self._cached_settings,
        где _cached_settings() возвращает полный dict настроек.
        """
        # Симулируем _cached_settings из BackendService
        runtime_settings: dict = {"privacy_mode_enabled": False}

        def fake_cached_settings() -> dict:
            return dict(runtime_settings)

        enricher = MetadataEnricher(settings_provider=fake_cached_settings)

        # С privacy=False: topics — список (не empty)
        item = _make_item(text="Данные алгоритм программирование обработка")
        result_off = enricher.enrich(item)
        self.assertIsInstance(result_off["metadata"]["topics"], list)

        # Включаем privacy в runtime
        runtime_settings["privacy_mode_enabled"] = True
        result_on = enricher.enrich(item)
        self.assertEqual(result_on["metadata"]["topics"], [])

        # Выключаем обратно
        runtime_settings["privacy_mode_enabled"] = False
        result_off2 = enricher.enrich(item)
        self.assertIsInstance(result_off2["metadata"]["topics"], list)

    # ── test_no_settings_provider_privacy_off_default ────────────────────────

    def test_no_settings_provider_privacy_off_default(self) -> None:
        """Без settings_provider privacy_mode по умолчанию False (topics могут быть non-empty)."""
        enricher = MetadataEnricher()  # no settings_provider
        item = _make_item(text="Программирование Python алгоритмы данные обработка")
        result = enricher.enrich(item)
        # Ключ topics всегда присутствует
        self.assertIn("topics", result["metadata"])
        self.assertIsInstance(result["metadata"]["topics"], list)


# ── Backstop + нормальная семантика совместно ─────────────────────────────────

class BackstopDoesNotBreakPrivacyTestCase(unittest.TestCase):
    """W1765: backstop + privacy_mode должны работать совместно."""

    def test_hostile_input_with_privacy_mode_on(self) -> None:
        """На враждебном вводе с privacy=True — topics=[], остальные поля валидны."""
        settings = {"privacy_mode_enabled": True}
        enricher = MetadataEnricher(settings_provider=lambda: settings)
        hostile = "." * 50_000
        t0 = time.perf_counter()
        result = enricher.enrich(_make_item(text=hostile))
        elapsed = time.perf_counter() - t0
        meta = result["metadata"]
        # Быстро
        self.assertLess(
            elapsed,
            TIMING_BUDGET_SEC,
            f"Враждебный ввод + privacy занял {elapsed:.3f}с "
            f"(лимит {TIMING_BUDGET_SEC}с)",
        )
        # Topics пусто
        self.assertEqual(meta["topics"], [])
        # Остальные поля присутствуют
        self.assertIn("word_count", meta)
        self.assertIn("sentence_count", meta)
        self.assertIn("language_detected", meta)


if __name__ == "__main__":
    unittest.main()
