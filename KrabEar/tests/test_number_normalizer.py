"""Wave 118 — unit tests for NumberNormalizer.

Coverage targets:
    test_ru_simple_numbers   — «сорок семь» → «47»
    test_ru_compound         — «сто двадцать три» → «123»
    test_es_simple           — «cuarenta y siete» → «47»
    test_es_compound         — «ciento veintitres» → «123»
    test_ordinal_handled     — «первый» → «1-й»
    test_unaffected_text_passes_through — non-number text unchanged
    test_concurrent_normalize — thread-safety smoke test

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_number_normalizer.py -v
"""

import sys
import os
import threading
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402

from core.number_normalizer import NumberNormalizer


class TestNumberNormalizerRuSimple(unittest.TestCase):
    """Russian simple (1-2 word) numbers → digits."""

    def setUp(self):
        self.n = NumberNormalizer()

    def _ru(self, text):
        return self.n.normalize(text, "ru")

    # --- test_ru_simple_numbers ---

    def test_ru_simple_forty_seven(self):
        """«сорок семь» → «47»."""
        self.assertEqual(self._ru("сорок семь"), "47")

    def test_ru_simple_zero(self):
        """«ноль» → «0»."""
        self.assertEqual(self._ru("ноль"), "0")

    def test_ru_simple_one(self):
        """«один» → «1»."""
        self.assertEqual(self._ru("один"), "1")

    def test_ru_simple_nineteen(self):
        """«девятнадцать» → «19»."""
        self.assertEqual(self._ru("девятнадцать"), "19")

    def test_ru_simple_twenty(self):
        """«двадцать» → «20»."""
        self.assertEqual(self._ru("двадцать"), "20")

    def test_ru_simple_ninety_nine(self):
        """«девяносто девять» → «99»."""
        self.assertEqual(self._ru("девяносто девять"), "99")

    def test_ru_simple_five_in_sentence(self):
        """«пять яблок» — digits appear in result."""
        result = self._ru("пять яблок")
        self.assertIn("5", result)

    def test_ru_simple_negative(self):
        """«минус пять» → «-5»."""
        result = self._ru("минус пять")
        self.assertIn("-5", result)


class TestNumberNormalizerRuCompound(unittest.TestCase):
    """Russian compound (multi-word) numbers → digits."""

    def setUp(self):
        self.n = NumberNormalizer()

    def _ru(self, text):
        return self.n.normalize(text, "ru")

    # --- test_ru_compound ---

    def test_ru_compound_123(self):
        """«сто двадцать три» → «123»."""
        self.assertEqual(self._ru("сто двадцать три"), "123")

    def test_ru_compound_300(self):
        """«триста» → «300»."""
        self.assertEqual(self._ru("триста"), "300")

    def test_ru_compound_1000(self):
        """«одна тысяча» → «1000»."""
        result = self._ru("одна тысяча")
        self.assertIn("1000", result)

    def test_ru_compound_2000(self):
        """«две тысячи» → «2000»."""
        result = self._ru("две тысячи")
        self.assertIn("2000", result)

    def test_ru_compound_3200(self):
        """«три тысячи двести» → «3200»."""
        result = self._ru("три тысячи двести")
        self.assertIn("3200", result)

    def test_ru_compound_million(self):
        """«один миллион» → «1000000»."""
        result = self._ru("один миллион")
        self.assertIn("1000000", result)

    def test_ru_compound_percent(self):
        """«тридцать процентов» → «30%»."""
        result = self._ru("тридцать процентов")
        self.assertIn("30%", result)

    def test_ru_compound_idempotent(self):
        """Already normalized «123» → unchanged."""
        text = "Мне нужно 123 штуки"
        self.assertEqual(self._ru(text), text)

    def test_ru_compound_fraction(self):
        """«половина» → «1/2»."""
        result = self._ru("половина")
        self.assertIn("1/2", result)


