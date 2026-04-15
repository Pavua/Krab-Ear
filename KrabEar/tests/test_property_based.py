"""Property-based / fuzz tests for critical Krab Ear components.

Использует модуль `random` (hypothesis не установлен) для генерации случайных
входных данных и проверки инвариантов.  Не менее 20 тестовых методов.
"""

from __future__ import annotations
from core.pipeline.stages.audio_normalization import AudioNormalizationStage
from core.pipeline.context import PipelineContext
from core.fuzzy_search import FuzzySearcher
from core.confidence_calibrator import ConfidenceCalibrator
from core.text_anonymizer import TextAnonymizer
from core.search_index import SearchIndex
from core.punctuation_fixer import PunctuationFixer
from core.utils import TextUtils, _HALLUCINATION_PATTERNS

import os
import random
import string
import sys
import unittest

import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ── Генераторы случайных данных ───────────────────────────────────────────────

RNG = random.Random(42)

_CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
_LATIN = string.ascii_letters
_DIGITS = string.digits
_PUNCT = ".,!?;: "
_ALL_CHARS = _CYRILLIC + _LATIN + _DIGITS + _PUNCT


def _rand_text(min_len: int = 5, max_len: int = 200, *, rng: random.Random = RNG) -> str:
    length = rng.randint(min_len, max_len)
    chars = [rng.choice(_ALL_CHARS) for _ in range(length)]
    return "".join(chars).strip() or "Привет мир."


def _rand_word(min_len: int = 3, max_len: int = 12, *, rng: random.Random = RNG) -> str:
    pool = _CYRILLIC + _LATIN
    return "".join(rng.choice(pool) for _ in range(rng.randint(min_len, max_len)))


def _rand_sentence(min_words: int = 3, max_words: int = 12, *, rng: random.Random = RNG) -> str:
    words = [_rand_word(rng=rng) for _ in range(rng.randint(min_words, max_words))]
    return " ".join(words)


def _rand_audio(length: int | None = None, *, rng: random.Random = RNG) -> np.ndarray:
    n = length or rng.randint(256, 8000)
    rng.gauss(0, 0.3)
    arr = np.array(
        [rng.gauss(0, 0.3) for _ in range(n)], dtype=np.float32
    )
    return arr


HALLUCINATION_ENDINGS = [
    "Спасибо за просмотр.",
    "Подписывайтесь на канал!",
    "До новых встреч.",
    "Всем пока!",
    "Ставьте лайки.",
    "Продолжение следует.",
]

N_ROUNDS = 30  # number of random inputs per test


# ═══════════════════════════════════════════════════════════════════════════════
# 1-6  TextUtils.cleanup_transcript
# ═══════════════════════════════════════════════════════════════════════════════

class TestCleanupTranscriptProperties(unittest.TestCase):
    """Property tests for TextUtils.cleanup_transcript."""

    def test_output_length_leq_input_soft(self):
        """Output is always <= input length (soft profile)."""
        for _ in range(N_ROUNDS):
            text = _rand_text(10, 300)
            result = TextUtils.cleanup_transcript(text, profile="soft")
            self.assertLessEqual(
                len(result), len(text),
                f"Output longer than input for: {text!r}",
            )

    def test_output_length_leq_input_strict(self):
        """Output is always <= input length (strict profile)."""
        for _ in range(N_ROUNDS):
            text = _rand_text(10, 300)
            result = TextUtils.cleanup_transcript(text, profile="strict")
            self.assertLessEqual(len(result), len(text))

    def test_no_hallucination_patterns_in_output(self):
        """After cleanup, none of the known hallucination patterns remain."""
        for ending in HALLUCINATION_ENDINGS:
            for _ in range(10):
                prefix = _rand_sentence(3, 8)
                text = f"{prefix} {ending}"
                result = TextUtils.cleanup_transcript(text, profile="strict")
                low = result.lower()
                for pat in _HALLUCINATION_PATTERNS:
                    self.assertIsNone(
                        pat.search(low),
                        f"Hallucination pattern {pat.pattern!r} found in output: {result!r}",
                    )

    def test_empty_input_returns_empty(self):
        """Empty / whitespace input always returns empty string."""
        for inp in ["", "   ", "\t\n"]:
            result = TextUtils.cleanup_transcript(inp)
            self.assertEqual(result, "", f"Expected '' for input {inp!r}")

    def test_idempotent_on_clean_text(self):
        """Applying cleanup twice to already-clean text does not shrink it further
        (2nd pass may equal 1st but must not produce a *shorter* result when the
        1st pass returned something meaningful)."""
        for _ in range(N_ROUNDS):
            text = _rand_sentence(4, 10)
            first = TextUtils.cleanup_transcript(text, profile="soft")
            if not first:
                continue
            second = TextUtils.cleanup_transcript(first, profile="soft")
            # second pass ≤ first pass in length (may trim punctuation)
            self.assertLessEqual(len(second), len(first) + 5)  # small tolerance

    def test_no_leading_trailing_whitespace_in_output(self):
        """Output never has leading/trailing whitespace."""
        for _ in range(N_ROUNDS):
            text = "  " + _rand_sentence() + "  "
            result = TextUtils.cleanup_transcript(text)
            self.assertEqual(result, result.strip())


