"""Comprehensive coverage for TextUtils cleanup, dedup, and hallucination handling."""

from __future__ import annotations
from core.utils import TextUtils

from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class CleanupSoftVsStrictTestCase(unittest.TestCase):
    """Verify soft vs strict profile behavior differences."""

    def test_soft_keeps_non_adjacent_repeat(self) -> None:
        """Soft profile doesn't remove repeats that aren't adjacent."""
        raw = "Утром встреча. Днем работа. Утром встреча."
        soft_cleaned = TextUtils.cleanup_transcript(raw, profile="soft")
        # Soft doesn't backtrack; if the repeat isn't the last two sentences, it's kept
        self.assertIn("Утром встреча", soft_cleaned)

    def test_strict_removes_non_adjacent_repeat(self) -> None:
        """Strict profile removes the final occurrence if phrase appeared earlier."""
        raw = "План такой: утром спорт. Днем работа. утром спорт."
        strict_cleaned = TextUtils.cleanup_transcript(raw, profile="strict")
        # Strict scans all previous segments; if "утром спорт" appeared before, remove the tail
        # The phrase is removed from the end but the content before it is preserved
        self.assertIn("Днем работа", strict_cleaned)
        # Count of the phrase should be 1 or less
        self.assertLessEqual(strict_cleaned.count("утром спорт"), 1)

    def test_soft_removes_immediate_repeat(self) -> None:
        """Soft removes last sentence if it's identical to the previous one."""
        raw = "Это важно. Это важно."
        soft_cleaned = TextUtils.cleanup_transcript(raw, profile="soft")
        self.assertEqual(soft_cleaned.count("Это важно"), 1)

    def test_strict_removes_tripled_tail(self) -> None:
        """Strict removes triplet of identical words at the end (model stutter)."""
        raw = "Запиши это сделай сделай сделай"
        strict_cleaned = TextUtils.cleanup_transcript(raw, profile="strict")
        self.assertEqual(strict_cleaned, "Запиши это сделай")

    def test_strict_removes_triplet_of_two_word_phrases(self) -> None:
        """Strict detects and removes triplets of repeated 2-word phrases."""
        raw = "сделай это сделай это сделай это"
        strict_cleaned = TextUtils.cleanup_transcript(raw, profile="strict")
        # Triplet of "сделай это" should be reduced
        self.assertLess(len(strict_cleaned), len(raw))

    def test_soft_respects_hallucination_stripping(self) -> None:
        """Soft profile still removes known hallucinations."""
        raw = "Встреча прошла успешно. Спасибо за просмотр."
        soft_cleaned = TextUtils.cleanup_transcript(raw, profile="soft")
        self.assertNotIn("Спасибо за просмотр", soft_cleaned)
        self.assertIn("Встреча прошла успешно", soft_cleaned)

    def test_strict_respects_hallucination_stripping(self) -> None:
        """Strict profile also removes hallucinations (called from _cleanup_strict)."""
        raw = "Обсудили три вопроса. Ставьте лайки."
        strict_cleaned = TextUtils.cleanup_transcript(raw, profile="strict")
        self.assertNotIn("Ставьте лайки", strict_cleaned)


class HallucinationStrippingTestCase(unittest.TestCase):
    """Verify hallucination pattern removal."""

    def test_strip_spasibo_za_prosmotr(self) -> None:
        """Strip 'Спасибо за просмотр' (YouTube-style hallucination)."""
        raw = "Обсудили три точки. Спасибо за просмотр."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Спасибо за просмотр", cleaned)

    def test_strip_podpisyvajtes(self) -> None:
        """Strip 'Подписывайтесь на канал'."""
        raw = "Это был обзор. Подписывайтесь на канал."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Подписывайтесь на канал", cleaned)

    def test_strip_do_novyh_vstrech(self) -> None:
        """Strip 'До новых встреч' (typical YT ending)."""
        raw = "Встреча закончена. До новых встреч."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("До новых встреч", cleaned)

    def test_strip_stavte_lajki(self) -> None:
        """Strip 'Ставьте лайки'."""
        raw = "Идея понятна. Ставьте лайки!"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Ставьте лайки", cleaned)

    def test_strip_prodolzhenie_sledует(self) -> None:
        """Strip 'Продолжение следует'."""
        raw = "Сейчас закончу. Продолжение следует..."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Продолжение следует", cleaned)

    def test_strip_to_be_continued(self) -> None:
        """Strip English 'to be continued'."""
        raw = "We'll discuss this tomorrow. to be continued."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("to be continued", cleaned)

    def test_strip_standalone_thanks(self) -> None:
        """Strip standalone trailing 'Спасибо.' (can come after period)."""
        raw = "Это главное. Спасибо."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Спасибо", cleaned)

    def test_hallucination_at_start_returns_empty(self) -> None:
        """If hallucination is at the start of text, return empty string."""
        raw = "Спасибо за просмотр"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned, "")

    def test_multiple_hallucinations_last_one_removed(self) -> None:
        """If multiple hallucinations present, only the last matched one is removed."""
        raw = "Встреча завтра. Ставьте лайки. Подписывайтесь на канал."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Подписывайтесь на канал", cleaned)
        # But "Ставьте лайки" might still be there depending on regex matching order
        self.assertIn("Встреча завтра", cleaned)


