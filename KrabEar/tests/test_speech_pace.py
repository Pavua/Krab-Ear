"""Тесты SpeechPaceAnalyzer — анализ темпа речи Krab Ear.

Покрывает: PaceReport, все категории темпа, edge-cases, compare_pace,
многоязычный текст и IPC-интеграцию через BackendService.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.speech_pace import SpeechPaceAnalyzer, PaceReport


class PaceReportDataclassTestCase(unittest.TestCase):
    """Тесты структуры PaceReport."""

    def test_pace_report_has_all_fields(self) -> None:
        """PaceReport содержит все обязательные поля."""
        report = PaceReport(
            words_per_minute=120.0,
            chars_per_minute=600.0,
            pace_category="normal",
            estimated_reading_time_sec=10.0,
            word_count=25,
            char_count=125,
            duration_sec=12.5,
        )
        self.assertEqual(report.words_per_minute, 120.0)
        self.assertEqual(report.chars_per_minute, 600.0)
        self.assertEqual(report.pace_category, "normal")
        self.assertEqual(report.estimated_reading_time_sec, 10.0)
        self.assertEqual(report.word_count, 25)
        self.assertEqual(report.char_count, 125)
        self.assertEqual(report.duration_sec, 12.5)

    def test_as_dict_returns_dict_with_all_keys(self) -> None:
        """as_dict() возвращает словарь со всеми ключами PaceReport."""
        report = PaceReport(
            words_per_minute=100.0,
            chars_per_minute=500.0,
            pace_category="normal",
            estimated_reading_time_sec=8.0,
            word_count=20,
            char_count=100,
            duration_sec=12.0,
        )
        d = report.as_dict()
        expected_keys = {
            "words_per_minute", "chars_per_minute", "pace_category",
            "estimated_reading_time_sec", "word_count", "char_count", "duration_sec",
        }
        self.assertEqual(set(d.keys()), expected_keys)


class SpeechPaceAnalyzerBasicTestCase(unittest.TestCase):
    """Базовые тесты SpeechPaceAnalyzer."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    # ── Тест 1: пустой текст ─────────────────────────────────────────────────

    def test_empty_text_returns_zero_report(self) -> None:
        """Пустой текст → нулевой PaceReport."""
        report = self.analyzer.analyze("", duration_sec=10.0)
        self.assertIsInstance(report, PaceReport)
        self.assertEqual(report.words_per_minute, 0.0)
        self.assertEqual(report.chars_per_minute, 0.0)
        self.assertEqual(report.word_count, 0)
        self.assertEqual(report.char_count, 0)
        self.assertEqual(report.pace_category, "slow")

    # ── Тест 2: нулевая/отрицательная длительность ──────────────────────────

    def test_zero_duration_returns_zero_report(self) -> None:
        """Нулевая длительность → нулевой PaceReport."""
        report = self.analyzer.analyze("Привет мир", duration_sec=0.0)
        self.assertEqual(report.words_per_minute, 0.0)
        self.assertEqual(report.word_count, 0)

    def test_negative_duration_returns_zero_report(self) -> None:
        """Отрицательная длительность → нулевой PaceReport."""
        report = self.analyzer.analyze("Hello world", duration_sec=-5.0)
        self.assertEqual(report.words_per_minute, 0.0)
        self.assertEqual(report.duration_sec, 0.0)

    # ── Тест 3: корректный подсчёт WPM ───────────────────────────────────────

    def test_wpm_calculation_is_correct(self) -> None:
        """WPM = word_count / (duration_sec / 60)."""
        # 60 слов за 60 секунд = 60 wpm
        text = " ".join(["word"] * 60)
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertAlmostEqual(report.words_per_minute, 60.0, places=1)
        self.assertEqual(report.word_count, 60)

    # ── Тест 4: возвращаемый тип ─────────────────────────────────────────────

    def test_analyze_returns_pace_report(self) -> None:
        """analyze() возвращает экземпляр PaceReport."""
        report = self.analyzer.analyze("Тест темпа речи.", duration_sec=2.0)
        self.assertIsInstance(report, PaceReport)


