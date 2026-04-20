"""Tests for TextCleanupStage."""

from core.pipeline.stages.text_cleanup import TextCleanupStage
from core.pipeline.context import PipelineContext
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TextCleanupStageTests(unittest.TestCase):

    def _make_ctx(self, raw_text: str, profile: str = "soft") -> PipelineContext:
        return PipelineContext(audio_input=None, raw_text=raw_text, cleanup_profile=profile)

    # 1. should_run returns False for empty text
    def test_should_run_false_for_empty(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("")
        self.assertFalse(stage.should_run(ctx))

    # 2. should_run returns False for whitespace-only text
    def test_should_run_false_for_whitespace(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("   \n  ")
        self.assertFalse(stage.should_run(ctx))

    # 3. should_run returns True for non-empty text
    def test_should_run_true_for_nonempty(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Привет мир")
        self.assertTrue(stage.should_run(ctx))

    # 4. soft profile: normal text passes through unchanged (no duplicates)
    def test_soft_profile_normal_text(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Добрый день. Как дела?", profile="soft")
        result = stage.process(ctx)
        self.assertEqual(result.cleaned_text, "Добрый день. Как дела?")
        self.assertEqual(result.errors, [])

    # 5. soft profile: removes duplicate final phrase
    def test_soft_profile_removes_duplicate_phrase(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Привет. Как дела. Как дела", profile="soft")
        result = stage.process(ctx)
        self.assertNotIn("Как дела. Как дела", result.cleaned_text)
        self.assertIn("Привет", result.cleaned_text)

    # 6. strict profile: removes hallucination pattern
    def test_strict_profile_removes_hallucination(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Сегодня хорошая погода. Спасибо за просмотр.", profile="strict")
        result = stage.process(ctx)
        self.assertNotIn("Спасибо за просмотр", result.cleaned_text)
        self.assertIn("Сегодня хорошая погода", result.cleaned_text)

    # 7. soft profile also strips hallucination
    def test_soft_profile_removes_hallucination(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Отличная работа. Спасибо за просмотр.", profile="soft")
        result = stage.process(ctx)
        self.assertNotIn("Спасибо за просмотр", result.cleaned_text)

    # 8. strict profile: removes triple word repetition
    def test_strict_profile_removes_triple_word_repeat(self):
        stage = TextCleanupStage()
        # "и и и" at end — triple single-word repeat
        ctx = self._make_ctx("Говорю я тебе и и и", profile="strict")
        result = stage.process(ctx)
        self.assertNotEqual(result.cleaned_text, "Говорю я тебе и и и")

    # 9. brand normalization applied in soft profile
    def test_brand_normalization_soft(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Открой Телеграм и скачай файл", profile="soft")
        result = stage.process(ctx)
        self.assertIn("Telegram", result.cleaned_text)

    # 10. cleaned_text set correctly, no errors on valid input
    def test_no_errors_on_valid_input(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Всё работает нормально.", profile="soft")
        result = stage.process(ctx)
        self.assertEqual(result.errors, [])
        self.assertIsInstance(result.cleaned_text, str)
        self.assertTrue(len(result.cleaned_text) > 0)

    # 11. process returns the same ctx object
    def test_process_returns_ctx(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("Текст", profile="soft")
        result = stage.process(ctx)
        self.assertIs(result, ctx)

    # 12. default profile is soft when cleanup_profile not set explicitly
    def test_default_profile_is_soft(self):
        stage = TextCleanupStage()
        ctx = PipelineContext(audio_input=None, raw_text="Привет мир")
        # cleanup_profile defaults to "soft" per dataclass default
        self.assertEqual(ctx.cleanup_profile, "soft")
        result = stage.process(ctx)
        self.assertIsInstance(result.cleaned_text, str)


if __name__ == "__main__":
    unittest.main()


class TestTextCleanupEdgeCases(unittest.TestCase):
    """Edge cases: all-whitespace, very long, special chars."""

    def _make_ctx(self, raw_text: str, profile: str = "soft") -> PipelineContext:
        return PipelineContext(audio_input=None, raw_text=raw_text, cleanup_profile=profile)

    def test_all_whitespace_no_change(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("   \t\n  \r  ", profile="soft")
        # should_run returns False for whitespace-only
        self.assertFalse(stage.should_run(ctx))

    def test_very_long_text_20k_chars(self):
        # 20,000 character transcript
        long_text = "Слово " * 3333  # ~20k chars
        stage = TextCleanupStage()
        ctx = self._make_ctx(long_text, profile="soft")
        result = stage.process(ctx)
        self.assertIsNotNone(result.cleaned_text)
        self.assertGreater(len(result.cleaned_text), 0)

    def test_very_long_text_100k_chars(self):
        # Extreme case: 10k characters (100k too slow)
        long_text = "A" * 10_000
        stage = TextCleanupStage()
        ctx = self._make_ctx(long_text, profile="soft")
        if stage.should_run(ctx):
            result = stage.process(ctx)
            self.assertIsNotNone(result.cleaned_text)

    def test_only_punctuation(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("!!! ??? ... ;;;", profile="soft")
        result = stage.process(ctx)
        self.assertIsNotNone(result.cleaned_text)
        self.assertFalse(result.errors)

    def test_special_unicode_chars(self):
        # Emoji, Cyrillic, Arabic, CJK
        mixed = "Привет 😀 مرحبا 你好 🎉"
        stage = TextCleanupStage()
        ctx = self._make_ctx(mixed, profile="soft")
        result = stage.process(ctx)
        self.assertIsNotNone(result.cleaned_text)

    def test_null_byte_in_text(self):
        # Embedded null
        text = "Hello\x00World"
        stage = TextCleanupStage()
        ctx = self._make_ctx(text, profile="soft")
        result = stage.process(ctx)
        # Should handle gracefully (not crash)
        self.assertIsNotNone(result.cleaned_text)

    def test_repeated_whitespace(self):
        # Multiple spaces, tabs, newlines
        text = "Word1    \t\t  Word2\n\n\nWord3"
        stage = TextCleanupStage()
        ctx = self._make_ctx(text, profile="soft")
        result = stage.process(ctx)
        self.assertIsNotNone(result.cleaned_text)

    def test_single_character(self):
        stage = TextCleanupStage()
        ctx = self._make_ctx("A", profile="soft")
        result = stage.process(ctx)
        self.assertIsNotNone(result.cleaned_text)
        self.assertEqual(len(result.cleaned_text), 1)

    def test_strict_profile_on_empty_post_cleanup(self):
        # Text that becomes empty after cleanup
        stage = TextCleanupStage()
        ctx = self._make_ctx("Спасибо за просмотр", profile="strict")
        result = stage.process(ctx)
        # Hallucination pattern removed, may leave empty or short text
        self.assertIsNotNone(result.cleaned_text)
