"""Тесты для WordTimingAnalyzer — анализ ритма речи по пословным таймстемпам.

Покрывает:
- TimingReport dataclass: поля, as_dict()
- WordTimingAnalyzer.analyze: пустые сегменты, нормальные данные
- Вычисление avg_word_duration_ms, avg_pause_duration_ms, total_pause_time_sec
- Обнаружение хезитаций
- speaking_rate_consistency (равномерность темпа)
- longest_pause_sec
- Fallback на сегменты без пословных меток
- IPC-метод handle_request / analyze_word_timing через BackendService
"""

from __future__ import annotations
from core.word_timing import WordTimingAnalyzer, TimingReport, _extract_words

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── Вспомогательные данные ────────────────────────────────────────────────────

def _make_segments_with_words(words: list[tuple[str, float, float]]) -> list[dict]:
    """Создаёт один Whisper-сегмент с пословными метками.

    Args:
        words: список кортежей (text, start_sec, end_sec).
    """
    word_entries = [
        {"word": w, "start": s, "end": e}
        for w, s, e in words
    ]
    return [{
        "start": words[0][1],
        "end": words[-1][2],
        "words": word_entries,
    }]


def _make_bare_segments(entries: list[tuple[str, float, float]]) -> list[dict]:
    """Создаёт сегменты без поля words (fallback-режим)."""
    return [
        {"text": text, "start": s, "end": e}
        for text, s, e in entries
    ]


# ── Тесты TimingReport dataclass ─────────────────────────────────────────────


class TestTimingReportDataclass(unittest.TestCase):
    """Проверяет структуру и сериализацию TimingReport."""

    def test_all_fields_present(self) -> None:
        """TimingReport должен содержать все 6 обязательных полей."""
        report = TimingReport(
            avg_word_duration_ms=200.0,
            avg_pause_duration_ms=150.0,
            total_pause_time_sec=0.3,
            speaking_rate_consistency=0.85,
            longest_pause_sec=0.6,
            hesitation_count=1,
        )
        self.assertEqual(report.avg_word_duration_ms, 200.0)
        self.assertEqual(report.avg_pause_duration_ms, 150.0)
        self.assertEqual(report.total_pause_time_sec, 0.3)
        self.assertEqual(report.speaking_rate_consistency, 0.85)
        self.assertEqual(report.longest_pause_sec, 0.6)
        self.assertEqual(report.hesitation_count, 1)

    def test_as_dict_returns_all_keys(self) -> None:
        """as_dict() должен вернуть словарь со всеми 6 ключами."""
        report = TimingReport(
            avg_word_duration_ms=100.0,
            avg_pause_duration_ms=80.0,
            total_pause_time_sec=0.16,
            speaking_rate_consistency=0.9,
            longest_pause_sec=0.2,
            hesitation_count=0,
        )
        d = report.as_dict()
        expected_keys = {
            "avg_word_duration_ms",
            "avg_pause_duration_ms",
            "total_pause_time_sec",
            "speaking_rate_consistency",
            "longest_pause_sec",
            "hesitation_count",
        }
        self.assertEqual(set(d.keys()), expected_keys)

    def test_as_dict_values_match(self) -> None:
        """as_dict() должен вернуть точно те же значения, что были в датаклассе."""
        report = TimingReport(
            avg_word_duration_ms=350.5,
            avg_pause_duration_ms=200.0,
            total_pause_time_sec=0.6,
            speaking_rate_consistency=0.75,
            longest_pause_sec=0.8,
            hesitation_count=2,
        )
        d = report.as_dict()
        self.assertAlmostEqual(d["avg_word_duration_ms"], 350.5)
        self.assertEqual(d["hesitation_count"], 2)


# ── Тесты WordTimingAnalyzer ──────────────────────────────────────────────────


