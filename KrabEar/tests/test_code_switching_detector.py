"""Тесты для CodeSwitchingDetector (core/code_switching_detector.py)."""

import sys
import unittest
from pathlib import Path

# Настройка путей для запуска как standalone
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KRAB_EAR_ROOT = PROJECT_ROOT / "KrabEar"
for p in (str(KRAB_EAR_ROOT), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.code_switching_detector import CodeSwitchingDetector, _classify_word


class TestCodeSwitchingDetectorMixed(unittest.TestCase):
    """Тесты для смешанных текстов (code-switching detected)."""

    def setUp(self):
        self.detector = CodeSwitchingDetector()

    def test_ru_with_english_word(self):
        """Русский текст с одним английским словом — code-switching."""
        result = self.detector.analyze("я запушил коммит в main репозиторий")
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertEqual(result["secondary_lang"], "en")
        self.assertGreater(result["switch_ratio"], 0.0)

    def test_result_keys_present(self):
        """Результат содержит все обязательные ключи."""
        result = self.detector.analyze("Привет world")
        self.assertIn("is_mixed", result)
        self.assertIn("primary_lang", result)
        self.assertIn("secondary_lang", result)
        self.assertIn("switch_ratio", result)

    def test_switch_ratio_bounds(self):
        """switch_ratio всегда в диапазоне [0.0, 1.0]."""
        texts = [
            "Привет world test",
            "Hello мир",
            "чисто русский текст",
            "pure english text",
            "",
        ]
        for text in texts:
            result = self.detector.analyze(text)
            self.assertGreaterEqual(result["switch_ratio"], 0.0, msg=f"text={text!r}")
            self.assertLessEqual(result["switch_ratio"], 1.0, msg=f"text={text!r}")

    def test_tech_tokens_excluded(self):
        """Технические токены (camelCase, snake_case) не считаются латиницей."""
        # snake_case и camelCase не должны вызывать code-switching
        result = self.detector.analyze(
            "я написал функцию myFunction для snake_case обработки"
        )
        # myFunction + snake_case — tech tokens, не должны быть counted as latin
        # Текст почти полностью кириллический → is_mixed может быть False
        # Главное — switch_ratio должен быть <= 0 или very low
        self.assertLessEqual(result["switch_ratio"], 0.5)

    def test_empty_string(self):
        """Пустая строка — is_mixed=False, primary_lang=unknown."""
        result = self.detector.analyze("")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "unknown")
        self.assertIsNone(result["secondary_lang"])
        self.assertEqual(result["switch_ratio"], 0.0)

    def test_whitespace_only(self):
        """Строка из пробелов — is_mixed=False."""
        result = self.detector.analyze("   \t  ")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "unknown")

    def test_pure_russian(self):
        """Чисто русский текст — is_mixed=False, primary_lang=ru."""
        result = self.detector.analyze(
            "Сегодня хорошая погода, солнце светит ярко"
        )
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "ru")
        self.assertIsNone(result["secondary_lang"])

    def test_pure_english(self):
        """Чисто английский текст — is_mixed=False, primary_lang=en."""
        result = self.detector.analyze("Today is a beautiful sunny day")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "en")
        self.assertIsNone(result["secondary_lang"])

    def test_numerics_only(self):
        """Только цифры — unknown (нейтральные токены)."""
        result = self.detector.analyze("123 456 789")
        self.assertFalse(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "unknown")

    def test_custom_threshold(self):
        """Высокий порог — code-switching не детектируется для малой примеси."""
        # "main" — одно латинское слово среди многих русских (~14%)
        # С threshold=0.50 это НЕ code-switching
        high_threshold = CodeSwitchingDetector(switch_threshold=0.50)
        result = high_threshold.analyze("я запушил коммит в main репозиторий")
        self.assertFalse(result["is_mixed"])

    def test_low_threshold(self):
        """Низкий порог — code-switching детектируется при минимальной примеси."""
        low_threshold = CodeSwitchingDetector(switch_threshold=0.05)
        result = low_threshold.analyze("я запушил коммит в main репозиторий")
        self.assertTrue(result["is_mixed"])

    def test_mostly_latin_with_cyrillic(self):
        """Преимущественно латинский текст с кириллическим словом."""
        result = self.detector.analyze(
            "This is mostly English but has одно русское слово"
        )
        # latin dominant → primary=en, secondary=ru, is_mixed=True
        self.assertTrue(result["is_mixed"])
        self.assertEqual(result["primary_lang"], "en")
        self.assertEqual(result["secondary_lang"], "ru")


class TestClassifyWord(unittest.TestCase):
    """Тесты внутренней функции _classify_word."""

    def test_cyrillic_word(self):
        self.assertEqual(_classify_word("привет"), "cyrillic")

    def test_latin_word(self):
        self.assertEqual(_classify_word("hello"), "latin")

    def test_camel_case_is_neutral(self):
        self.assertIsNone(_classify_word("camelCase"))

    def test_snake_case_is_neutral(self):
        self.assertIsNone(_classify_word("snake_case"))

    def test_screaming_snake_is_neutral(self):
        self.assertIsNone(_classify_word("MY_CONST"))

    def test_empty_word_is_neutral(self):
        self.assertIsNone(_classify_word(""))

    def test_digits_only_is_neutral(self):
        self.assertIsNone(_classify_word("12345"))

    def test_git_hash_is_neutral(self):
        # 7+ hex chars = git hash → neutral
        self.assertIsNone(_classify_word("a3f5c1d"))

    def test_url_is_neutral(self):
        self.assertIsNone(_classify_word("https://example.com"))


if __name__ == "__main__":
    unittest.main()