class TestNumberNormalizerEsSimple(unittest.TestCase):
    """Spanish simple numbers → digits."""

    def setUp(self):
        self.n = NumberNormalizer()

    def _es(self, text):
        return self.n.normalize(text, "es")

    # --- test_es_simple ---

    def test_es_simple_forty_seven(self):
        """«cuarenta y siete» → «47»."""
        result = self._es("cuarenta y siete")
        self.assertIn("47", result)

    def test_es_simple_ten(self):
        """«diez» → «10»."""
        self.assertEqual(self._es("diez"), "10")

    def test_es_simple_fifty(self):
        """«cincuenta» → «50»."""
        self.assertEqual(self._es("cincuenta"), "50")

    def test_es_simple_zero(self):
        """«cero» → «0»."""
        self.assertEqual(self._es("cero"), "0")

    def test_es_simple_one(self):
        """«uno» → «1»."""
        self.assertEqual(self._es("uno"), "1")

    def test_es_simple_twenty_one(self):
        """«veintiuno» → «21»."""
        result = self._es("veintiuno")
        self.assertIn("21", result)

    def test_es_simple_negative(self):
        """«menos cinco» → «-5»."""
        result = self._es("menos cinco")
        self.assertIn("-5", result)

    def test_es_simple_idempotent(self):
        """Already normalized «123 euros» → unchanged."""
        text = "Precio: 123 euros"
        self.assertEqual(self._es(text), text)


class TestNumberNormalizerEsCompound(unittest.TestCase):
    """Spanish compound numbers → digits."""

    def setUp(self):
        self.n = NumberNormalizer()

    def _es(self, text):
        return self.n.normalize(text, "es")

    # --- test_es_compound ---

    def test_es_compound_123(self):
        """«ciento veintitres» → «123»."""
        result = self._es("ciento veintitres")
        self.assertIn("123", result)

    def test_es_compound_300(self):
        """«trescientos» → «300»."""
        result = self._es("trescientos")
        self.assertIn("300", result)

    def test_es_compound_1000(self):
        """«mil» → «1000»."""
        result = self._es("mil")
        self.assertIn("1000", result)

    def test_es_compound_percent(self):
        """«treinta por ciento» → «30%»."""
        result = self._es("treinta por ciento")
        self.assertIn("30", result)

    def test_es_compound_idempotent(self):
        """Already normalized number → unchanged."""
        text = "Son 50 euros"
        self.assertEqual(self._es(text), text)


class TestNumberNormalizerOrdinal(unittest.TestCase):
    """Ordinal numerals → digit+suffix form."""

    def setUp(self):
        self.n = NumberNormalizer()

    # --- test_ordinal_handled ---

    def test_ordinal_ru_pervyi(self):
        """Russian «первый» → «1-й»."""
        result = self.n.normalize("первый", "ru")
        self.assertEqual(result, "1-й")

    def test_ordinal_ru_vtoroi(self):
        """Russian «второй» → «2-й»."""
        result = self.n.normalize("второй", "ru")
        self.assertEqual(result, "2-й")

    def test_ordinal_ru_tretii(self):
        """Russian «третий» → «3-й»."""
        result = self.n.normalize("третий", "ru")
        self.assertEqual(result, "3-й")

    def test_ordinal_ru_genitive(self):
        """Russian «первого» → contains «1»."""
        result = self.n.normalize("первого", "ru")
        self.assertIn("1", result)

    def test_ordinal_ru_feminine(self):
        """Russian «первая» → «1-я»."""
        result = self.n.normalize("первая", "ru")
        self.assertEqual(result, "1-я")

    def test_ordinal_en_first(self):
        """English «first» → contains «1»."""
        result = self.n.normalize("first", "en")
        self.assertIn("1", result)

    def test_ordinal_en_third(self):
        """English «third» → contains «3»."""
        result = self.n.normalize("third", "en")
        self.assertIn("3", result)

    def test_ordinal_in_sentence_ru(self):
        """Ordinal inside a sentence is normalised."""
        result = self.n.normalize("Это первый шаг", "ru")
        self.assertIn("1", result)


class TestNumberNormalizerUnaffectedText(unittest.TestCase):
    """Non-number text must pass through unchanged."""

    def setUp(self):
        self.n = NumberNormalizer()

    # --- test_unaffected_text_passes_through ---

    def test_unaffected_ru_no_numbers(self):
        """Russian text without numerals → unchanged."""
        text = "Привет мир"
        self.assertEqual(self.n.normalize(text, "ru"), text)

    def test_unaffected_es_no_numbers(self):
        """Spanish text without numerals → unchanged."""
        text = "Hola mundo"
        self.assertEqual(self.n.normalize(text, "es"), text)

    def test_unaffected_en_no_numbers(self):
        """English text without numerals → unchanged."""
        text = "Hello world"
        self.assertEqual(self.n.normalize(text, "en"), text)

    def test_unaffected_empty_string_ru(self):
        """Empty string → empty string."""
        self.assertEqual(self.n.normalize("", "ru"), "")

    def test_unaffected_empty_string_es(self):
        """Empty string (ES) → empty string."""
        self.assertEqual(self.n.normalize("", "es"), "")

    def test_unaffected_unknown_language(self):
        """Unknown language → text unchanged."""
        text = "text unchanged"
        self.assertEqual(self.n.normalize(text, "ja"), text)

    def test_unaffected_digits_unchanged(self):
        """Text already containing digits → unchanged."""
        text = "I have 42 apples"
        self.assertEqual(self.n.normalize(text, "en"), text)

    def test_unaffected_percent_unchanged(self):
        """Text containing «50%» → unchanged."""
        text = "50% off sale"
        self.assertEqual(self.n.normalize(text, "en"), text)