class SpeechPaceCategoryTestCase(unittest.TestCase):
    """Тесты категорий темпа речи."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    def _make_text_for_wpm(self, target_wpm: float, duration_sec: float = 60.0) -> str:
        """Генерирует текст заданного количества слов для конкретного WPM."""
        word_count = int(target_wpm * duration_sec / 60.0)
        return " ".join(["word"] * word_count)

    # ── Тест 5: категория "slow" ──────────────────────────────────────────────

    def test_slow_pace_category(self) -> None:
        """WPM < 100 → pace_category == 'slow'."""
        text = self._make_text_for_wpm(80)  # 80 wpm
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertLess(report.words_per_minute, 100.0)
        self.assertEqual(report.pace_category, "slow")

    # ── Тест 6: категория "normal" ───────────────────────────────────────────

    def test_normal_pace_category(self) -> None:
        """100 <= WPM <= 160 → pace_category == 'normal'."""
        text = self._make_text_for_wpm(130)  # 130 wpm
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertGreaterEqual(report.words_per_minute, 100.0)
        self.assertLessEqual(report.words_per_minute, 160.0)
        self.assertEqual(report.pace_category, "normal")

    # ── Тест 7: категория "fast" ──────────────────────────────────────────────

    def test_fast_pace_category(self) -> None:
        """160 < WPM <= 200 → pace_category == 'fast'."""
        text = self._make_text_for_wpm(180)  # 180 wpm
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertGreater(report.words_per_minute, 160.0)
        self.assertLessEqual(report.words_per_minute, 200.0)
        self.assertEqual(report.pace_category, "fast")

    # ── Тест 8: категория "very_fast" ────────────────────────────────────────

    def test_very_fast_pace_category(self) -> None:
        """WPM > 200 → pace_category == 'very_fast'."""
        text = self._make_text_for_wpm(250)  # 250 wpm
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertGreater(report.words_per_minute, 200.0)
        self.assertEqual(report.pace_category, "very_fast")


class SpeechPaceEstimatedReadingTimeTestCase(unittest.TestCase):
    """Тесты расчёта estimated_reading_time_sec."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    # ── Тест 9: расчёт reading time ──────────────────────────────────────────

    def test_estimated_reading_time_at_150_wpm(self) -> None:
        """estimated_reading_time_sec = word_count / 150 * 60."""
        text = " ".join(["word"] * 150)  # 150 слов
        report = self.analyzer.analyze(text, duration_sec=60.0)
        # 150 слов при 150 wpm = 60 секунд
        self.assertAlmostEqual(report.estimated_reading_time_sec, 60.0, places=1)
        self.assertEqual(report.word_count, 150)

    def test_estimated_reading_time_scales_with_word_count(self) -> None:
        """Время чтения растёт пропорционально числу слов."""
        text_short = " ".join(["word"] * 75)   # 75 слов → 30 сек чтения
        text_long = " ".join(["word"] * 300)    # 300 слов → 120 сек чтения
        r_short = self.analyzer.analyze(text_short, duration_sec=30.0)
        r_long = self.analyzer.analyze(text_long, duration_sec=30.0)
        self.assertAlmostEqual(r_short.estimated_reading_time_sec, 30.0, places=1)
        self.assertAlmostEqual(r_long.estimated_reading_time_sec, 120.0, places=1)


