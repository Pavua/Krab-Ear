"""Тесты ReadabilityScorer — оценка читабельности транскрибаций Krab Ear."""

from __future__ import annotations

import tempfile
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.readability_scorer import ReadabilityScorer, ReadabilityReport


class ReadabilityScorerBasicTestCase(unittest.TestCase):
    """Базовые тесты ReadabilityScorer."""

    def setUp(self) -> None:
        self.scorer = ReadabilityScorer()

    # ── Тест 1: пустой текст ─────────────────────────────────────────────────

    def test_empty_text_returns_zero_report(self) -> None:
        """Пустой текст → нулевой ReadabilityReport."""
        report = self.scorer.score("")
        self.assertIsInstance(report, ReadabilityReport)
        self.assertEqual(report.word_count, 0)
        self.assertEqual(report.sentence_count, 0)
        self.assertEqual(report.flesch_score, 0.0)
        self.assertEqual(report.longest_sentence, "")
        self.assertEqual(report.shortest_sentence, "")

    # ── Тест 2: whitespace-only текст ───────────────────────────────────────

    def test_whitespace_text_returns_zero_report(self) -> None:
        """Текст из одних пробелов → нулевой отчёт."""
        report = self.scorer.score("   \n\t  ")
        self.assertEqual(report.word_count, 0)
        self.assertEqual(report.sentence_count, 0)

    # ── Тест 3: тип возвращаемого значения ───────────────────────────────────

    def test_score_returns_readability_report(self) -> None:
        """score() возвращает экземпляр ReadabilityReport."""
        report = self.scorer.score("Привет мир.")
        self.assertIsInstance(report, ReadabilityReport)

    # ── Тест 4: все поля заполнены ───────────────────────────────────────────

    def test_report_has_all_fields(self) -> None:
        """ReadabilityReport содержит все обязательные поля."""
        report = self.scorer.score("Это тестовое предложение. Проверяем поля.")
        self.assertIsInstance(report.flesch_score, float)
        self.assertIsInstance(report.avg_sentence_length, float)
        self.assertIsInstance(report.avg_word_length, float)
        self.assertIsInstance(report.vocabulary_level, str)
        self.assertIsInstance(report.sentence_count, int)
        self.assertIsInstance(report.word_count, int)
        self.assertIsInstance(report.longest_sentence, str)
        self.assertIsInstance(report.shortest_sentence, str)

    # ── Тест 5: flesch_score в диапазоне 0–100 ───────────────────────────────

    def test_flesch_score_range(self) -> None:
        """flesch_score всегда в диапазоне [0, 100]."""
        texts = [
            "Привет.",
            "Это очень длинное и сложное предложение с множеством слов и оборотов.",
            "a b c d e f g h i j k l m n o p",
            "Нейронные сети используют многослойное представление признаков.",
        ]
        for text in texts:
            report = self.scorer.score(text)
            self.assertGreaterEqual(report.flesch_score, 0.0, msg=f"text={text!r}")
            self.assertLessEqual(report.flesch_score, 100.0, msg=f"text={text!r}")

    # ── Тест 6: подсчёт слов и предложений ───────────────────────────────────

    def test_word_and_sentence_counts(self) -> None:
        """Корректный подсчёт слов и предложений."""
        text = "Первое предложение. Второе предложение. Третье предложение."
        report = self.scorer.score(text)
        self.assertEqual(report.sentence_count, 3)
        self.assertEqual(report.word_count, 6)

    # ── Тест 7: простой текст → более высокий flesch, чем сложный ────────────

    def test_simple_text_higher_flesch_than_complex(self) -> None:
        """Простой короткий текст получает более высокий Flesch, чем сложный."""
        simple = "Я иду домой. Это мой дом."
        complex_text = (
            "Многоуровневая архитектура распределённых нейронных сетей "
            "обеспечивает эффективное представление семантических признаков "
            "в пространстве высокоразмерных векторных вложений."
        )
        simple_report = self.scorer.score(simple)
        complex_report = self.scorer.score(complex_text)
        self.assertGreater(simple_report.flesch_score, complex_report.flesch_score)

    # ── Тест 8: vocabulary_level принимает только допустимые значения ─────────

    def test_vocabulary_level_valid_values(self) -> None:
        """vocabulary_level принимает только 'simple', 'moderate', 'complex'."""
        texts = [
            "Я иду. Он пришёл.",
            "Это обычный текст средней сложности с нормальными словами.",
            "Нейронные трансформеры экстраполируют семантические репрезентации.",
        ]
        allowed = {"simple", "moderate", "complex"}
        for text in texts:
            report = self.scorer.score(text)
            self.assertIn(report.vocabulary_level, allowed, msg=f"text={text!r}")

    # ── Тест 9: longest/shortest sentence ────────────────────────────────────

    def test_longest_and_shortest_sentences(self) -> None:
        """longest_sentence длиннее shortest_sentence по числу слов."""
        text = "Я. Это длинное предложение с многими словами и оборотами здесь."
        report = self.scorer.score(text)
        longest_words = len(report.longest_sentence.split())
        shortest_words = len(report.shortest_sentence.split())
        self.assertGreaterEqual(longest_words, shortest_words)

    # ── Тест 10: однослово́й текст ────────────────────────────────────────────

    def test_single_word_text(self) -> None:
        """Однословный текст обрабатывается без ошибок."""
        report = self.scorer.score("Привет")
        self.assertEqual(report.word_count, 1)
        self.assertEqual(report.sentence_count, 1)
        self.assertGreaterEqual(report.flesch_score, 0.0)

    # ── Тест 11: средняя длина слова > 0 для не-пустого текста ───────────────

    def test_avg_word_length_positive(self) -> None:
        """Средняя длина слова > 0 для непустого текста."""
        report = self.scorer.score("Привет мир это тест")
        self.assertGreater(report.avg_word_length, 0.0)

    # ── Тест 12: многоязычный текст (RU+ES+EN) ───────────────────────────────

    def test_multilingual_text(self) -> None:
        """Многоязычный текст не вызывает ошибок."""
        text = "Hola mundo. Hello world. Привет мир."
        report = self.scorer.score(text)
        self.assertIsInstance(report, ReadabilityReport)
        self.assertEqual(report.sentence_count, 3)
        self.assertGreater(report.word_count, 0)