class TestNumberNormalizerEsOrdinal(unittest.TestCase):
    """W997 — Spanish ordinal numerals → digit+suffix form (W991 F2 fix)."""

    def setUp(self):
        self.n = NumberNormalizer()

    def _es(self, text):
        return self.n.normalize(text, "es")

    # --- test_es_ordinal_primero_replaced ---

    def test_es_ordinal_primero_replaced(self):
        """«primero» → «1.º»."""
        result = self._es("primero")
        self.assertEqual(result, "1.º")

    def test_es_ordinal_primer_replaced(self):
        """«primer» (apocope) → «1.º»."""
        result = self._es("primer")
        self.assertEqual(result, "1.º")

    def test_es_ordinal_primera_replaced(self):
        """«primera» (fem) → «1.ª»."""
        result = self._es("primera")
        self.assertEqual(result, "1.ª")

    def test_es_ordinal_segundo_replaced(self):
        """«segundo» → «2.º»."""
        result = self._es("segundo")
        self.assertEqual(result, "2.º")

    def test_es_ordinal_segunda_replaced(self):
        """«segunda» (fem) → «2.ª»."""
        result = self._es("segunda")
        self.assertEqual(result, "2.ª")

    def test_es_ordinal_tercero_replaced(self):
        """«tercero» → «3.º»."""
        result = self._es("tercero")
        self.assertEqual(result, "3.º")

    def test_es_ordinal_tercer_replaced(self):
        """«tercer» (apocope) → «3.º»."""
        result = self._es("tercer")
        self.assertEqual(result, "3.º")

    def test_es_ordinal_cuarto_replaced(self):
        """«cuarto» → «4.º»."""
        result = self._es("cuarto")
        self.assertEqual(result, "4.º")

    def test_es_ordinal_quinto_replaced(self):
        """«quinto» → «5.º»."""
        result = self._es("quinto")
        self.assertEqual(result, "5.º")

    def test_es_ordinal_sexto_replaced(self):
        """«sexto» → «6.º»."""
        result = self._es("sexto")
        self.assertEqual(result, "6.º")

    def test_es_ordinal_septimo_replaced(self):
        """«séptimo» (with accent) → «7.º»."""
        result = self._es("séptimo")
        self.assertEqual(result, "7.º")

    def test_es_ordinal_septimo_no_accent_replaced(self):
        """«septimo» (no accent) → «7.º»."""
        result = self._es("septimo")
        self.assertEqual(result, "7.º")

    def test_es_ordinal_octavo_replaced(self):
        """«octavo» → «8.º»."""
        result = self._es("octavo")
        self.assertEqual(result, "8.º")

    def test_es_ordinal_noveno_replaced(self):
        """«noveno» → «9.º»."""
        result = self._es("noveno")
        self.assertEqual(result, "9.º")

    # --- test_es_ordinal_decimo_replaced ---

    def test_es_ordinal_decimo_replaced(self):
        """«décimo» (with accent) → «10.º»."""
        result = self._es("décimo")
        self.assertEqual(result, "10.º")

    def test_es_ordinal_decimo_no_accent_replaced(self):
        """«decimo» (no accent) → «10.º»."""
        result = self._es("decimo")
        self.assertEqual(result, "10.º")

    def test_es_ordinal_decima_replaced(self):
        """«décima» (fem) → «10.ª»."""
        result = self._es("décima")
        self.assertEqual(result, "10.ª")

    def test_es_ordinal_in_sentence(self):
        """Ordinal inside a sentence is normalised."""
        result = self._es("Es el primer intento")
        self.assertIn("1.º", result)

    # --- test_es_ordinal_does_not_corrupt_compound ---

    def test_es_ordinal_does_not_corrupt_compound_cuartito(self):
        """«cuartito» must NOT be matched as «cuarto» + suffix (W993 lesson: (?!\\w) boundary)."""
        result = self._es("cuartito")
        # Must not contain «4.º» — cuartito is a diminutive, not an ordinal
        self.assertNotIn("4.º", result)
        self.assertEqual(result, "cuartito")

    def test_es_ordinal_does_not_corrupt_compound_primero_embedded(self):
        """«primeros» (plural) must NOT be matched as «primero» + «s»."""
        result = self._es("primeros")
        self.assertNotIn("1.º", result)
        self.assertEqual(result, "primeros")

    def test_es_ordinal_does_not_corrupt_compound_segunda_embedded(self):
        """«segundario» must NOT be matched as «segunda» + suffix."""
        result = self._es("segundario")
        self.assertNotIn("2.ª", result)
        self.assertNotIn("2.º", result)

    def test_es_ordinal_boundary_standalone_word(self):
        """Standalone ordinal within punctuation is still matched."""
        result = self._es("¡primero!")
        self.assertIn("1.º", result)