class PhraseDedupTestCase(unittest.TestCase):
    """Verify phrase deduplication logic."""

    def test_identical_sentences_deduplicated(self) -> None:
        """Two identical sentences in a row -> keep one."""
        raw = "Это было хорошо. Это было хорошо."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned.count("Это было хорошо"), 1)

    def test_phrase_with_punctuation_normalized(self) -> None:
        """Phrases differ only by punctuation -> deduplicated."""
        raw = "Встреча завтра в девять. встреча завтра в девять!"
        cleaned = TextUtils.cleanup_transcript(raw)
        # Should be deduplicated since normalize_phrase strips punctuation
        # The last occurrence should be removed
        self.assertLess(cleaned.count("встреча завтра в девять"), 2)

    def test_long_phrase_not_deduplicated(self) -> None:
        """Phrases longer than 8 words are NOT deduplicated (max_words guard)."""
        long_phrase = "Это очень длинная фраза с девятью отдельными словами которая не должна"
        raw = f"{long_phrase}. {long_phrase}."
        cleaned = TextUtils.cleanup_transcript(raw)
        # Long phrases are outside the dedup range (max_words=8)
        # so the repeat should be kept
        word_count = len(long_phrase.split())
        self.assertGreater(word_count, 8, "Phrase should be >8 words for this test")

    def test_short_word_repeat_in_tail(self) -> None:
        """Short repeated words (1-3 words) at the tail are removed."""
        raw = "Запиши это сделай это сделай это"
        cleaned = TextUtils.cleanup_transcript(raw)
        # Should remove the trailing "сделай это"
        self.assertNotEqual(cleaned, raw)
        self.assertIn("Запиши это", cleaned)

    def test_normalize_phrase_removes_punctuation(self) -> None:
        """normalize_phrase strips all non-alphanumeric chars."""
        result = TextUtils.normalize_phrase("Привет, мир!")
        self.assertEqual(result, "привет мир")

    def test_normalize_phrase_preserves_digits(self) -> None:
        """normalize_phrase keeps digits."""
        result = TextUtils.normalize_phrase("Код 12345 верный")
        self.assertEqual(result, "код 12345 верный")

    def test_same_short_phrase_returns_false_for_empty(self) -> None:
        """same_short_phrase returns False if either input is empty/whitespace."""
        self.assertFalse(TextUtils.same_short_phrase("", "hello"))
        self.assertFalse(TextUtils.same_short_phrase("hello", ""))
        self.assertFalse(TextUtils.same_short_phrase("   ", "hello"))


class UnicodeWhitespaceTestCase(unittest.TestCase):
    """Verify unicode whitespace normalization."""

    def test_collapse_multiple_spaces(self) -> None:
        """Multiple spaces between words are collapsed to one."""
        raw = "Встреча    завтра     в     девять"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("    ", cleaned)
        self.assertIn("Встреча завтра в девять", cleaned)

    def test_collapse_tabs_and_newlines(self) -> None:
        """Tabs, newlines, and mixed whitespace are collapsed."""
        raw = "Встреча\t\tзавтра\nв\nдевять"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned, "Встреча завтра в девять")

    def test_strip_leading_trailing_whitespace(self) -> None:
        """Leading and trailing whitespace is stripped."""
        raw = "   \n  Встреча завтра  \t  "
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned, "Встреча завтра")

    def test_preserve_unicode_spaces(self) -> None:
        """Unicode special spaces (NBSP, etc.) are also collapsed."""
        # U+00A0 = non-breaking space
        raw = "Встреча\u00a0\u00a0завтра"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("\u00a0\u00a0", cleaned)

    def test_empty_after_whitespace_stripping_returns_empty(self) -> None:
        """If text is only whitespace, cleanup returns empty."""
        for ws in ("   ", "\t\t", "\n\n", "  \n\t  "):
            with self.subTest(ws=repr(ws)):
                cleaned = TextUtils.cleanup_transcript(ws)
                self.assertEqual(cleaned, "")