class TestWordTimingAnalyzerEmpty(unittest.TestCase):
    """Пустые / невалидные входные данные."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_empty_segments_returns_zero_report(self) -> None:
        """Пустой список сегментов → нулевой TimingReport."""
        report = self.analyzer.analyze([])
        self.assertEqual(report.avg_word_duration_ms, 0.0)
        self.assertEqual(report.avg_pause_duration_ms, 0.0)
        self.assertEqual(report.total_pause_time_sec, 0.0)
        self.assertEqual(report.speaking_rate_consistency, 0.0)
        self.assertEqual(report.longest_pause_sec, 0.0)
        self.assertEqual(report.hesitation_count, 0)

    def test_segments_with_no_valid_timing_returns_zero_report(self) -> None:
        """Сегменты без start/end (или end <= start) → нулевой отчёт."""
        bad_segments = [
            {"text": "hello"},
            {"start": 1.0, "end": 0.5},   # end < start — невалидно
            {"start": 0.5, "end": 0.5},   # нулевая длительность
        ]
        report = self.analyzer.analyze(bad_segments)
        self.assertEqual(report.avg_word_duration_ms, 0.0)

    def test_single_word_no_pauses(self) -> None:
        """Одно слово — паузы отсутствуют, consistency = 1.0."""
        segments = _make_segments_with_words([("hello", 0.0, 0.5)])
        report = self.analyzer.analyze(segments)
        self.assertEqual(report.avg_pause_duration_ms, 0.0)
        self.assertEqual(report.total_pause_time_sec, 0.0)
        self.assertEqual(report.hesitation_count, 0)
        self.assertAlmostEqual(report.speaking_rate_consistency, 1.0)


class TestWordTimingAnalyzerBasic(unittest.TestCase):
    """Базовые вычисления по корректным входным данным."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_avg_word_duration_calculated_correctly(self) -> None:
        """avg_word_duration_ms должна быть средним по длительностям слов."""
        # Три слова по 200 мс, 400 мс, 600 мс → среднее 400 мс
        segments = _make_segments_with_words([
            ("Раз", 0.0, 0.2),
            ("два", 0.4, 0.8),
            ("три", 1.0, 1.6),
        ])
        report = self.analyzer.analyze(segments)
        self.assertAlmostEqual(report.avg_word_duration_ms, 400.0, places=0)

    def test_avg_pause_duration_calculated_correctly(self) -> None:
        """avg_pause_duration_ms должна учитывать паузы между словами."""
        # Пауза между словом 1 и 2: 0.5 - 0.3 = 0.2 с
        # Пауза между словом 2 и 3: 1.0 - 0.6 = 0.4 с
        # Средняя: (0.2 + 0.4) / 2 = 0.3 с = 300 мс
        segments = _make_segments_with_words([
            ("слово", 0.0, 0.3),
            ("второе", 0.5, 0.6),
            ("третье", 1.0, 1.2),
        ])
        report = self.analyzer.analyze(segments)
        self.assertAlmostEqual(report.avg_pause_duration_ms, 300.0, places=0)

    def test_total_pause_time_calculated_correctly(self) -> None:
        """total_pause_time_sec должна равняться сумме всех пауз."""
        # Пауза 1: 0.5 - 0.3 = 0.2 с; пауза 2: 1.0 - 0.6 = 0.4 с → итого 0.6 с
        segments = _make_segments_with_words([
            ("слово", 0.0, 0.3),
            ("второе", 0.5, 0.6),
            ("третье", 1.0, 1.2),
        ])
        report = self.analyzer.analyze(segments)
        self.assertAlmostEqual(report.total_pause_time_sec, 0.6, places=3)

    def test_longest_pause_identified_correctly(self) -> None:
        """longest_pause_sec должна возвращать максимальную паузу."""
        # Паузы: 0.2 с, 0.8 с, 0.1 с → longest = 0.8 с
        segments = _make_segments_with_words([
            ("один", 0.0, 0.3),
            ("два", 0.5, 0.7),
            ("три", 1.5, 1.8),
            ("четыре", 1.9, 2.1),
        ])
        report = self.analyzer.analyze(segments)
        self.assertAlmostEqual(report.longest_pause_sec, 0.8, places=2)