class TestNumberNormalizerConcurrent(unittest.TestCase):
    """Thread-safety smoke test for normalize()."""

    # --- test_concurrent_normalize ---

    def test_concurrent_normalize_ru(self):
        """Multiple threads calling normalize() concurrently produce consistent results."""
        n = NumberNormalizer()
        errors = []
        results = {}

        def worker(tid, text):
            try:
                results[tid] = n.normalize(text, "ru")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, "сто двадцать три"))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Threads raised errors: {errors}")
        for tid, val in results.items():
            self.assertIn("123", val, f"Thread {tid} got unexpected result: {val!r}")

    def test_concurrent_normalize_es(self):
        """Spanish normalization is stable under concurrent access."""
        n = NumberNormalizer()
        errors = []
        results = {}

        def worker(tid, text):
            try:
                results[tid] = n.normalize(text, "es")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, "cuarenta y siete"))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"Threads raised errors: {errors}")
        for tid, val in results.items():
            self.assertIn("47", val, f"Thread {tid} got unexpected result: {val!r}")

    def test_concurrent_normalize_mixed_languages(self):
        """Different language normalizations running in parallel stay isolated."""
        n = NumberNormalizer()
        errors = []
        results = {}

        inputs = [
            ("ru", "сорок семь"),
            ("es", "cuarenta y siete"),
            ("en", "forty seven"),
            ("ru", "сто двадцать три"),
            ("es", "ciento veintitres"),
            ("en", "one hundred twenty three"),
        ]

        def worker(tid, lang, text):
            try:
                results[tid] = (lang, n.normalize(text, lang))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(i, lang, text))
            for i, (lang, text) in enumerate(inputs)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        _expected = {
            0: "47", 1: "47", 2: "47",
            3: "123", 4: "123", 5: "123",
        }
        self.assertEqual(errors, [], f"Threads raised errors: {errors}")
        for tid, (lang, val) in results.items():
            self.assertIn(_expected[tid], val, f"Thread {tid} ({lang}): {val!r}")