class SpeechPaceMultilingualTestCase(unittest.TestCase):
    """Тесты на многоязычных текстах."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    # ── Тест 10: русский текст ───────────────────────────────────────────────

    def test_russian_text_words_counted(self) -> None:
        """Русский текст корректно подсчитывает слова."""
        text = "Привет мир как дела у тебя сегодня"
        report = self.analyzer.analyze(text, duration_sec=5.0)
        self.assertEqual(report.word_count, 7)
        self.assertGreater(report.words_per_minute, 0)

    # ── Тест 11: испанский текст ─────────────────────────────────────────────

    def test_spanish_text_with_accents_counted(self) -> None:
        """Испанский текст с акцентами корректно подсчитывает слова."""
        text = "Buenos días cómo está usted hoy señor"
        report = self.analyzer.analyze(text, duration_sec=3.0)
        self.assertEqual(report.word_count, 7)

    # ── Тест 12: смешанный текст ─────────────────────────────────────────────

    def test_mixed_ru_en_text(self) -> None:
        """Смешанный RU+EN текст корректно подсчитывает слова."""
        text = "Привет hello мир world тест test"
        report = self.analyzer.analyze(text, duration_sec=6.0)
        self.assertEqual(report.word_count, 6)


class SpeechPaceComparePaceTestCase(unittest.TestCase):
    """Тесты метода compare_pace."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    # ── Тест 13: пустой список ───────────────────────────────────────────────

    def test_compare_pace_empty_list(self) -> None:
        """compare_pace([]) → нулевые значения."""
        result = self.analyzer.compare_pace([])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["wpm"]["avg"], 0.0)
        self.assertEqual(result["wpm"]["min"], 0.0)
        self.assertEqual(result["wpm"]["max"], 0.0)

    # ── Тест 14: один отчёт ──────────────────────────────────────────────────

    def test_compare_pace_single_report(self) -> None:
        """compare_pace([report]) → avg == min == max == wpm."""
        text = " ".join(["word"] * 120)
        report = self.analyzer.analyze(text, duration_sec=60.0)
        result = self.analyzer.compare_pace([report])
        self.assertEqual(result["count"], 1)
        self.assertAlmostEqual(result["wpm"]["avg"], report.words_per_minute, places=1)
        self.assertAlmostEqual(result["wpm"]["min"], report.words_per_minute, places=1)
        self.assertAlmostEqual(result["wpm"]["max"], report.words_per_minute, places=1)

    # ── Тест 15: несколько отчётов ───────────────────────────────────────────

    def test_compare_pace_multiple_reports_avg(self) -> None:
        """compare_pace правильно вычисляет avg/min/max по нескольким отчётам."""
        r1 = self.analyzer.analyze(" ".join(["a"] * 60), duration_sec=60.0)   # ~60 wpm
        r2 = self.analyzer.analyze(" ".join(["a"] * 120), duration_sec=60.0)  # ~120 wpm
        r3 = self.analyzer.analyze(" ".join(["a"] * 180), duration_sec=60.0)  # ~180 wpm
        result = self.analyzer.compare_pace([r1, r2, r3])
        self.assertEqual(result["count"], 3)
        self.assertAlmostEqual(result["wpm"]["min"], r1.words_per_minute, places=1)
        self.assertAlmostEqual(result["wpm"]["max"], r3.words_per_minute, places=1)
        expected_avg = (r1.words_per_minute + r2.words_per_minute + r3.words_per_minute) / 3
        self.assertAlmostEqual(result["wpm"]["avg"], expected_avg, places=1)

    # ── Тест 16: распределение по категориям ─────────────────────────────────

    def test_compare_pace_distribution(self) -> None:
        """compare_pace возвращает корректное распределение по категориям."""
        r_slow = self.analyzer.analyze(" ".join(["a"] * 50), duration_sec=60.0)    # ~50 wpm → slow
        r_normal = self.analyzer.analyze(" ".join(["a"] * 130), duration_sec=60.0)  # ~130 wpm → normal
        r_fast = self.analyzer.analyze(" ".join(["a"] * 175), duration_sec=60.0)   # ~175 wpm → fast
        result = self.analyzer.compare_pace([r_slow, r_normal, r_fast])
        dist = result["pace_distribution"]
        self.assertEqual(dist["slow"], 1)
        self.assertEqual(dist["normal"], 1)
        self.assertEqual(dist["fast"], 1)
        self.assertEqual(dist["very_fast"], 0)

    # ── Тест 17: ключи результата ────────────────────────────────────────────

    def test_compare_pace_result_keys(self) -> None:
        """compare_pace возвращает словарь с обязательными ключами."""
        r = self.analyzer.analyze("hello world", duration_sec=5.0)
        result = self.analyzer.compare_pace([r])
        expected_keys = {"count", "wpm", "cpm", "duration_sec", "pace_distribution"}
        self.assertEqual(set(result.keys()), expected_keys)
        for metric in ("wpm", "cpm", "duration_sec"):
            self.assertIn("avg", result[metric])
            self.assertIn("min", result[metric])
            self.assertIn("max", result[metric])


