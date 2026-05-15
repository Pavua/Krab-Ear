"""test_regex_precompile_perf.py

A2 sanity-check: verify that precompiled regex constants in hot-path modules
produce identical output to the original inline re.X(pattern, ...) calls.

No timing assertions — correctness only (timing is environment-dependent).
"""

import re
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestContextMemoryRegex(unittest.TestCase):
    """core/context_memory.py: _RE_SENT_SPLIT and _RE_WORD_CLEAN."""

    def setUp(self):
        from core.context_memory import _RE_SENT_SPLIT, _RE_WORD_CLEAN
        self._sent_split = _RE_SENT_SPLIT
        self._word_clean = _RE_WORD_CLEAN

    def test_sent_split_matches_inline(self):
        texts = [
            "Привет мир. Как дела?",
            "Hello world! This is a test...",
            "Нет знаков конца",
            "",
        ]
        for text in texts:
            expected = re.split(r"[.!?]+", text)
            result = self._sent_split.split(text)
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")

    def test_word_clean_matches_inline(self):
        words = ["Привет!", "Hello,", "GPT-4", "word.", "СловоWithPunctuation;", "test"]
        for word in words:
            expected = re.sub(r"[^\wА-Яа-яÁÉÍÓÚáéíóúÑñ-]", "", word, flags=re.UNICODE)
            result = self._word_clean.sub("", word)
            self.assertEqual(result, expected, msg=f"Mismatch for: {word!r}")


class TestTermExtractorRegex(unittest.TestCase):
    """core/term_extractor.py: _RE_SENT_SPLIT and _RE_WORD_CLEAN."""

    def setUp(self):
        from core.term_extractor import _RE_SENT_SPLIT, _RE_WORD_CLEAN
        self._sent_split = _RE_SENT_SPLIT
        self._word_clean = _RE_WORD_CLEAN

    def test_sentences_matches_inline(self):
        texts = [
            "Первое предложение. Второе предложение! Третье?",
            "No punctuation here",
            "A. B. C.",
        ]
        for text in texts:
            expected = re.split(r"(?<=[.!?])\s+", text.strip())
            result = self._sent_split.split(text.strip())
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")

    def test_word_clean_matches_inline(self):
        words = ["Привет!", "Hello,", "word.", "Слово,", "test"]
        for word in words:
            expected = re.sub(r"[^\wА-Яа-я]", "", word, flags=re.UNICODE)
            result = self._word_clean.sub("", word)
            self.assertEqual(result, expected, msg=f"Mismatch for: {word!r}")


class TestSearchIndexRegex(unittest.TestCase):
    """core/search_index.py: _RE_TOKEN."""

    def setUp(self):
        from core.search_index import _RE_TOKEN
        self._token = _RE_TOKEN

    def test_tokenize_matches_inline(self):
        texts = [
            "Привет мир hello world 123",
            "",
            "GPT-4 это модель",
            "café résumé naïve",  # non-ASCII — should not match
        ]
        for text in texts:
            lowered = text.lower()
            expected = re.findall(r"[а-яёa-z0-9]+", lowered)
            result = self._token.findall(lowered)
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")


class TestPasteFormatterRegex(unittest.TestCase):
    """core/paste_formatter.py: _RE_SENT_SPLIT."""

    def setUp(self):
        from core.paste_formatter import _RE_SENT_SPLIT
        self._sent_split = _RE_SENT_SPLIT

    def test_split_matches_inline(self):
        texts = [
            "Hello world. This is good! Is it?",
            "Одно предложение",
            "",
        ]
        for text in texts:
            expected = re.split(r"(?<=[.!?])\s+", text)
            result = self._sent_split.split(text)
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")