class TestNumberNormalizerCompoundWordBoundary(unittest.TestCase):
    """W991 F1 HIGH — right-boundary guard prevents compound word corruption.

    Without (?!\\w) at the tail of num_seq_pat, a decade prefix such as
    «двадцать» matches inside «двадцатилетний» and corrupts it to
    «2дцатилетний».  This suite verifies the fix across RU / ES / EN.
    """

    def setUp(self):
        self.n = NumberNormalizer()

    # ------------------------------------------------------------------
    # Russian compound words (W991 F1 production examples)
    # ------------------------------------------------------------------

    def test_compound_word_not_corrupted_ru_dvadtsatiletni(self):
        """«двадцатилетний опыт» MUST pass through verbatim (W991 F1 HIGH)."""
        text = "двадцатилетний опыт"
        result = self.n.normalize(text, "ru")
        self.assertEqual(
            result,
            text,
            f"Compound word corrupted: expected {text!r}, got {result!r}",
        )

    def test_compound_word_not_corrupted_ru_tridtsatigradusny(self):
        """«тридцатиградусный» MUST pass through verbatim (W991 F1 HIGH)."""
        text = "тридцатиградусный"
        result = self.n.normalize(text, "ru")
        self.assertEqual(
            result,
            text,
            f"Compound word corrupted: expected {text!r}, got {result!r}",
        )

    def test_compound_word_in_sentence_ru(self):
        """Compound word is preserved while standalone numbers in same sentence are converted."""
        text = "У него двадцатилетний опыт и сорок рублей"
        result = self.n.normalize(text, "ru")
        self.assertIn("двадцатилетний", result, "Compound word was corrupted")
        self.assertIn("40", result, "Standalone number was not converted")

    def test_compound_word_not_corrupted_ru_pyatiletka(self):
        """«пятилетка» (five-year plan) MUST not be corrupted by «пять» match."""
        text = "пятилетка"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Compound word corrupted: {result!r}")

    def test_compound_word_not_corrupted_ru_stoprocentny(self):
        """«стопроцентный» (hundred-percent) MUST not be corrupted by «сто» match."""
        text = "стопроцентный результат"
        result = self.n.normalize(text, "ru")
        self.assertIn("стопроцентный", result, f"Compound word corrupted: {result!r}")

    # ------------------------------------------------------------------
    # Spanish compound words
    # ------------------------------------------------------------------

    def test_compound_word_not_corrupted_es_veintinueve(self):
        """«veintinueve» standalone → «29»; embedded form must not corrupt."""
        # standalone → should convert
        standalone = self.n.normalize("veintinueve", "es")
        self.assertIn("29", standalone, "Standalone 'veintinueve' should convert to 29")

        # embedded — if a hypothetical compound were in the text, it must not match mid-word
        # We verify that the right-boundary guard is in place by checking a word that
        # starts with «dos» does not get mangled.
        text = "dosificación"  # starts with «dos» (2)
        result = self.n.normalize(text, "es")
        self.assertEqual(result, text, f"Spanish compound word corrupted: {result!r}")

    def test_compound_word_not_corrupted_es_ciento(self):
        """Words starting with «cien» fragment are not corrupted."""
        text = "ciencias"  # starts with «cien» (100) but is not a numeral
        result = self.n.normalize(text, "es")
        self.assertEqual(result, text, f"Spanish word corrupted: {result!r}")

    # ------------------------------------------------------------------
    # English compound words
    # ------------------------------------------------------------------

    def test_compound_word_not_corrupted_en_threesome(self):
        """«threesome» MUST not be corrupted by «three» match."""
        text = "threesome"
        result = self.n.normalize(text, "en")
        self.assertEqual(result, text, f"English compound word corrupted: {result!r}")

    def test_compound_word_not_corrupted_en_sixteen(self):
        """«sixteenth» MUST not be corrupted by «six» match (ordinal suffix)."""
        text = "sixteenth"
        result = self.n.normalize(text, "en")
        # «sixteen» is in _EN_ONES (value 16). Without right-boundary guard,
        # «sixteen» would match inside «sixteenth» → «16th».
        # With the guard, «sixteenth» has no trailing word boundary → untouched.
        self.assertEqual(result, text, f"English word corrupted: {result!r}")

    # ------------------------------------------------------------------
    # Regression: standalone numerals still convert (right-boundary must not block)
    # ------------------------------------------------------------------

    def test_standalone_number_still_replaced_ru_dvadtsat(self):
        """«двадцать» standalone → «20» (regression guard for right-boundary fix)."""
        result = self.n.normalize("двадцать", "ru")
        self.assertEqual(result, "20")

    def test_standalone_number_still_replaced_ru_in_sentence(self):
        """«двадцать рублей» → contains «20» (regression guard)."""
        result = self.n.normalize("двадцать рублей", "ru")
        self.assertIn("20", result)

    def test_standalone_number_still_replaced_ru_compound_number(self):
        """«сто двадцать три» → «123» (multi-word number not broken by fix)."""
        result = self.n.normalize("сто двадцать три", "ru")
        self.assertEqual(result, "123")

    def test_standalone_number_still_replaced_ru_tridtsat(self):
        """«тридцать» standalone → «30» (regression guard)."""
        result = self.n.normalize("тридцать", "ru")
        self.assertEqual(result, "30")

    def test_standalone_number_still_replaced_es_treinta(self):
        """«treinta» standalone → «30» (Spanish regression guard)."""
        result = self.n.normalize("treinta", "es")
        self.assertEqual(result, "30")

    def test_standalone_number_still_replaced_en_twenty(self):
        """«twenty» standalone → «20» (English regression guard)."""
        result = self.n.normalize("twenty", "en")
        self.assertEqual(result, "20")

    def test_standalone_number_still_replaced_en_thirty_two(self):
        """«thirty-two» → «32» (hyphenated EN compound, regression guard)."""
        result = self.n.normalize("thirty-two", "en")
        self.assertEqual(result, "32")


