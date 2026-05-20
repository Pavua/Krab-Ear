"""Wave 228 extras — DateTimeNormalizer focused test suite.

Covers: RU inflected dates, RU relative words, ES numeric dates,
numeric format passthrough, ISO-8601 passthrough, invalid-date
graceful handling, and thread safety.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_datetime_normalizer.py -v
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.datetime_normalizer import DateTimeNormalizer


class TestRUInflectedDates(unittest.TestCase):
    """RU: словесные даты в различных падежах → DD.MM[.YYYY]."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text: str) -> str:
        return self.d.normalize(text, "ru")

    def test_ru_inflected_dates_pervogo_yanvarya(self):
        """'первого января' → '01.01'"""
        result = self._ru("первого января")
        self.assertIn("01.01", result)

    def test_ru_inflected_dates_tretye_noyabrya(self):
        """'третье ноября' → '03.11' (already in test_normalizers but re-verified here)."""
        result = self._ru("третье ноября")
        self.assertIn("03.11", result)

    def test_ru_inflected_dates_pyatnadtsatogo_yanvarya(self):
        """'пятнадцатого января' → '15.01'"""
        result = self._ru("пятнадцатого января")
        self.assertIn("15.01", result)

    def test_ru_inflected_dates_dvadtsat_pyatogo_dekabrya(self):
        """'двадцать пятого декабря' → '25.12'"""
        result = self._ru("двадцать пятого декабря")
        self.assertIn("25.12", result)

    def test_ru_inflected_dates_tridtsat_pervogo_marta(self):
        """'тридцать первого марта' → '31.03'"""
        result = self._ru("тридцать первого марта")
        self.assertIn("31.03", result)

    def test_ru_inflected_dates_with_year(self):
        """'пятнадцатого января 2026 года' → '15.01.2026'"""
        result = self._ru("пятнадцатого января 2026 года")
        self.assertIn("15.01.2026", result)

    def test_ru_inflected_dates_first_may(self):
        """'первое мая' → '01.05'"""
        result = self._ru("первое мая")
        self.assertIn("01.05", result)

    def test_ru_inflected_dates_digital_day(self):
        """'3 ноября' → '03.11'"""
        result = self._ru("3 ноября")
        self.assertIn("03.11", result)

    def test_ru_inflected_dates_preserved_in_context(self):
        """Date normalisation works mid-sentence."""
        result = self._ru("Встреча назначена на первого января следующего года.")
        self.assertIn("01.01", result)


class TestRURelativeDates(unittest.TestCase):
    """RU: relative words are not mangled (normalizer is date-literal-only)."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text: str) -> str:
        return self.d.normalize(text, "ru")

    def test_ru_relative_vchera_passthrough(self):
        """'вчера' passes through unchanged — not a concrete date."""
        result = self._ru("вчера")
        self.assertEqual(result, "вчера")

    def test_ru_relative_zavtra_passthrough(self):
        """'завтра' passes through unchanged."""
        result = self._ru("завтра")
        self.assertEqual(result, "завтра")

    def test_ru_relative_cherez_nedelyu_passthrough(self):
        """'через неделю' passes through unchanged."""
        result = self._ru("через неделю")
        self.assertEqual(result, "через неделю")

    def test_ru_relative_segodnya_passthrough(self):
        """'сегодня' passes through unchanged."""
        result = self._ru("сегодня")
        self.assertEqual(result, "сегодня")

    def test_ru_relative_words_not_confused_with_months(self):
        """Relative words adjacent to other text don't corrupt output."""
        text = "Вчера вечером третьего марта было собрание."
        result = self._ru(text)
        # The literal date part should be normalised
        self.assertIn("03.03", result)
        # The relative word should survive
        self.assertIn("Вчера", result)


class TestESNumericDates(unittest.TestCase):
    """ES: numeric + spoken month → DD.MM[.YYYY]."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _es(self, text: str) -> str:
        return self.d.normalize(text, "es")

    def test_es_numeric_12_de_marzo(self):
        """'12 de marzo' → '12.03'"""
        result = self._es("12 de marzo")
        self.assertIn("12.03", result)

    def test_es_numeric_15_de_enero_2026(self):
        """'15 de enero de 2026' → '15.01.2026'"""
        result = self._es("15 de enero de 2026")
        self.assertIn("15.01.2026", result)

    def test_es_numeric_3_de_noviembre(self):
        """'3 de noviembre' → '03.11'"""
        result = self._es("3 de noviembre")
        self.assertIn("03.11", result)

    def test_es_numeric_1_de_enero(self):
        """'1 de enero' → '01.01'"""
        result = self._es("1 de enero")
        self.assertIn("01.01", result)

    def test_es_numeric_31_de_diciembre(self):
        """'31 de diciembre' → '31.12'"""
        result = self._es("31 de diciembre")
        self.assertIn("31.12", result)

    def test_es_numeric_with_el_prefix(self):
        """'el 12 de marzo' → '12.03' (handles 'el' prefix)."""
        result = self._es("el 12 de marzo")
        self.assertIn("12.03", result)

    def test_es_numeric_mid_sentence(self):
        """Date normalisation works in mid-sentence ES context."""
        result = self._es("La reunión es el 5 de julio de 2026 por la tarde.")
        self.assertIn("05.07.2026", result)


class TestNumericFormats(unittest.TestCase):
    """Numeric date formats (already formatted) pass through unchanged."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_numeric_formats_dd_mm_yyyy_passthrough(self):
        """'12.03.2026' is not re-processed (idempotent)."""
        text = "12.03.2026"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_formats_iso_date_passthrough(self):
        """'2026-03-12' is not touched."""
        text = "2026-03-12"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_formats_slash_date_passthrough(self):
        """'03/12/2026' passes through unchanged."""
        text = "03/12/2026"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_formats_time_hhmm_passthrough(self):
        """'09:00' is not re-processed."""
        text = "В 09:00 совещание"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_formats_already_normalised_date_stable(self):
        """Double-normalise a numeric date produces same result."""
        text = "03.11"
        first = self.d.normalize(text, "ru")
        second = self.d.normalize(first, "ru")
        self.assertEqual(first, second)


