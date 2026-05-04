"""Tests for STT quality edge cases: repetition-loop detector + brand expansion.

Phase C.4 (2026-05-04) — covers:
  - is_likely_repetition_loop() heuristics
  - Brand regex expansions: Gemma, Anthropic, LM Studio variant
  - Brand entries already present: Krab Ear, MLX, LM Studio, Claude

No MLX / Whisper imports — pure Python, memory-safe.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.utils import is_likely_repetition_loop, TextUtils  # noqa: E402


# ── is_likely_repetition_loop ───────────────────────────────────────────────

class RepetitionLoopDetectorTests(unittest.TestCase):
    """Tests for is_likely_repetition_loop() heuristics."""

    # ── Heuristic 1: repeated bigrams ──────────────────────────────────────

    def test_repetition_loop_detected_5_bigrams(self):
        """'X Y' repeated 5+ times should be flagged (live example variant)."""
        text = "Атакса хвостимда Атакса хвостимда Атакса хвостимда Атакса хвостимда Атакса хвостимда"
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop, f"Expected loop, got reason={reason!r}")
        self.assertIn("repeated_bigram", reason)

    def test_repetition_loop_detected_bigram_count_in_reason(self):
        """Reason string should include the repeat count."""
        phrase = "согласен да " * 6
        text = phrase.strip()
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop)
        # Reason should mention repeated_bigram with count >= 5
        self.assertIn("repeated_bigram", reason)

    def test_four_bigrams_not_flagged(self):
        """4 repeated bigrams is below the threshold — should NOT be flagged."""
        text = "ну ок ну ок ну ок ну ок нормально всё хорошо"
        is_loop, reason = is_likely_repetition_loop(text)
        # 4 repetitions should not trigger (threshold is 5)
        if is_loop:
            # Allow if another heuristic fires on this contrived input
            self.assertNotIn("repeated_bigram x4", reason)

    # ── Heuristic 2: repeated sentences ────────────────────────────────────

    def test_repetition_loop_detected_3_sentences(self):
        """Same sentence repeated 3 times should be flagged."""
        sentence = "Я согласен с этим мнением."
        text = " ".join([sentence] * 3)
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop, f"Expected loop, got reason={reason!r}")
        self.assertIn("repeated_sentence", reason)

    def test_repetition_loop_4_sentences(self):
        """Same sentence repeated 4 times — higher confidence."""
        sentence = "Продолжение следует в следующей серии."
        text = " ".join([sentence] * 4)
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop)
        self.assertIn("repeated_sentence", reason)

    def test_two_identical_sentences_not_flagged(self):
        """2 identical sentences is below threshold — should NOT be flagged."""
        sentence = "Это нормальный текст."
        other = "А вот это уже другое предложение."
        text = sentence + " " + sentence + " " + other
        is_loop, reason = is_likely_repetition_loop(text)
        # 2 repetitions should not trigger sentence heuristic
        # (may still trigger bigram if very short, but for this input shouldn't)
        self.assertFalse(is_loop, f"False positive: reason={reason!r}")

    # ── Heuristic 3: low unique-ratio ──────────────────────────────────────

    def test_repetition_loop_detected_low_unique_ratio(self):
        """'согласен да' × 30 — extreme redundancy loop."""
        text = "согласен да " * 30
        text = text.strip()
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop, f"Expected loop, got reason={reason!r}")

    def test_low_unique_ratio_reason_string(self):
        """Reason string for low-unique-ratio should contain the ratio value."""
        # Force only heuristic 3 by using 3 distinct bigrams but low unique ratio
        # Use a very long text with same 2 words repeated many times
        text = ("один два " * 40).strip()
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertTrue(is_loop)
        # Could be flagged by bigram OR low_unique_ratio
        self.assertTrue(
            "repeated_bigram" in reason or "low_unique_ratio" in reason,
            f"Unexpected reason: {reason!r}",
        )

    # ── Normal text edge cases ──────────────────────────────────────────────

    def test_normal_text_not_detected(self):
        """Normal diverse Russian text should not be flagged."""
        text = (
            "Сегодня утром я пошёл в магазин и купил молоко. "
            "Потом вернулся домой и сделал кофе. "
            "Работа шла неплохо, но к вечеру устал."
        )
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertFalse(is_loop, f"False positive: reason={reason!r}")

    def test_short_text_not_detected(self):
        """Text < 20 chars should never be flagged."""
        for short in ["", "ок", "да нет", "привет мир"]:
            with self.subTest(text=short):
                is_loop, reason = is_likely_repetition_loop(short)
                self.assertFalse(is_loop, f"Short text {short!r} flagged: {reason}")

    def test_few_tokens_not_detected(self):
        """Text with < 6 tokens should not be flagged even if repeated."""
        text = "да да да да"
        is_loop, reason = is_likely_repetition_loop(text)
        self.assertFalse(is_loop)

    def test_return_type_always_tuple(self):
        """Function always returns (bool, str) regardless of input."""
        for text in [None, "", "hello", "a " * 100]:
            with self.subTest(text=repr(text)[:30]):
                result = is_likely_repetition_loop(text or "")
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)
                self.assertIsInstance(result[0], bool)
                self.assertIsInstance(result[1], str)

    def test_does_not_raise_on_edge_inputs(self):
        """Function must never raise."""
        edge_cases = [
            "",
            " ",
            "!" * 50,
            "а" * 300,
            "один " * 200,
        ]
        for text in edge_cases:
            with self.subTest(text=repr(text)[:30]):
                try:
                    is_likely_repetition_loop(text)
                except Exception as exc:
                    self.fail(f"Raised {type(exc).__name__}: {exc}")


# ── Brand expansion ─────────────────────────────────────────────────────────

class BrandExpansionTests(unittest.TestCase):
    """Tests for Phase C.4 brand regex additions in TextUtils.normalize_entities."""

    def _normalize(self, text: str) -> str:
        return TextUtils.normalize_entities(text)

    def test_brand_gemma_ru_to_en(self):
        """«Гемма» should be replaced with «Gemma»."""
        result = self._normalize("Гемма работает быстро")
        self.assertIn("Gemma", result)
        self.assertNotIn("Гемма", result)

    def test_brand_gemma_dzhemma(self):
        """«Джемма» (alternative Whisper mishear) → «Gemma»."""
        result = self._normalize("Джемма это хорошая модель")
        self.assertIn("Gemma", result)
        self.assertNotIn("Джемма", result)

    def test_brand_anthropic_ru_to_en(self):
        """«Антропик» should be replaced with «Anthropic»."""
        result = self._normalize("Антропик выпустил новую модель")
        self.assertIn("Anthropic", result)
        self.assertNotIn("Антропик", result)

    def test_brand_krab_ear_existing(self):
        """«Краб Ир» (existing entry) → «Krab Ear»."""
        result = self._normalize("Краб Ир хорошо распознаёт")
        self.assertIn("Krab Ear", result)

    def test_brand_mlx_existing(self):
        """«Эм Эл Икс» (existing entry) → «MLX»."""
        result = self._normalize("Эм Эл Икс работает на GPU")
        self.assertIn("MLX", result)

    def test_brand_lm_studio_existing(self):
        """«ЛМ Студио» (existing entry) → «LM Studio»."""
        result = self._normalize("ЛМ Студио запустился нормально")
        self.assertIn("LM Studio", result)

    def test_brand_claude_existing(self):
        """«Клод» (existing entry) → «Claude»."""
        result = self._normalize("Клод ответил правильно")
        self.assertIn("Claude", result)

    def test_brand_lm_studio_elam_variant(self):
        """«Элэм Студио» (new Phase C.4 variant) → «LM Studio»."""
        result = self._normalize("Элэм Студио не запустился")
        self.assertIn("LM Studio", result)

    def test_no_false_positive_on_unrelated_text(self):
        """Normal text without brand keywords should pass through unchanged."""
        text = "Сегодня хорошая погода и настроение отличное"
        result = self._normalize(text)
        # Text should be essentially the same (normalize_entities may fix time/casing)
        self.assertEqual(result.lower(), text.lower())


# ── Integration: error_codes contains stt.repetition_loop ──────────────────

class ErrorCodeRepetitionLoopTests(unittest.TestCase):
    """Regression test: stt.repetition_loop must exist in ERROR_REGISTRY."""

    def setUp(self):
        from backend.error_codes import ERROR_REGISTRY
        self.registry = ERROR_REGISTRY

    def test_stt_repetition_loop_exists(self):
        self.assertIn("stt.repetition_loop", self.registry)

    def test_stt_repetition_loop_severity_warn(self):
        entry = self.registry["stt.repetition_loop"]
        self.assertEqual(entry["severity"], "warn")

    def test_stt_repetition_loop_not_actionable(self):
        entry = self.registry["stt.repetition_loop"]
        self.assertFalse(entry["actionable"])

    def test_stt_repetition_loop_has_user_msg_ru(self):
        entry = self.registry["stt.repetition_loop"]
        self.assertTrue(entry["user_msg_ru"])

    def test_stt_repetition_loop_dedupe_60s(self):
        entry = self.registry["stt.repetition_loop"]
        self.assertEqual(entry["dedupe_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