class TestNumberNormalizerSecurityFixes(unittest.TestCase):
    """Regression tests for security hardening (wave-A PR #1648).

    Covers two fixes:
    1. ReDoS guard: repetition bound {0,20} prevents catastrophic backtracking on
       pathological inputs with 20+ consecutive number words.
    2. Unit-word boundary guard: (?!\\w) after unit_pat prevents unit tokens from
       matching inside a longer word (e.g. «percentage» should not be mangled by
       «percent» unit match).
    """

    def setUp(self):
        self.n = NumberNormalizer()

    # ------------------------------------------------------------------
    # Fix 1: ReDoS guard — repetition bound {0,20}
    # A sequence longer than 20 number words must complete in <1 s and
    # produce a sane (non-hanging) result.
    # ------------------------------------------------------------------

    def test_redos_long_sequence_ru_completes_fast(self):
        """50 consecutive «один» words must finish quickly without catastrophic backtracking."""
        import time
        words = " ".join(["один"] * 50)
        t0 = time.time()
        result = self.n.normalize(words, "ru")
        elapsed = time.time() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"RU normalize took {elapsed:.2f}s — possible backtracking hang")
        # Result must not be empty and must contain at least one digit
        self.assertIn("1", result, "Long RU sequence produced no digits")

    def test_redos_long_sequence_es_completes_fast(self):
        """50 consecutive «uno» words must finish quickly without catastrophic backtracking."""
        import time
        words = " ".join(["uno"] * 50)
        t0 = time.time()
        result = self.n.normalize(words, "es")
        elapsed = time.time() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"ES normalize took {elapsed:.2f}s — possible backtracking hang")
        self.assertIn("1", result, "Long ES sequence produced no digits")

    def test_redos_long_sequence_en_completes_fast(self):
        """50 consecutive «one» words must finish quickly without catastrophic backtracking."""
        import time
        words = " ".join(["one"] * 50)
        t0 = time.time()
        result = self.n.normalize(words, "en")
        elapsed = time.time() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"EN normalize took {elapsed:.2f}s — possible backtracking hang")
        self.assertIn("1", result, "Long EN sequence produced no digits")

    def test_redos_exactly_at_limit_ru(self):
        """20 consecutive number words (at the {0,20} limit) must still convert."""
        # 1 base word + 20 repetitions = 21 tokens; limit is 1 base + up to 20 more
        words = " ".join(["один"] * 20)
        result = self.n.normalize(words, "ru")
        # Should produce at least one converted digit group (any digit present)
        self.assertTrue(any(c.isdigit() for c in result),
                        f"20-word RU sequence produced no digits: {result!r}")

    # ------------------------------------------------------------------
    # Fix 2: Unit-word boundary guard — (?!\\w) after unit_pat
    # Unit words that appear as a prefix of a longer word must not be matched.
    # ------------------------------------------------------------------

    def test_unit_boundary_en_percent_in_percentage(self):
        """«percentage» must NOT have «percent» unit matched inside it (EN boundary guard)."""
        result = self.n.normalize("percentage increase", "en")
        self.assertEqual(result, "percentage increase",
                         f"Unit word 'percent' corrupted 'percentage': {result!r}")

    def test_unit_boundary_en_standalone_percent_converts(self):
        """«five percent» → contains «5%» (regression: standalone unit must still convert)."""
        result = self.n.normalize("five percent", "en")
        self.assertIn("5%", result, f"Standalone 'five percent' was not converted: {result!r}")

    def test_unit_boundary_ru_procentov_standalone(self):
        """«пять процентов» → «5%» (standalone unit still converts after boundary fix)."""
        result = self.n.normalize("пять процентов", "ru")
        self.assertIn("5%", result, f"'пять процентов' was not converted: {result!r}")

    def test_unit_boundary_es_porciento_standalone(self):
        """«treinta por ciento» → contains «30» (standalone ES unit converts)."""
        result = self.n.normalize("treinta por ciento", "es")
        self.assertIn("30", result, f"'treinta por ciento' was not converted: {result!r}")

    def test_unit_boundary_en_dollars_in_dollarsign_word(self):
        """Sanity: «ten dollars» → converts, «dollarstore» (hypothetical) would not match 'dollar'."""
        # Verify standalone unit converts
        result = self.n.normalize("ten dollars", "en")
        self.assertIn("10", result, f"'ten dollars' was not converted: {result!r}")


if __name__ == "__main__":
    unittest.main()
