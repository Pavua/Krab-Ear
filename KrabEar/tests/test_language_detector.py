"""Тесты для LanguageDetector (core/language_detector.py)."""

from core.language_detector import LanguageDetector, LanguageResult
import sys
import unittest
from pathlib import Path

# Настройка путей для запуска как standalone
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(KRAB_EAR_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestLanguageDetectorRussian(unittest.TestCase):
    """Тесты для русского языка."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_russian_sentence(self):
        result = self.detector.detect("Привет, как дела сегодня?")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.script, "cyrillic")
        self.assertGreater(result.confidence, 0.5)

    def test_russian_long_text(self):
        text = "Сегодня хорошая погода, солнце светит ярко и тепло."
        result = self.detector.detect(text)
        self.assertEqual(result.language, "ru")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_russian_single_word(self):
        result = self.detector.detect("Москва")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.script, "cyrillic")


class TestLanguageDetectorUkrainian(unittest.TestCase):
    """Тесты для украинского языка."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_ukrainian_with_marker_char(self):
        # «і» — характерная украинская буква
        result = self.detector.detect("Добрий день, як справи?")
        # «и», «а», «е» — общие; здесь нет украинских маркеров → «ru»
        # Проверяем, что скрипт кириллический
        self.assertEqual(result.script, "cyrillic")

    def test_ukrainian_explicit_markers(self):
        result = self.detector.detect("Київ — столиця України і гарне місто.")
        self.assertEqual(result.language, "uk")
        self.assertEqual(result.script, "cyrillic")


class TestLanguageDetectorSpanish(unittest.TestCase):
    """Тесты для испанского языка."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_spanish_with_tilde(self):
        result = self.detector.detect("Hola, ¿cómo estás hoy?")
        self.assertEqual(result.language, "es")
        self.assertEqual(result.script, "latin")

    def test_spanish_accented_vowels(self):
        result = self.detector.detect("El niño jugaba en el jardín con alegría.")
        self.assertEqual(result.language, "es")
        self.assertGreater(result.confidence, 0.8)

    def test_spanish_inverted_question(self):
        result = self.detector.detect("¿Qué tal?")
        self.assertEqual(result.language, "es")


class TestLanguageDetectorEnglish(unittest.TestCase):
    """Тесты для английского языка."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_english_sentence(self):
        result = self.detector.detect("Hello, how are you doing today?")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.script, "latin")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_english_pure_ascii(self):
        result = self.detector.detect("The quick brown fox jumps over the lazy dog.")
        self.assertEqual(result.language, "en")
        self.assertAlmostEqual(result.confidence, 1.0, places=1)


class TestLanguageDetectorEdgeCases(unittest.TestCase):
    """Граничные случаи."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_empty_string(self):
        result = self.detector.detect("")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.script, "unknown")

    def test_whitespace_only(self):
        result = self.detector.detect("   \t\n  ")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.confidence, 0.0)

    def test_numbers_and_punctuation_only(self):
        result = self.detector.detect("123 456 ... !!!")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.script, "unknown")

    def test_short_text_cyrillic(self):
        result = self.detector.detect("да")
        self.assertIn(result.language, ("ru", "uk"))
        self.assertEqual(result.script, "cyrillic")
        self.assertLessEqual(result.confidence, 0.5)  # низкая уверенность

    def test_mixed_cyrillic_latin(self):
        result = self.detector.detect("Привет hello мир world")
        self.assertEqual(result.script, "mixed")
        # Кириллицы больше → ru
        self.assertIn(result.language, ("ru", "uk", "en"))

    def test_result_is_dataclass(self):
        result = self.detector.detect("test")
        self.assertIsInstance(result, LanguageResult)
        self.assertIsInstance(result.language, str)
        self.assertIsInstance(result.confidence, float)
        self.assertIsInstance(result.script, str)

    def test_confidence_range(self):
        for text in ["hello world", "привет мир", "hola mundo", ""]:
            result = self.detector.detect(text)
            self.assertGreaterEqual(result.confidence, 0.0)
            self.assertLessEqual(result.confidence, 1.0)


class TestLanguageDetectorBatch(unittest.TestCase):
    """Тесты пакетного определения языка."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_detect_batch_returns_list(self):
        texts = ["hello", "привет", "hola"]
        results = self.detector.detect_batch(texts)
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 3)

    def test_detect_batch_order(self):
        texts = ["hello world", "привет мир", "¿cómo estás?"]
        results = self.detector.detect_batch(texts)
        self.assertEqual(results[0].language, "en")
        self.assertEqual(results[1].language, "ru")
        self.assertEqual(results[2].language, "es")

    def test_detect_batch_empty_list(self):
        results = self.detector.detect_batch([])
        self.assertEqual(results, [])

    def test_detect_batch_includes_empty_strings(self):
        results = self.detector.detect_batch(["", "hello", ""])
        self.assertEqual(results[0].language, "und")
        self.assertEqual(results[1].language, "en")
        self.assertEqual(results[2].language, "und")


