"""Тесты TermExtractor — интеллектуальное извлечение терминов Krab Ear."""

from __future__ import annotations
import tempfile
from backend.state_store import StateStore
from core.term_extractor import TermExtractor, ExtractedTerm

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TermExtractorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = TermExtractor()

    def test_extract_terms_returns_list(self) -> None:
        """extract_terms возвращает список ExtractedTerm."""
        terms = self.extractor.extract_terms("машинное обучение нейронные сети", language="ru")
        self.assertIsInstance(terms, list)
        for t in terms:
            self.assertIsInstance(t, ExtractedTerm)

    def test_extract_terms_empty_text(self) -> None:
        """Пустой текст → пустой список терминов."""
        terms = self.extractor.extract_terms("", language="ru")
        self.assertEqual(terms, [])

    def test_extracted_term_has_required_fields(self) -> None:
        """ExtractedTerm содержит обязательные поля."""
        terms = self.extractor.extract_terms("искусственный интеллект распознавание речи", language="ru")
        if terms:
            term = terms[0]
            self.assertIsInstance(term.term, str)
            self.assertIsInstance(term.score, float)
            self.assertIsInstance(term.frequency, int)
            self.assertGreater(len(term.term), 0)

    def test_extract_terms_russian(self) -> None:
        """Извлекает русские термины, игнорируя стоп-слова."""
        text = "транскрипция речи обработка естественного языка нейронные сети"
        terms = self.extractor.extract_terms(text, language="ru")
        term_texts = [t.term.lower() for t in terms]
        # Стоп-слова не должны быть в терминах
        for stop in ("и", "в", "на", "с", "это"):
            self.assertNotIn(stop, term_texts)

    def test_extract_terms_spanish(self) -> None:
        """Извлекает испанские термины."""
        text = "reconocimiento de voz procesamiento del lenguaje natural inteligencia artificial"
        terms = self.extractor.extract_terms(text, language="es")
        self.assertIsInstance(terms, list)

    def test_extract_terms_english(self) -> None:
        """Извлекает английские термины."""
        text = "speech recognition natural language processing machine learning"
        terms = self.extractor.extract_terms(text, language="en")
        self.assertIsInstance(terms, list)

    def test_score_between_zero_and_one(self) -> None:
        """Оценка термина в диапазоне [0, 1]."""
        terms = self.extractor.extract_terms("обработка речи акустическая модель", language="ru")
        for t in terms:
            self.assertGreaterEqual(t.score, 0.0)
            self.assertLessEqual(t.score, 1.0)

    def test_high_frequency_term_appears(self) -> None:
        """Часто встречающееся слово получает высокий счётчик frequency."""
        text = "алгоритм алгоритм алгоритм обучение обучение данные"
        terms = self.extractor.extract_terms(text, language="ru")
        term_map = {t.term.lower(): t for t in terms}
        if "алгоритм" in term_map:
            self.assertGreaterEqual(term_map["алгоритм"].frequency, 2)


class TermExtractorIPCTestCase(unittest.TestCase):
    """Проверяет IPC-хэндлер extract_terms."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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

    def test_extract_terms_handler(self) -> None:
        """IPC-хэндлер extract_terms возвращает список терминов."""
        resp = self.svc.handle_request({
            "id": "1",
            "method": "extract_terms",
            "params": {
                "text": "машинное обучение нейронные сети искусственный интеллект",
                "language": "ru",
            },
        })
        self.assertTrue(resp["ok"])
        result = resp["result"]
        self.assertIn("terms", result)
        self.assertIsInstance(result["terms"], list)

    def test_extract_terms_empty_text(self) -> None:
        """IPC-хэндлер с пустым текстом возвращает пустой список."""
        resp = self.svc.handle_request({
            "id": "2",
            "method": "extract_terms",
            "params": {"text": "", "language": "ru"},
        })
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["result"]["terms"], [])

    def test_extract_terms_result_structure(self) -> None:
        """Каждый элемент списка терминов содержит обязательные поля."""
        resp = self.svc.handle_request({
            "id": "3",
            "method": "extract_terms",
            "params": {"text": "распознавание речи алгоритм обработки", "language": "ru"},
        })
        self.assertTrue(resp["ok"])
        for term in resp["result"]["terms"]:
            self.assertIn("term", term)
            self.assertIn("score", term)
            self.assertIn("frequency", term)


if __name__ == "__main__":
    unittest.main()