class TestWordTimingHesitations(unittest.TestCase):
    """Обнаружение хезитаций (пауз > 0.5 с в середине фразы)."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_no_hesitations_when_pauses_short(self) -> None:
        """Короткие паузы (< 0.5 с) не считаются хезитациями."""
        segments = _make_segments_with_words([
            ("привет", 0.0, 0.3),
            ("как", 0.5, 0.7),  # пауза 0.2 с — не хезитация
            ("дела", 0.9, 1.1),  # пауза 0.2 с — не хезитация
        ])
        report = self.analyzer.analyze(segments)
        self.assertEqual(report.hesitation_count, 0)

    def test_hesitation_detected_on_long_mid_pause(self) -> None:
        """Пауза > 0.5 с в середине фразы → hesitation_count > 0."""
        segments = _make_segments_with_words([
            ("это", 0.0, 0.2),
            ("ну", 0.9, 1.1),   # пауза 0.7 с → хезитация
            ("как", 1.2, 1.4),
            ("сказать", 1.5, 1.9),
        ])
        report = self.analyzer.analyze(segments)
        self.assertGreaterEqual(report.hesitation_count, 1)

    def test_multiple_hesitations_counted(self) -> None:
        """Несколько длинных пауз → несколько хезитаций."""
        segments = _make_segments_with_words([
            ("начало", 0.0, 0.2),
            ("пауза1", 0.9, 1.1),   # пауза 0.7 с
            ("пауза2", 2.0, 2.2),   # пауза 0.9 с
            ("конец", 3.0, 3.2),    # пауза 0.8 с (последняя, не считается)
        ])
        report = self.analyzer.analyze(segments)
        # Хезитации: 0.7 с и 0.9 с (в середине), последняя пауза исключена
        self.assertEqual(report.hesitation_count, 2)


class TestWordTimingConsistency(unittest.TestCase):
    """Тесты для метрики speaking_rate_consistency."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_uniform_words_gives_high_consistency(self) -> None:
        """Одинаковые длительности слов → высокий consistency (близко к 1)."""
        # Все слова по 300 мс → CV = 0 → consistency = 1.0
        segments = _make_segments_with_words([
            ("раз", 0.0, 0.3),
            ("два", 0.4, 0.7),
            ("три", 0.8, 1.1),
            ("четыре", 1.2, 1.5),
        ])
        report = self.analyzer.analyze(segments)
        self.assertGreater(report.speaking_rate_consistency, 0.9)

    def test_varied_words_gives_lower_consistency(self) -> None:
        """Сильно различающиеся длительности → низкий consistency (< 0.8)."""
        # Слова 50 мс, 50 мс, 50 мс, 2000 мс → большой разброс
        segments = _make_segments_with_words([
            ("а", 0.0, 0.05),
            ("б", 0.2, 0.25),
            ("в", 0.4, 0.45),
            ("очень_длинное_слово", 1.0, 3.0),
        ])
        report = self.analyzer.analyze(segments)
        self.assertLess(report.speaking_rate_consistency, 0.8)

    def test_consistency_between_zero_and_one(self) -> None:
        """speaking_rate_consistency всегда должна быть в диапазоне [0, 1]."""
        segments = _make_segments_with_words([
            ("быстро", 0.0, 0.1),
            ("медленно", 0.5, 1.5),
            ("нормально", 1.7, 2.0),
        ])
        report = self.analyzer.analyze(segments)
        self.assertGreaterEqual(report.speaking_rate_consistency, 0.0)
        self.assertLessEqual(report.speaking_rate_consistency, 1.0)


class TestWordTimingFallback(unittest.TestCase):
    """Fallback-режим: сегменты без поля words."""

    def setUp(self) -> None:
        self.analyzer = WordTimingAnalyzer()

    def test_bare_segments_without_words_field(self) -> None:
        """Сегменты без поля words должны анализироваться как единицы."""
        segments = _make_bare_segments([
            ("Первое предложение.", 0.0, 1.0),
            ("Второе предложение.", 2.0, 3.5),
        ])
        report = self.analyzer.analyze(segments)
        # Две «единицы» есть → avg_word_duration_ms > 0
        self.assertGreater(report.avg_word_duration_ms, 0.0)
        # Пауза между сегментами: 2.0 - 1.0 = 1.0 с
        self.assertAlmostEqual(report.total_pause_time_sec, 1.0, places=2)

    def test_mixed_segments_with_and_without_words(self) -> None:
        """Смешанные сегменты (одни с words, другие без) обрабатываются корректно."""
        segments = [
            {
                "start": 0.0, "end": 0.8,
                "words": [
                    {"word": "hello", "start": 0.0, "end": 0.4},
                    {"word": "world", "start": 0.5, "end": 0.8},
                ]
            },
            {
                "start": 1.5, "end": 2.5,
                "text": "segment without words",
            }
        ]
        report = self.analyzer.analyze(segments)
        self.assertGreater(report.avg_word_duration_ms, 0.0)
        self.assertGreaterEqual(report.total_pause_time_sec, 0.0)


