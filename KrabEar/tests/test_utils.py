"""Unit tests for core.utils — TextUtils, is_likely_repetition_loop, brand replacements.

Wave 175: первый coverage для utils.py.

Design constraints:
  - Pure unit tests — no disk, no network, no MLX, no models.
  - Tests are deterministic и locale-independent.
"""
import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.utils import (  # noqa: E402
    TextUtils,
    is_likely_repetition_loop,
)


# ---------------------------------------------------------------------------
# TextUtils.normalize_phrase
# ---------------------------------------------------------------------------

class TestNormalizePhrase(unittest.TestCase):

    def test_lowercases_text(self):
        self.assertEqual(TextUtils.normalize_phrase("Hello World"), "hello world")

    def test_strips_punctuation(self):
        # _NORMALIZE_RE removes non-word/non-space/non-hyphen chars
        result = TextUtils.normalize_phrase("Привет, мир!")
        self.assertNotIn(",", result)
        self.assertNotIn("!", result)

    def test_strips_whitespace(self):
        result = TextUtils.normalize_phrase("  hello  ")
        self.assertEqual(result, "hello")

    def test_empty_string(self):
        self.assertEqual(TextUtils.normalize_phrase(""), "")


# ---------------------------------------------------------------------------
# TextUtils.same_short_phrase
# ---------------------------------------------------------------------------

class TestSameShortPhrase(unittest.TestCase):

    def test_identical_phrases(self):
        self.assertTrue(TextUtils.same_short_phrase("Hello", "Hello"))

    def test_identical_ignoring_punctuation(self):
        self.assertTrue(TextUtils.same_short_phrase("Привет!", "Привет"))

    def test_different_phrases(self):
        self.assertFalse(TextUtils.same_short_phrase("Hello", "World"))

    def test_empty_phrases(self):
        self.assertFalse(TextUtils.same_short_phrase("", ""))

    def test_long_phrase_not_same(self):
        long_phrase = "слово " * 10  # 10 words
        self.assertFalse(TextUtils.same_short_phrase(long_phrase, long_phrase, max_words=8))


# ---------------------------------------------------------------------------
# TextUtils.cleanup_transcript — soft profile
# ---------------------------------------------------------------------------

class TestCleanupTranscriptSoft(unittest.TestCase):

    def test_removes_trailing_duplicate_sentence(self):
        text = "я записываю голос. я записываю голос."
        result = TextUtils.cleanup_transcript(text, profile="soft")
        # Second copy should be removed
        self.assertNotEqual(result, text)

    def test_empty_input(self):
        self.assertEqual(TextUtils.cleanup_transcript(""), "")

    def test_whitespace_only(self):
        result = TextUtils.cleanup_transcript("   ")
        self.assertEqual(result, "")

    def test_no_duplicates_unchanged(self):
        text = "я тестирую приложение"
        result = TextUtils.cleanup_transcript(text, profile="soft")
        # No duplication → brand normalization may change text but not remove content
        self.assertIn("тестирую", result)

    def test_brand_normalization_applied(self):
        """Mercadona кириллица → латиница."""
        result = TextUtils.cleanup_transcript("пошёл в Меркадонну купить продукты")
        self.assertIn("Mercadona", result)

    def test_strips_youtube_hallucination(self):
        text = "Привет всем. Спасибо за просмотр."
        result = TextUtils.cleanup_transcript(text, profile="soft")
        self.assertNotIn("Спасибо за просмотр", result)

    def test_strips_trailing_spasibo(self):
        text = "Закончил работу. Спасибо."
        result = TextUtils.cleanup_transcript(text, profile="soft")
        self.assertNotIn("Спасибо.", result)

    def test_dedup_re_articulation_comma(self):
        """'записываю уже, уже' → 'записываю уже'."""
        text = "записываю уже, уже это"
        result = TextUtils.cleanup_transcript(text, profile="soft")
        # Comma-separated repeat should be collapsed
        self.assertNotIn("уже, уже", result)


# ---------------------------------------------------------------------------
# TextUtils.cleanup_transcript — strict profile
# ---------------------------------------------------------------------------

class TestCleanupTranscriptStrict(unittest.TestCase):

    def test_strict_removes_duplicates(self):
        text = "тест тест тест тест тест тест тест тест тест тест"
        result = TextUtils.cleanup_transcript(text, profile="strict")
        # Strict should detect repetition loop and reduce text
        self.assertLess(len(result), len(text))

    def test_strict_does_not_mangle_normal_text(self):
        text = "сегодня хорошая погода и я рад этому"
        result = TextUtils.cleanup_transcript(text, profile="strict")
        # Core words should survive
        self.assertIn("хорошая", result)

    def test_strict_also_applies_soft(self):
        """Strict includes brand normalization from soft pass."""
        text = "Телеграм уведомление пришло"
        result = TextUtils.cleanup_transcript(text, profile="strict")
        self.assertIn("Telegram", result)


# ---------------------------------------------------------------------------
# TextUtils._dedup_re_articulation
# ---------------------------------------------------------------------------