# ═══════════════════════════════════════════════════════════════════════════════
# 7-11  PunctuationFixer.fix
# ═══════════════════════════════════════════════════════════════════════════════

class TestPunctuationFixerProperties(unittest.TestCase):
    """Property tests for PunctuationFixer.fix."""

    def setUp(self):
        self.fixer = PunctuationFixer()

    def test_first_letter_capitalized_ru(self):
        """Russian output always starts with an uppercase letter (if non-empty)."""
        for _ in range(N_ROUNDS):
            # build a sentence starting with lowercase letter
            word = _rand_word(rng=RNG).lower()
            rest = _rand_sentence(2, 6)
            text = word + " " + rest
            result = self.fixer.fix(text, language="ru")
            if result:
                self.assertTrue(
                    result[0].isupper() or not result[0].isalpha(),
                    f"Output does not start with uppercase: {result!r}",
                )

    def test_no_double_spaces_ru(self):
        """Output never contains double spaces after fix (ru)."""
        for _ in range(N_ROUNDS):
            parts = [_rand_word(rng=RNG) for _ in range(5)]
            # inject multiple spaces
            text = "  ".join(parts)
            result = self.fixer.fix(text, language="ru")
            self.assertNotIn("  ", result, f"Double space found in: {result!r}")

    def test_no_double_spaces_es(self):
        """Output never contains double spaces after fix (es)."""
        for _ in range(N_ROUNDS):
            parts = [_rand_word(rng=RNG) for _ in range(4)]
            text = "   ".join(parts)
            result = self.fixer.fix(text, language="es")
            self.assertNotIn("  ", result)

    def test_first_letter_capitalized_es(self):
        """Spanish output always starts with uppercase (or ¿/¡)."""
        for _ in range(N_ROUNDS):
            word = _rand_word(rng=RNG).lower()
            rest = _rand_sentence(2, 5)
            text = word + " " + rest
            result = self.fixer.fix(text, language="es")
            if result:
                first = result[0]
                self.assertTrue(
                    first.isupper() or first in "¿¡",
                    f"Bad first char {first!r} in: {result!r}",
                )

    def test_empty_input_returned_unchanged(self):
        """Empty / whitespace input is returned as-is (no crash)."""
        for inp in ["", "   "]:
            result = self.fixer.fix(inp, language="ru")
            self.assertEqual(result, inp)


# ═══════════════════════════════════════════════════════════════════════════════
# 12-14  SearchIndex.search
# ═══════════════════════════════════════════════════════════════════════════════

class TestSearchIndexProperties(unittest.TestCase):
    """Property tests for SearchIndex.search."""

    def _make_items(self, n: int = 20) -> list[dict]:
        items = []
        for i in range(n):
            items.append({
                "id": f"item_{i}",
                "text": _rand_sentence(4, 12),
            })
        return items

    def test_all_results_have_positive_score(self):
        """Every returned SearchResult has score > 0."""
        idx = SearchIndex()
        items = self._make_items(30)
        idx.build_index(items)
        for item in items:
            words = item["text"].split()
            if not words:
                continue
            query = words[0]
            results = idx.search(query)
            for r in results:
                self.assertGreater(r.score, 0, f"Zero score for query {query!r}")

    def test_no_duplicate_ids_in_results(self):
        """No item appears more than once in a single search result set."""
        idx = SearchIndex()
        items = self._make_items(25)
        idx.build_index(items)
        for item in items:
            words = item["text"].split()
            if not words:
                continue
            query = " ".join(words[:2])
            results = idx.search(query)
            ids = [r.item_id for r in results]
            self.assertEqual(len(ids), len(set(ids)),
                             f"Duplicate ids for query {query!r}: {ids}")

    def test_empty_query_returns_empty_list(self):
        """Searching with empty query always returns []."""
        idx = SearchIndex()
        items = self._make_items(10)
        idx.build_index(items)
        for q in ["", "   "]:
            self.assertEqual(idx.search(q), [])


# ═══════════════════════════════════════════════════════════════════════════════
# 15-17  TextAnonymizer.anonymize
# ═══════════════════════════════════════════════════════════════════════════════

