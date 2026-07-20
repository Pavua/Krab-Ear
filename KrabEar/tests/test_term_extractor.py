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
        terms = self.extractor.extract_terms(
            "GPT-4 and OpenAI released ChatGPT model.", language="en"
        )
        if terms:
            term = terms[0]
            self.assertIsInstance(term.term, str)
            self.assertIsInstance(term.confidence, float)
            self.assertIsInstance(term.frequency, int)
            self.assertIsInstance(term.is_proper_noun, bool)
            self.assertIsInstance(term.context, str)
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

    def test_confidence_between_zero_and_one(self) -> None:
        """confidence термина в диапазоне [0, 1]."""
        terms = self.extractor.extract_terms(
            "The API and SDK released by OpenAI have NLP capabilities.", language="en"
        )
        for t in terms:
            self.assertGreaterEqual(t.confidence, 0.0)
            self.assertLessEqual(t.confidence, 1.0)

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
        # Регистрируем после cleanup каталога: unittest выполнит закрытие первым.
        self.addCleanup(self.svc.close)

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


class TermExtractorDirectTestCase(unittest.TestCase):
    """Прямые тесты TermExtractor без IPC — покрывают все пути кода."""

    def setUp(self) -> None:
        self.extractor = TermExtractor()

    def test_camel_case_detected(self) -> None:
        """CamelCase слова извлекаются как технические термины."""
        terms = self.extractor.extract_terms(
            "We use CamelCase naming like SpeechRecognition and NeuralNetwork.",
            language="en",
        )
        term_names = [t.term for t in terms]
        camel = [n for n in term_names if n[0].isupper() and len(n) > 4]
        self.assertGreater(len(camel), 0, "CamelCase термины должны быть извлечены")

    def test_abbreviation_detected(self) -> None:
        """Аббревиатуры (2+ заглавных) извлекаются."""
        terms = self.extractor.extract_terms(
            "The NLP model uses BERT for sentence embeddings. API and SDK available.",
            language="en",
        )
        term_names = [t.term for t in terms]
        self.assertTrue(
            any(n.isupper() for n in term_names),
            "Аббревиатуры должны быть обнаружены",
        )

    def test_tech_with_digits_detected(self) -> None:
        """Технические термины с цифрами (GPT-4, iPhone13) извлекаются."""
        terms = self.extractor.extract_terms(
            "GPT4 model and Python3 language are widely used.", language="en"
        )
        term_names = [t.term.lower() for t in terms]
        has_digit_term = any(any(c.isdigit() for c in n) for n in term_names)
        self.assertTrue(has_digit_term, "Термины с цифрами должны быть извлечены")

    def test_is_proper_noun_flag_camelcase_false(self) -> None:
        """CamelCase термины НЕ помечаются как proper noun (is_proper_noun=False)."""
        terms = self.extractor.extract_terms(
            "The SpeechRecognition system uses MachineLearning algorithms.", language="en"
        )
        camel_terms = [t for t in terms if len(t.term) > 4]
        self.assertGreater(len(camel_terms), 0, "Должны быть извлечены CamelCase термины")
        for t in camel_terms:
            self.assertFalse(
                t.is_proper_noun, f"CamelCase '{t.term}' не должен быть proper noun"
            )

    def test_context_snippet_provided(self) -> None:
        """Поле context содержит непустой фрагмент текста."""
        terms = self.extractor.extract_terms(
            "The NLP toolkit processes RNN layers efficiently.", language="en"
        )
        for t in terms:
            self.assertIsInstance(t.context, str)
            self.assertGreater(len(t.context), 0, "context не должен быть пустым")

    def test_repeated_bigram_extracted(self) -> None:
        """Биграмм, встречающийся 2+ раз, попадает в результат."""
        # Повторяем одну и ту же пару значимых слов
        terms = self.extractor.extract_terms(
            "neural network performs well. neural network beats baselines. "
            "neural network scales fast.",
            language="en",
        )
        term_texts = [t.term.lower() for t in terms]
        self.assertIn("neural network", term_texts, "Биграмм 'neural network' должен быть извлечён")

    def test_extract_from_history_min_frequency(self) -> None:
        """extract_from_history фильтрует термины по min_frequency."""
        items = [
            {"text": "The NLP model and NLP toolkit work well together."},
            {"text": "NLP processing at scale requires NLP models."},
            {"text": "We use NLP for speech and language tasks."},
        ]
        terms = self.extractor.extract_from_history(items, min_frequency=3)
        # min_frequency=3 должна пропускать только часто встречающиеся
        for t in terms:
            self.assertGreaterEqual(t.frequency, 3)

    def test_extract_from_history_empty_items(self) -> None:
        """extract_from_history пустого списка возвращает []."""
        result = self.extractor.extract_from_history([])
        self.assertEqual(result, [])

    def test_extract_from_history_uses_source_text_field(self) -> None:
        """extract_from_history читает поле source_text если text отсутствует."""
        items = [
            {"source_text": "API SDK NLP used in API SDK processing pipeline."},
            {"source_text": "API SDK again used here for NLP tasks."},
            {"source_text": "Third item with API SDK and NLP calls."},
        ]
        terms = self.extractor.extract_from_history(items, min_frequency=2)
        self.assertIsInstance(terms, list)

    def test_min_term_length_respected(self) -> None:
        """min_term_length отсекает слишком короткие токены."""
        extractor = TermExtractor(min_term_length=5)
        terms = extractor.extract_terms(
            "AI ML DL systems and MachineLearning NeuralNetworks.", language="en"
        )
        for t in terms:
            if not any(c.isupper() for c in t.term[1:]):  # не аббревиатура
                self.assertGreaterEqual(len(t.term), 5)

    def test_whitespace_only_text_returns_empty(self) -> None:
        """Текст только из пробелов → пустой список."""
        terms = self.extractor.extract_terms("   \t\n  ", language="ru")
        self.assertEqual(terms, [])

    def test_sorted_by_confidence_descending(self) -> None:
        """Результат отсортирован по убыванию confidence."""
        terms = self.extractor.extract_terms(
            "The NLP API and SDK use BERT model CamelCase processing.", language="en"
        )
        if len(terms) >= 2:
            for i in range(len(terms) - 1):
                self.assertGreaterEqual(
                    terms[i].confidence, terms[i + 1].confidence,
                    "Термины должны быть отсортированы по убыванию confidence",
                )


if __name__ == "__main__":
    unittest.main()