class TestDedupReArticulation(unittest.TestCase):

    def test_comma_separated_single_word_dedup(self):
        result = TextUtils._dedup_re_articulation("протестирую, протестирую")
        self.assertNotIn("протестирую, протестирую", result)

    def test_comma_separated_phrase_dedup(self):
        result = TextUtils._dedup_re_articulation("с выбранной, с выбранной")
        self.assertNotIn("с выбранной, с выбранной", result)

    def test_emphasis_marker_preserved(self):
        """'очень очень важно' должен оставаться (emphasis, не repeat)."""
        result = TextUtils._dedup_re_articulation("очень очень важно")
        self.assertIn("очень", result)

    def test_no_repeat_unchanged(self):
        text = "привет мир как дела"
        result = TextUtils._dedup_re_articulation(text)
        self.assertEqual(result, text)

    def test_empty_input(self):
        self.assertEqual(TextUtils._dedup_re_articulation(""), "")

    def test_multiword_dedup(self):
        """'вот сейчас вот сейчас' → 'вот сейчас'."""
        result = TextUtils._dedup_re_articulation("вот сейчас вот сейчас")
        self.assertNotIn("вот сейчас вот сейчас", result)


# ---------------------------------------------------------------------------
# TextUtils.normalize_entities
# ---------------------------------------------------------------------------

class TestNormalizeEntities(unittest.TestCase):

    def test_mercadona(self):
        self.assertIn("Mercadona", TextUtils.normalize_entities("пошёл в Меркадонну"))

    def test_telegram(self):
        self.assertIn("Telegram", TextUtils.normalize_entities("в Телеграм написали"))

    def test_whisper_brand(self):
        self.assertIn("Whisper", TextUtils.normalize_entities("Виспер модель"))

    def test_time_normalization_hhmm(self):
        # "15.00" → "15:00"
        result = TextUtils.normalize_entities("встреча в 15.00 часов")
        self.assertIn("15:00", result)

    def test_time_normalization_dot(self):
        # TIME_NORMALIZE_RE requires a separator character (. or :) — not bare space
        result = TextUtils.normalize_entities("начнём в 09.00")
        self.assertIn("09:00", result)

    def test_empty_string(self):
        self.assertEqual(TextUtils.normalize_entities(""), "")

    def test_github_cyrillic(self):
        self.assertIn("GitHub", TextUtils.normalize_entities("в Гит-Хаб пуш сделал"))

    def test_python_brand(self):
        # Pattern requires «Питон 3» (bare «Питон» is a common word — snake)
        self.assertIn("Python 3", TextUtils.normalize_entities("написал на Питон 3"))

    def test_docker_brand(self):
        self.assertIn("Docker", TextUtils.normalize_entities("запустил Докер контейнер"))


# ---------------------------------------------------------------------------
# is_likely_repetition_loop
# ---------------------------------------------------------------------------

class TestIsLikelyRepetitionLoop(unittest.TestCase):

    def test_normal_text_not_loop(self):
        text = "сегодня я работал над новым проектом и всё шло хорошо"
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertFalse(is_loop)
        self.assertEqual(reason, "")

    def test_empty_text_not_loop(self):
        is_loop, _ = is_likely_repetition_loop("")
        self.assertFalse(is_loop)

    def test_short_text_not_loop(self):
        is_loop, _ = is_likely_repetition_loop("привет")
        self.assertFalse(is_loop)

    def test_repeated_bigram_detected(self):
        # 5+ identical adjacent bigrams triggers heuristic 1
        phrase = "согласен да " * 8
        is_loop, reason = is_likely_repetition_loop(phrase.strip())
        self.assertTrue(is_loop)
        self.assertIn("repeated_bigram", reason)

    def test_repeated_sentences_detected(self):
        # 3+ identical sentences trigger heuristic 2
        sent = "атакса хвостимда"
        text = f"{sent}. {sent}. {sent}. {sent}."
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop)
        self.assertIn("repeated_sentence", reason)

    def test_low_unique_ratio_detected(self):
        # >30 tokens but very few unique → at least one heuristic fires
        # (heuristic 1 or 3 — either way is_loop must be True)
        word = "повторяю"
        text = (word + " ") * 35
        is_loop, reason = is_likely_repetition_loop(text.strip())
        self.assertTrue(is_loop)
        self.assertNotEqual(reason, "")

    def test_returns_tuple(self):
        result = is_likely_repetition_loop("test text here")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_few_tokens_not_loop(self):
        # < 6 tokens → early return False
        is_loop, _ = is_likely_repetition_loop("один два три")
        self.assertFalse(is_loop)


# ---------------------------------------------------------------------------
# TextUtils._strip_hallucinations
# ---------------------------------------------------------------------------

class TestStripHallucinations(unittest.TestCase):

    def test_spasibo_za_prosmotr(self):
        text = "Смотрите следующее видео. Спасибо за просмотр."
        result = TextUtils._strip_hallucinations(text)
        self.assertNotIn("спасибо за просмотр", result.lower())

    def test_podpisyvajtes_na_kanal(self):
        text = "Ставьте лайки и подписывайтесь на канал"
        result = TextUtils._strip_hallucinations(text)
        self.assertNotIn("подписывайтесь на канал", result.lower())

    def test_do_novyh_vstrech(self):
        text = "Пока всем! До новых встреч"
        result = TextUtils._strip_hallucinations(text)
        self.assertNotIn("до новых встреч", result.lower())

    def test_no_hallucination_unchanged(self):
        text = "я тестирую систему распознавания речи"
        result = TextUtils._strip_hallucinations(text)
        self.assertEqual(result, text)

    def test_content_before_hallucination_preserved(self):
        text = "Важная информация здесь. Спасибо за просмотр."
        result = TextUtils._strip_hallucinations(text)
        self.assertIn("Важная информация", result)


if __name__ == "__main__":
    unittest.main()