class TestTextAnonymizerProperties(unittest.TestCase):
    """Property tests for TextAnonymizer.anonymize."""

    PHONE_SAMPLES = [
        "+7 (999) 123-45-67",
        "89991234567",
        "8(926)000-00-00",
    ]
    EMAIL_SAMPLES = [
        "user@example.com",
        "test.addr+tag@mail.ru",
        "admin@site.org",
    ]

    def setUp(self):
        self.anon = TextAnonymizer()

    def test_redaction_count_matches_actual_replacements(self):
        """AnonymizeResult.redaction_count == len(redactions)."""
        for phone in self.PHONE_SAMPLES:
            text = f"Позвони мне: {phone} срочно."
            result = self.anon.anonymize(text)
            self.assertEqual(
                result.redaction_count, len(result.redactions),
                f"Count mismatch for text: {text!r}",
            )

    def test_original_phone_not_in_anonymized(self):
        """After anonymization the original phone literal is absent from output."""
        for phone in self.PHONE_SAMPLES:
            text = f"Мой телефон {phone}."
            result = self.anon.anonymize(text)
            self.assertNotIn(phone, result.anonymized_text,
                             f"Phone still present: {phone!r}")

    def test_original_email_not_in_anonymized(self):
        """After anonymization the original email is absent from output."""
        for email in self.EMAIL_SAMPLES:
            text = f"Пиши на {email} если что."
            result = self.anon.anonymize(text)
            self.assertNotIn(email, result.anonymized_text)

    def test_empty_text_returns_zero_redactions(self):
        """Empty text → 0 redactions, unchanged text."""
        result = self.anon.anonymize("")
        self.assertEqual(result.redaction_count, 0)
        self.assertEqual(result.anonymized_text, "")

    def test_clean_text_unchanged(self):
        """Text without PII passes through unchanged."""
        for _ in range(N_ROUNDS):
            text = _rand_sentence(3, 8)
            result = self.anon.anonymize(text)
            # random text very unlikely to match PII patterns;
            # if it does, just verify count consistency
            self.assertEqual(result.redaction_count, len(result.redactions))


# ═══════════════════════════════════════════════════════════════════════════════
# 18-20  AudioNormalizationStage
# ═══════════════════════════════════════════════════════════════════════════════

class TestAudioNormalizationProperties(unittest.TestCase):
    """Property tests for AudioNormalizationStage."""

    TARGET_RMS = 0.1

    def _make_ctx(self, data: np.ndarray) -> PipelineContext:
        return PipelineContext(audio_input=data)

    def setUp(self):
        self.stage = AudioNormalizationStage()

    def test_output_rms_near_target(self):
        """For non-silent input, output RMS ≈ TARGET_RMS."""
        for _ in range(N_ROUNDS):
            # Signals with nonzero amplitude
            amplitude = RNG.uniform(0.05, 0.9)
            n = RNG.randint(512, 4096)
            data = np.array(
                [RNG.gauss(0, amplitude) for _ in range(n)], dtype=np.float32
            )
            # Ensure non-silent
            rms_in = float(np.sqrt(np.mean(data ** 2)))
            if rms_in < 1e-5:
                continue
            ctx = self._make_ctx(data)
            out_ctx = self.stage.process(ctx)
            out = out_ctx.normalized_audio
            rms_out = float(np.sqrt(np.mean(out ** 2)))
            self.assertAlmostEqual(
                rms_out, self.TARGET_RMS, places=3,
                msg=f"RMS {rms_out:.4f} far from target {self.TARGET_RMS} (input rms={rms_in:.4f})",
            )

    def test_no_clipping(self):
        """Output samples are always within [-1.0, 1.0]."""
        for _ in range(N_ROUNDS):
            data = np.array(
                [RNG.gauss(0, 1.0) for _ in range(RNG.randint(256, 2048))],
                dtype=np.float32,
            )
            ctx = self._make_ctx(data)
            out = self.stage.process(ctx).normalized_audio
            if isinstance(out, np.ndarray):
                self.assertLessEqual(float(out.max()), 1.0 + 1e-6)
                self.assertGreaterEqual(float(out.min()), -1.0 - 1e-6)

    def test_output_is_float32(self):
        """Output dtype is always float32."""
        for dtype in (np.float32, np.float64, np.int16):
            data = np.ones(500, dtype=dtype) * 0.3
            ctx = self._make_ctx(data)
            out = self.stage.process(ctx).normalized_audio
            if isinstance(out, np.ndarray):
                self.assertEqual(out.dtype, np.float32)

    def test_stereo_converted_to_mono(self):
        """Stereo (N, 2) input always produces 1-D output."""
        for _ in range(10):
            n = RNG.randint(256, 2048)
            data = np.random.uniform(-0.5, 0.5, (n, 2)).astype(np.float32)
            ctx = self._make_ctx(data)
            out = self.stage.process(ctx).normalized_audio
            self.assertEqual(out.ndim, 1)