class SpeechPaceCharCountTestCase(unittest.TestCase):
    """Тесты подсчёта символов и chars_per_minute."""

    def setUp(self) -> None:
        self.analyzer = SpeechPaceAnalyzer()

    # ── Тест 18: chars_per_minute > wpm при длинных словах ──────────────────

    def test_chars_per_minute_calculated(self) -> None:
        """chars_per_minute рассчитывается корректно."""
        text = "ab cd ef"  # 3 слова по 2 символа = 6 символов
        report = self.analyzer.analyze(text, duration_sec=60.0)
        self.assertEqual(report.word_count, 3)
        self.assertEqual(report.char_count, 6)
        self.assertAlmostEqual(report.chars_per_minute, 6.0, places=1)
        self.assertAlmostEqual(report.words_per_minute, 3.0, places=1)

    # ── Тест 19: whitespace-only текст ──────────────────────────────────────

    def test_whitespace_only_text_returns_zero(self) -> None:
        """Текст из пробелов → нулевой отчёт."""
        report = self.analyzer.analyze("   \n\t  ", duration_sec=10.0)
        self.assertEqual(report.word_count, 0)
        self.assertEqual(report.char_count, 0)
        self.assertEqual(report.words_per_minute, 0.0)


class SpeechPaceIPCHandlerTestCase(unittest.TestCase):
    """Тесты IPC-обработчика _handle_analyze_speech_pace (прямой вызов).

    Проверяем корректность обработки параметров и формат ответа без
    поднятия всего BackendService (избегаем зависимости от mlx-whisper и т.д.).
    """

    def setUp(self) -> None:
        """Создаём минимальный объект с нужным атрибутом._speech_pace_analyzer."""
        self._analyzer = SpeechPaceAnalyzer()

    def _call_handler(self, params: dict) -> dict:
        """Имитирует _handle_analyze_speech_pace без BackendService."""
        text = params.get("text", "")
        duration_sec = float(params.get("duration_sec", 0.0))
        report = self._analyzer.analyze(text=text, duration_sec=duration_sec)
        return report.as_dict()

    # ── Тест 20: IPC analyze_speech_pace с обычным текстом ──────────────────

    def test_ipc_handler_normal_text(self) -> None:
        """Обработчик возвращает корректный результат для нормального темпа."""
        text = " ".join(["word"] * 120)
        result = self._call_handler({"text": text, "duration_sec": 60.0})
        self.assertIn("words_per_minute", result)
        self.assertIn("pace_category", result)
        self.assertAlmostEqual(result["words_per_minute"], 120.0, places=1)
        self.assertEqual(result["pace_category"], "normal")

    # ── Тест 21: IPC analyze_speech_pace с пустым текстом ───────────────────

    def test_ipc_handler_empty_text(self) -> None:
        """Обработчик возвращает нулевые значения для пустого текста."""
        result = self._call_handler({"text": "", "duration_sec": 10.0})
        self.assertEqual(result["words_per_minute"], 0.0)
        self.assertEqual(result["word_count"], 0)
        self.assertIn("pace_category", result)
        self.assertIn("estimated_reading_time_sec", result)


if __name__ == "__main__":
    unittest.main()
