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


if __name__ == "__main__":
    unittest.main()