class ReadabilityScorerIPCTestCase(unittest.TestCase):
    """Тесты IPC-хэндлера score_readability."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        from backend.state_store import StateStore
        store = StateStore(Path(self.tmp.name) / "data")

        from unittest.mock import MagicMock
        recorder = MagicMock()
        recorder.is_recording = False

        from backend.service import BackendService
        self.svc = BackendService(
            store=store,
            recorder=recorder,
            transcriber=MagicMock(),
            translator=MagicMock(),
        )

    # ── Тест 13: IPC возвращает ok=True ──────────────────────────────────────

    def test_ipc_handler_returns_ok(self) -> None:
        """IPC-хэндлер score_readability возвращает ok=True."""
        resp = self.svc.handle_request({
            "id": "1",
            "method": "score_readability",
            "params": {"text": "Это тестовое предложение. Проверяем читабельность."},
        })
        self.assertTrue(resp["ok"])

    # ── Тест 14: структура ответа ─────────────────────────────────────────────

    def test_ipc_response_structure(self) -> None:
        """Ответ содержит все поля ReadabilityReport."""
        resp = self.svc.handle_request({
            "id": "2",
            "method": "score_readability",
            "params": {"text": "Привет мир. Это простой текст."},
        })
        self.assertTrue(resp["ok"])
        result = resp["result"]
        expected_keys = {
            "flesch_score", "avg_sentence_length", "avg_word_length",
            "vocabulary_level", "sentence_count", "word_count",
            "longest_sentence", "shortest_sentence",
        }
        for key in expected_keys:
            self.assertIn(key, result, msg=f"Missing key: {key}")

    # ── Тест 15: пустой текст через IPC ──────────────────────────────────────

    def test_ipc_empty_text(self) -> None:
        """IPC с пустым текстом возвращает нули."""
        resp = self.svc.handle_request({
            "id": "3",
            "method": "score_readability",
            "params": {"text": ""},
        })
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertEqual(result["word_count"], 0)
        self.assertEqual(result["flesch_score"], 0.0)

    # ── Тест 16: неизвестный метод всё ещё ошибка ────────────────────────────

    def test_unknown_method_returns_error(self) -> None:
        """Другие методы по-прежнему возвращают ошибку для несуществующих имён."""
        resp = self.svc.handle_request({
            "id": "4",
            "method": "nonexistent_readability_method",
            "params": {},
        })
        self.assertFalse(resp["ok"])


if __name__ == "__main__":
    unittest.main()