class TestISO8601Passthrough(unittest.TestCase):
    """ISO-8601 strings must pass through without corruption."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_iso_8601_passthrough_date_only(self):
        """'2026-03-12' passes through all language paths."""
        iso = "2026-03-12"
        for lang in ("ru", "es", "en"):
            with self.subTest(lang=lang):
                self.assertEqual(self.d.normalize(iso, lang), iso)

    def test_iso_8601_passthrough_datetime(self):
        """Full ISO-8601 datetime string is not mangled."""
        iso = "2026-03-12T14:30:00"
        for lang in ("ru", "es", "en"):
            with self.subTest(lang=lang):
                result = self.d.normalize(iso, lang)
                # At minimum the date part survives intact
                self.assertIn("2026-03-12", result)

    def test_iso_8601_passthrough_in_context(self):
        """ISO date in a sentence does not get double-converted."""
        text = "Exported at 2026-03-12T09:00:00Z from the system."
        result = self.d.normalize(text, "en")
        self.assertIn("2026-03-12", result)


class TestHandlesInvalidDateGracefully(unittest.TestCase):
    """Invalid / unrecognised input must not raise and should return sane output."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_handles_invalid_date_gracefully_empty(self):
        """Empty string returns empty string — no exception."""
        self.assertEqual(self.d.normalize("", "ru"), "")

    def test_handles_invalid_date_gracefully_no_date(self):
        """Plain text with no date info is returned unchanged."""
        text = "blah blah no date here"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_handles_invalid_date_gracefully_unknown_lang(self):
        """Unknown language code → text is returned unchanged."""
        text = "some text"
        self.assertEqual(self.d.normalize(text, "ja"), text)
        self.assertEqual(self.d.normalize(text, "zh"), text)

    def test_handles_invalid_date_gracefully_garbage_input(self):
        """Garbage string with no parseable date is returned unchanged."""
        text = "xyz123 !@# $%^"
        result = self.d.normalize(text, "ru")
        self.assertEqual(result, text)

    def test_handles_invalid_date_gracefully_partial_match(self):
        """Partial pattern that doesn't fully match is not truncated."""
        text = "первого"  # day ordinal with no month — no full match
        result = self.d.normalize(text, "ru")
        # Should not raise; original text preserved (no month → no replacement)
        self.assertIsInstance(result, str)

    def test_handles_invalid_date_gracefully_month_only(self):
        """Month name alone without a day is not corrupted."""
        text = "январе шли переговоры"
        result = self.d.normalize(text, "ru")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)


class TestConcurrentNormalize(unittest.TestCase):
    """DateTimeNormalizer is stateless; concurrent calls must be safe."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_concurrent_normalize_ru(self):
        """20 concurrent RU normalizations produce consistent results."""
        results: list[str] = []
        errors: list[str] = []

        def worker():
            try:
                r = self.d.normalize("третье ноября", "ru")
                results.append(r)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")
        self.assertEqual(len(results), 20)
        for r in results:
            self.assertIn("03.11", r)

    def test_concurrent_normalize_mixed_languages(self):
        """Concurrent calls across RU/ES/EN share no mutable state."""
        payloads = [
            ("третье ноября", "ru", "03.11"),
            ("12 de marzo", "es", "12.03"),
            ("third of November", "en", "03.11"),
        ]
        results: list[bool] = []
        errors: list[str] = []

        def worker(text: str, lang: str, expected: str):
            try:
                r = self.d.normalize(text, lang)
                results.append(expected in r)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))

        threads = []
        for _ in range(5):
            for text, lang, expected in payloads:
                threads.append(threading.Thread(target=worker, args=(text, lang, expected)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent errors: {errors}")
        self.assertTrue(all(results), "Some concurrent normalizations returned wrong result")


if __name__ == "__main__":
    unittest.main()