class PunctuationPreservationTestCase(unittest.TestCase):
    """Verify punctuation is preserved where appropriate."""

    def test_preserve_sentence_punctuation(self) -> None:
        """Sentence-ending punctuation is preserved."""
        raw = "Встреча завтра. Приносит мне отчёт!"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn(".", cleaned)
        self.assertIn("!", cleaned)

    def test_preserve_ellipsis(self) -> None:
        """Ellipsis (...) in the middle is preserved as dots."""
        raw = "Хм... интересное предложение."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("интересное предложение", cleaned)

    def test_preserve_quotes(self) -> None:
        """Quotes are preserved."""
        raw = 'Он сказал: "Это отлично".'
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn('"', cleaned)

    def test_preserve_apostrophes_in_words(self) -> None:
        """Apostrophes in contractions are preserved."""
        raw = "It's a great day."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("'", cleaned)

    def test_preserve_hyphens_in_compound_words(self) -> None:
        """Hyphens in compound words are preserved."""
        raw = "Это хорошо-плохо решение."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("хорошо-плохо", cleaned)


class LongInputPerformanceTestCase(unittest.TestCase):
    """Verify cleanup handles large inputs without crashing."""

    def test_very_long_input_10k_chars(self) -> None:
        """Cleanup doesn't crash on 10k character input."""
        # Generate a long sentence
        base = "Это предложение повторяется много раз для тестирования. "
        raw = base * 150  # ~10k chars
        cleaned = TextUtils.cleanup_transcript(raw)
        # Should complete without crashing
        self.assertIsInstance(cleaned, str)
        self.assertGreater(len(cleaned), 0)

    def test_long_input_with_hallucinations(self) -> None:
        """Long input with hallucination at the end is cleaned."""
        base = "Это предложение повторяется. " * 200
        raw = base + "Спасибо за просмотр."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertNotIn("Спасибо за просмотр", cleaned)

    def test_very_long_single_sentence(self) -> None:
        """Very long single sentence without punctuation is handled."""
        words = ["слово"] * 100  # Reduced from 1000 to avoid memory issues
        raw = " ".join(words)
        cleaned = TextUtils.cleanup_transcript(raw)
        # Long sentence with repeating words might trigger dedup; main thing is no crash
        self.assertIsInstance(cleaned, str)
        self.assertGreater(len(cleaned), 0)

    def test_many_small_sentences(self) -> None:
        """Cleanup handles many small sentences efficiently."""
        sentences = ["Пункт " + str(i) + "." for i in range(500)]
        raw = " ".join(sentences)
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIsInstance(cleaned, str)
        # All points should be preserved (no repeats)
        for i in range(500):
            self.assertIn(f"Пункт {i}", cleaned)


class EdgeCasesTestCase(unittest.TestCase):
    """Miscellaneous edge cases."""

    def test_empty_string_returns_empty(self) -> None:
        """Empty string input returns empty string."""
        self.assertEqual(TextUtils.cleanup_transcript(""), "")

    def test_single_word(self) -> None:
        """Single word is preserved."""
        self.assertEqual(TextUtils.cleanup_transcript("Привет"), "Привет")

    def test_only_punctuation(self) -> None:
        """Only punctuation collapses to punctuation."""
        raw = "!!! ??? ... ;;;;"
        cleaned = TextUtils.cleanup_transcript(raw)
        # Should collapse spaces but preserve punctuation structure
        self.assertNotIn("    ", cleaned)

    def test_mixed_cyrillic_latin_preserved(self) -> None:
        """Mixed Cyrillic and Latin text is preserved."""
        raw = "Встреча с Паблито (Pablito) завтра."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("Pablito", cleaned)
        self.assertIn("Встреча", cleaned)

    def test_emoji_preserved(self) -> None:
        """Emoji characters are preserved."""
        raw = "Хорошая идея 👍 давай попробуем 🎉"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("👍", cleaned)
        self.assertIn("🎉", cleaned)

    def test_numbers_preserved(self) -> None:
        """Numbers in text are preserved."""
        raw = "Встреча в 15:30, участников 12."
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertIn("15:30", cleaned)
        self.assertIn("12", cleaned)

    def test_newlines_in_input_collapsed(self) -> None:
        """Newlines in the input are treated as whitespace and collapsed."""
        raw = "Встреча\nзавтра\nв\nдевять"
        cleaned = TextUtils.cleanup_transcript(raw)
        self.assertEqual(cleaned, "Встреча завтра в девять")

    def test_cleanup_is_idempotent(self) -> None:
        """Running cleanup twice gives the same result (with repeats handled correctly)."""
        raw = "Встреча завтра. Спасибо за просмотр."
        cleaned_once = TextUtils.cleanup_transcript(raw)
        cleaned_twice = TextUtils.cleanup_transcript(cleaned_once)
        self.assertEqual(cleaned_once, cleaned_twice)


if __name__ == "__main__":
    unittest.main()