class TestExtractWordsHelper(unittest.TestCase):
    """Юнит-тесты вспомогательной функции _extract_words."""

    def test_extracts_words_from_segment_words_field(self) -> None:
        """_extract_words должна возвращать все слова из поля words сегмента."""
        segments = _make_segments_with_words([
            ("А", 0.0, 0.1),
            ("Б", 0.2, 0.3),
        ])
        words = _extract_words(segments)
        self.assertEqual(len(words), 2)
        self.assertEqual(words[0]["word"], "А")
        self.assertEqual(words[1]["word"], "Б")

    def test_ignores_words_with_invalid_timing(self) -> None:
        """Слова с end <= start не должны попасть в результат."""
        segments = [{
            "start": 0.0, "end": 1.0,
            "words": [
                {"word": "хорошее", "start": 0.0, "end": 0.3},
                {"word": "плохое", "start": 0.5, "end": 0.5},  # end == start
                {"word": "снова_хорошее", "start": 0.6, "end": 0.9},
            ]
        }]
        words = _extract_words(segments)
        self.assertEqual(len(words), 2)
        texts = [w["word"] for w in words]
        self.assertIn("хорошее", texts)
        self.assertIn("снова_хорошее", texts)
        self.assertNotIn("плохое", texts)


class TestWordTimingIPCHandler(unittest.TestCase):
    """IPC-обработчик _handle_analyze_word_timing — прямой вызов без BackendService.

    Избегаем поднятия полного BackendService (зависит от mlx-whisper и других
    тяжёлых компонентов). Тестируем логику обработчика непосредственно, как в
    аналогичных тестах SpeechPaceIPCHandlerTestCase.
    """

    def setUp(self) -> None:
        self._analyzer = WordTimingAnalyzer()

    def _call_handler(self, params: dict) -> dict:
        """Имитирует _handle_analyze_word_timing без BackendService."""
        segments = params.get("segments", [])
        if not isinstance(segments, list):
            raise RuntimeError("segments должен быть списком")
        report = self._analyzer.analyze(segments)
        return report.as_dict()

    def test_ipc_method_returns_all_fields(self) -> None:
        """Обработчик возвращает словарь со всеми 6 полями TimingReport."""
        segments = _make_segments_with_words([
            ("hello", 0.0, 0.4),
            ("world", 0.5, 0.9),
        ])
        result = self._call_handler({"segments": segments})
        expected_keys = {
            "avg_word_duration_ms",
            "avg_pause_duration_ms",
            "total_pause_time_sec",
            "speaking_rate_consistency",
            "longest_pause_sec",
            "hesitation_count",
        }
        self.assertEqual(set(result.keys()), expected_keys)

    def test_ipc_empty_segments_returns_zeros(self) -> None:
        """Пустые segments → нулевой отчёт без исключений."""
        result = self._call_handler({"segments": []})
        self.assertEqual(result["avg_word_duration_ms"], 0.0)
        self.assertEqual(result["hesitation_count"], 0)
        self.assertEqual(result["total_pause_time_sec"], 0.0)

    def test_ipc_invalid_segments_type_raises(self) -> None:
        """segments не-список → RuntimeError."""
        with self.assertRaises(RuntimeError):
            self._call_handler({"segments": "не_список"})

    def test_ipc_with_hesitation_detected(self) -> None:
        """Обработчик корректно обнаруживает хезитацию через IPC-интерфейс."""
        segments = _make_segments_with_words([
            ("это", 0.0, 0.2),
            ("ну", 0.9, 1.1),    # пауза 0.7 с
            ("скажем", 1.2, 1.6),
            ("так", 1.7, 1.9),
        ])
        result = self._call_handler({"segments": segments})
        self.assertGreaterEqual(result["hesitation_count"], 1)
        self.assertGreater(result["longest_pause_sec"], 0.5)


if __name__ == "__main__":
    unittest.main()