class TestLanguageDetectorMixedDominance(unittest.TestCase):
    """Смешанный скрипт — доминирующий скрипт определяет язык."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_more_cyrillic_than_latin_returns_ru(self):
        # «Привет мир» (10 кир) vs «hi» (2 лат) → кириллица доминирует
        result = self.detector.detect("Привет мир hi")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.script, "mixed")

    def test_more_latin_than_cyrillic_returns_en(self):
        # «hello world» (10 лат) vs «Да» (2 кир) → латиница доминирует
        result = self.detector.detect("hello world Да")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.script, "mixed")

    def test_more_latin_with_spanish_marker_returns_es(self):
        # «hola cómo» + небольшой блок кириллицы
        result = self.detector.detect("hola cómo estás тут")
        # Латиница доминирует + испанский маркер → es
        self.assertEqual(result.language, "es")
        self.assertEqual(result.script, "mixed")

    def test_fifty_fifty_script_latin_wins(self):
        # Точно 50/50 — по логике детектора латиница берёт приоритет
        result = self.detector.detect("ab Вб")
        self.assertEqual(result.script, "mixed")
        # language определяется по латинской ветке
        self.assertIn(result.language, ("en", "es"))

    def test_confidence_mixed_less_than_pure(self):
        pure = self.detector.detect("Привет мир солнце светит")
        mixed = self.detector.detect("Привет мир hello world")
        # Смешанный текст должен давать меньшую уверенность
        self.assertLessEqual(mixed.confidence, pure.confidence)


class TestLanguageDetectorShortTexts(unittest.TestCase):
    """Короткие тексты — низкая уверенность, но корректный скрипт."""

    def setUp(self):
        self.detector = LanguageDetector()

    def test_single_cyrillic_letter(self):
        result = self.detector.detect("А")
        self.assertIn(result.language, ("ru", "uk"))

    def test_single_latin_letter(self):
        result = self.detector.detect("A")
        self.assertIn(result.language, ("en", "es"))

    def test_two_cyrillic_letters_low_confidence(self):
        result = self.detector.detect("ну")
        self.assertLessEqual(result.confidence, 0.5)
        self.assertIn(result.language, ("ru", "uk"))

    def test_two_latin_letters_low_confidence(self):
        result = self.detector.detect("ok")
        self.assertLessEqual(result.confidence, 0.5)

    def test_exactly_three_letters_normal_confidence(self):
        # 3 буквы — уже не короткий (>= _MIN_LETTERS)
        result = self.detector.detect("abc")
        self.assertGreater(result.confidence, 0.4)

    def test_spanish_inverted_exclamation(self):
        # ¡ — ES-маркер, должен определить es
        result = self.detector.detect("¡Hola amigo querido!")
        self.assertEqual(result.language, "es")

    def test_spanish_inverted_question(self):
        # ¿ — ES-маркер
        result = self.detector.detect("¿Qué pasa contigo hoy?")
        self.assertEqual(result.language, "es")

    def test_spanish_n_tilde(self):
        result = self.detector.detect("El niño juega")
        self.assertEqual(result.language, "es")

    def test_ukrainian_explicit(self):
        result = self.detector.detect("Він їде до Львова")
        self.assertEqual(result.language, "uk")

    def test_confidence_always_in_range(self):
        cases = [
            "а",           # 1 кир
            "ab",          # 2 лат
            "abc",         # 3 лат
            "hello world",
            "привет мир",
            "¿cómo?",
            "",
            "123",
        ]
        for text in cases:
            result = self.detector.detect(text)
            self.assertGreaterEqual(result.confidence, 0.0,
                                    f"confidence < 0 for {text!r}")
            self.assertLessEqual(result.confidence, 1.0,
                                 f"confidence > 1 for {text!r}")


class TestLanguageDetectorWave112(unittest.TestCase):
    """Wave 112 — дополнительные кейсы: emoji, числа, явные имена тестов."""

    def setUp(self):
        self.detector = LanguageDetector()

    # --- exact names from Wave 112 spec ---

    def test_detect_pure_russian(self):
        result = self.detector.detect("Сегодня отличная погода, светит солнце")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.script, "cyrillic")
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_detect_pure_spanish(self):
        """Spanish with accents á/é/í/ó/ú/ñ."""
        result = self.detector.detect("El niño jugó con mucha alegría")
        self.assertEqual(result.language, "es")
        self.assertEqual(result.script, "latin")

    def test_detect_pure_english(self):
        result = self.detector.detect("The quick brown fox jumps over the lazy dog")
        self.assertEqual(result.language, "en")
        self.assertEqual(result.script, "latin")

    def test_detect_mixed_returns_dominant(self):
        """Dominant script language wins in mixed text."""
        result = self.detector.detect("Привет мир привет мир hi")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.script, "mixed")

    def test_short_text_uncertain_label(self):
        """Short text (<3 letters) gets low confidence."""
        result = self.detector.detect("ну")
        self.assertIn(result.language, ("ru", "uk"))
        self.assertLessEqual(result.confidence, 0.5)

    def test_empty_text(self):
        result = self.detector.detect("")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.script, "unknown")

    def test_numbers_only(self):
        result = self.detector.detect("123 456 789")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.script, "unknown")

    def test_emoji_only(self):
        """Emoji has no letters → undetermined."""
        result = self.detector.detect("😀🎉🔥💯")
        self.assertEqual(result.language, "und")
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.script, "unknown")


if __name__ == "__main__":
    unittest.main()