class TestNormalizationProfilesRegex(unittest.TestCase):
    """core/normalization_profiles.py: _RE_NORMALIZE_WS, _RE_CAPITALIZE_SENT, _RE_STRIP_TRAILING_PERIOD."""

    def setUp(self):
        from core.normalization_profiles import (
            _RE_NORMALIZE_WS,
            _RE_CAPITALIZE_SENT,
            _RE_STRIP_TRAILING_PERIOD,
        )
        self._ws = _RE_NORMALIZE_WS
        self._cap = _RE_CAPITALIZE_SENT
        self._period = _RE_STRIP_TRAILING_PERIOD

    def test_normalize_ws_matches_inline(self):
        texts = ["hello   world", "  spaces  ", "no  extra", "clean"]
        for text in texts:
            expected = re.sub(r"\s+", " ", text).strip()
            result = self._ws.sub(" ", text).strip()
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")

    def test_strip_trailing_period_matches_inline(self):
        texts = ["hello.", "hello...", "hello", "hello!"]
        for text in texts:
            expected = re.sub(r"[.]+$", "", text.rstrip())
            result = self._period.sub("", text.rstrip())
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")

    def test_capitalize_sent_matches_inline(self):
        # Pattern is used with a callback — verify same matches found
        texts = [
            "первое слово. второе слово",
            "hello world! another sentence",
        ]
        for text in texts:
            expected_spans = [m.span() for m in re.finditer(r"(?:^|(?<=[.!?…])\s+)([а-яa-z])", text)]
            result_spans = [m.span() for m in self._cap.finditer(text)]
            self.assertEqual(result_spans, expected_spans, msg=f"Mismatch for: {text!r}")


class TestEmotionDetectorRegex(unittest.TestCase):
    """core/emotion_detector.py: _RE_WORD_TOKENS."""

    def setUp(self):
        from core.emotion_detector import _RE_WORD_TOKENS
        self._tokens = _RE_WORD_TOKENS

    def test_findall_matches_inline(self):
        texts = [
            "Привет мир Hello World",
            "числа 123 не в счёт",
            "",
            "café résumé naïve",
        ]
        for text in texts:
            expected = re.findall(r"[А-Яа-яёЁA-Za-zÀ-ÿ]+", text)
            result = self._tokens.findall(text)
            self.assertEqual(result, expected, msg=f"Mismatch for: {text!r}")


class TestReadabilityScorerRegex(unittest.TestCase):
    """core/readability_scorer.py: _RE_HYPHEN_APOS."""

    def setUp(self):
        from core.readability_scorer import _RE_HYPHEN_APOS
        self._re = _RE_HYPHEN_APOS

    def test_sub_matches_inline(self):
        words = ["test-word", "it's", "father-in-law", "simple", "can't-stop"]
        for word in words:
            expected = re.sub(r"[-']", "", word)
            result = self._re.sub("", word)
            self.assertEqual(result, expected, msg=f"Mismatch for: {word!r}")


class TestCodeSwitchingDetectorRegex(unittest.TestCase):
    """core/code_switching_detector.py: _RE_NON_WORD."""

    def setUp(self):
        from core.code_switching_detector import _RE_NON_WORD
        self._re = _RE_NON_WORD

    def test_sub_matches_inline(self):
        words = ["hello!", "мир,", "test.word", "clean", "123"]
        for word in words:
            expected = re.sub(r"[^\w]", "", word)
            result = self._re.sub("", word)
            self.assertEqual(result, expected, msg=f"Mismatch for: {word!r}")


class TestAutoTitleRegex(unittest.TestCase):
    """core/auto_title.py: _RE_WORD_PUNCT."""

    def setUp(self):
        from core.auto_title import _RE_WORD_PUNCT
        self._re = _RE_WORD_PUNCT

    def test_sub_matches_inline(self):
        words = ["Слово!", "Hello,", "word.", "СловоWithPunctuation;", "clean"]
        for word in words:
            expected = re.sub(r"[^\wА-Яа-яёЁ]", "", word, flags=re.UNICODE).lower()
            result = self._re.sub("", word).lower()
            self.assertEqual(result, expected, msg=f"Mismatch for: {word!r}")


if __name__ == "__main__":
    unittest.main()