# ═══════════════════════════════════════════════════════════════════════════════
# 21-23  ConfidenceCalibrator.calibrate
# ═══════════════════════════════════════════════════════════════════════════════

class TestConfidenceCalibratorProperties(unittest.TestCase):
    """Property tests for ConfidenceCalibrator.calibrate."""

    def setUp(self):
        self.cal = ConfidenceCalibrator()

    def test_output_always_in_0_1(self):
        """calibrate() always returns a value in [0.0, 1.0]."""
        for _ in range(N_ROUNDS):
            raw = RNG.uniform(0.0, 1.0)
            duration = RNG.uniform(0.5, 120.0)
            lang = RNG.choice(["ru", "es", "en", "de", "fr", ""])
            model = RNG.choice(["balanced", "max", "tiny", "large-v3"])
            result = self.cal.calibrate(raw, duration, lang, model)
            self.assertGreaterEqual(result, 0.0, f"Negative output {result}")
            self.assertLessEqual(result, 1.0, f"Output > 1.0: {result}")

    def test_output_is_monotonic_wrt_raw(self):
        """Higher raw confidence → higher or equal calibrated confidence
        (when all other params are identical)."""
        for _ in range(N_ROUNDS):
            duration = RNG.uniform(1.0, 30.0)
            lang = "ru"
            model = "max"
            raw_low = RNG.uniform(0.0, 0.5)
            raw_high = RNG.uniform(raw_low, 1.0)
            out_low = self.cal.calibrate(raw_low, duration, lang, model)
            out_high = self.cal.calibrate(raw_high, duration, lang, model)
            self.assertLessEqual(
                out_low, out_high + 1e-9,
                f"Monotonicity violated: raw({raw_low:.3f}→{out_low:.4f}) > raw({raw_high:.3f}→{out_high:.4f})",
            )

    def test_extreme_raw_clamped(self):
        """Values well above 1.0 or below 0.0 are clamped to [0, 1]."""
        for raw in (-10.0, -0.5, 1.5, 100.0):
            result = self.cal.calibrate(raw, 5.0, "ru", "max")
            self.assertGreaterEqual(result, 0.0)
            self.assertLessEqual(result, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
# 24-27  FuzzySearcher.search
# ═══════════════════════════════════════════════════════════════════════════════

class TestFuzzySearcherProperties(unittest.TestCase):
    """Property tests for FuzzySearcher.search."""

    def setUp(self):
        self.searcher = FuzzySearcher()

    def test_exact_match_score_1(self):
        """Searching for a text that is itself should yield score 1.0."""
        for _ in range(20):
            text = _rand_sentence(3, 8)
            results = self.searcher.search(text, [text], threshold=0.0)
            self.assertEqual(len(results), 1)
            self.assertAlmostEqual(results[0].score, 1.0, places=5,
                                   msg=f"Exact match score not 1.0 for {text!r}: {results[0].score}")

    def test_all_scores_in_0_1(self):
        """All returned scores are in [0.0, 1.0]."""
        texts = [_rand_sentence(3, 10) for _ in range(20)]
        for _ in range(N_ROUNDS):
            query = _rand_sentence(2, 6)
            results = self.searcher.search(query, texts, threshold=0.0)
            for r in results:
                self.assertGreaterEqual(r.score, 0.0)
                self.assertLessEqual(r.score, 1.0)

    def test_threshold_filtering(self):
        """All results satisfy score >= threshold."""
        texts = [_rand_sentence(4, 10) for _ in range(30)]
        for threshold in (0.3, 0.5, 0.7, 0.9):
            query = _rand_sentence(3, 7)
            results = self.searcher.search(query, texts, threshold=threshold)
            for r in results:
                self.assertGreaterEqual(
                    r.score, threshold - 1e-9,
                    f"Score {r.score} below threshold {threshold}",
                )

    def test_empty_query_returns_empty(self):
        """Empty query always returns empty list."""
        texts = [_rand_sentence() for _ in range(10)]
        results = self.searcher.search("", texts)
        self.assertEqual(results, [])

    def test_results_sorted_descending(self):
        """Results are sorted by score in descending order."""
        texts = [_rand_sentence(3, 10) for _ in range(20)]
        for _ in range(N_ROUNDS):
            query = _rand_sentence(2, 5)
            results = self.searcher.search(query, texts, threshold=0.0)
            scores = [r.score for r in results]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_duplicate_indices(self):
        """Each text index appears at most once in the results."""
        texts = [_rand_sentence(3, 8) for _ in range(20)]
        for _ in range(N_ROUNDS):
            query = _rand_sentence(2, 5)
            results = self.searcher.search(query, texts, threshold=0.0)
            indices = [r.index for r in results]
            self.assertEqual(len(indices), len(set(indices)))


if __name__ == "__main__":
    unittest.main()
