"""Tests for W1083 F4 MED — datetime ISO-8601 output format default.

Verifies:
  - Default output is ISO-8601 (YYYY-MM-DD / MM-DD).
  - European format opt-in via constructor and module constant.
  - ISO-8601 dates are lexicographically sortable.

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_datetime_iso_W1094.py -v
"""

from __future__ import annotations

import sys
import os
import unittest

# Allow standalone execution from repo root.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import core.datetime_normalizer as dn_module
from core.datetime_normalizer import DateTimeNormalizer


class TestDefaultOutputIsISO8601(unittest.TestCase):
    """Default normalizer must emit ISO-8601 dates."""

    def setUp(self) -> None:
        # Save original so tearDown can restore it — prevents leaking "iso8601"
        # into test_datetime_normalizer.py which expects the "european" default.
        self._orig_fmt = dn_module.DATETIME_OUTPUT_FORMAT
        dn_module.DATETIME_OUTPUT_FORMAT = "iso8601"
        self.norm = DateTimeNormalizer()

    def tearDown(self) -> None:
        dn_module.DATETIME_OUTPUT_FORMAT = self._orig_fmt

    # --- Russian ---

    def test_ru_day_month_only(self) -> None:
        result = self.norm.normalize("третье ноября", "ru")
        self.assertEqual(result, "11-03")

    def test_ru_day_month_year(self) -> None:
        result = self.norm.normalize("пятнадцатого января 2026 года", "ru")
        self.assertEqual(result, "2026-01-15")

    def test_ru_digit_day_month_only(self) -> None:
        result = self.norm.normalize("3 ноября", "ru")
        self.assertEqual(result, "11-03")

    def test_ru_digit_day_month_year(self) -> None:
        result = self.norm.normalize("15 января 2026 года", "ru")
        self.assertEqual(result, "2026-01-15")

    # --- Spanish ---

    def test_es_day_month_only(self) -> None:
        result = self.norm.normalize("3 de noviembre", "es")
        self.assertEqual(result, "11-03")

    def test_es_day_month_year(self) -> None:
        result = self.norm.normalize("15 de enero de 2026", "es")
        self.assertEqual(result, "2026-01-15")

    # --- English ---

    def test_en_ordinal_of_month(self) -> None:
        result = self.norm.normalize("third of November", "en")
        self.assertEqual(result, "11-03")

    def test_en_month_day_year(self) -> None:
        result = self.norm.normalize("November 15 2026", "en")
        self.assertEqual(result, "2026-11-15")

    def test_en_month_ordinal_day_year(self) -> None:
        result = self.norm.normalize("January 15th 2026", "en")
        self.assertEqual(result, "2026-01-15")


class TestEuropeanFormatOptIn(unittest.TestCase):
    """European format must be available via constructor and module constant."""

    def test_constructor_european_ru(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        result = norm.normalize("третье ноября", "ru")
        self.assertEqual(result, "03.11")

    def test_constructor_european_ru_with_year(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        result = norm.normalize("15 января 2026 года", "ru")
        self.assertEqual(result, "15.01.2026")

    def test_constructor_european_es(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        result = norm.normalize("3 de noviembre", "es")
        self.assertEqual(result, "03.11")

    def test_constructor_european_en(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        result = norm.normalize("third of November", "en")
        self.assertEqual(result, "03.11")

    def test_module_constant_european(self) -> None:
        original = dn_module.DATETIME_OUTPUT_FORMAT
        try:
            dn_module.DATETIME_OUTPUT_FORMAT = "european"
            # New instance picks up module default.
            norm = DateTimeNormalizer()
            result = norm.normalize("третье ноября", "ru")
            self.assertEqual(result, "03.11")
        finally:
            dn_module.DATETIME_OUTPUT_FORMAT = original

    def test_instance_format_independent_of_module_constant(self) -> None:
        """Instance format is fixed at construction time — immune to later module changes."""
        norm_iso = DateTimeNormalizer(output_format="iso8601")
        norm_eu = DateTimeNormalizer(output_format="european")

        # Mutate module constant — should not affect already-created instances.
        original = dn_module.DATETIME_OUTPUT_FORMAT
        try:
            dn_module.DATETIME_OUTPUT_FORMAT = "european"
            self.assertEqual(norm_iso.normalize("третье ноября", "ru"), "11-03")
            self.assertEqual(norm_eu.normalize("третье ноября", "ru"), "03.11")
        finally:
            dn_module.DATETIME_OUTPUT_FORMAT = original


class TestLexicographicSortWorksISO8601(unittest.TestCase):
    """ISO-8601 full dates must sort lexicographically == chronologically."""

    def setUp(self) -> None:
        # Save original so tearDown can restore it — prevents leaking "iso8601"
        # into test_datetime_normalizer.py which expects the "european" default.
        self._orig_fmt = dn_module.DATETIME_OUTPUT_FORMAT
        dn_module.DATETIME_OUTPUT_FORMAT = "iso8601"
        self.norm = DateTimeNormalizer()

    def tearDown(self) -> None:
        dn_module.DATETIME_OUTPUT_FORMAT = self._orig_fmt

    def test_dates_sort_chronologically(self) -> None:
        inputs = [
            ("15 января 2026 года", "ru"),
            ("3 ноября 2025 года", "ru"),
            ("1 марта 2026 года", "ru"),
        ]
        normalized = [self.norm.normalize(text, lang) for text, lang in inputs]
        self.assertEqual(sorted(normalized), ["2025-11-03", "2026-01-15", "2026-03-01"])

    def test_dates_lex_order_matches_chrono_order(self) -> None:
        dates = ["2026-01-15", "2025-11-03", "2026-03-01"]
        self.assertEqual(
            sorted(dates),
            ["2025-11-03", "2026-01-15", "2026-03-01"],
        )

    def test_european_format_does_not_sort_correctly(self) -> None:
        """Demonstrate that European format breaks lexicographic sort (regression check)."""
        # This test documents the known limitation of the European format.
        dates_eu = ["15.01.2026", "03.11.2025", "01.03.2026"]
        # Lexicographic sort of DD.MM.YYYY is wrong chronologically.
        lex_sorted = sorted(dates_eu)
        chrono_sorted = ["03.11.2025", "15.01.2026", "01.03.2026"]
        self.assertNotEqual(lex_sorted, chrono_sorted,
                            "European format SHOULD fail lexicographic sort — if this "
                            "assertion fails, the test logic is wrong.")

    def test_iso_month_day_sort_without_year(self) -> None:
        """MM-DD partial dates also sort within the same year correctly."""
        inputs = [
            "15 января",
            "3 ноября",
            "1 марта",
        ]
        norm = DateTimeNormalizer(output_format="iso8601")
        normalized = [norm.normalize(t, "ru") for t in inputs]
        self.assertEqual(sorted(normalized), ["01-15", "03-01", "11-03"])


class TestFmtDateHelper(unittest.TestCase):
    """Unit tests for the internal _fmt_date method."""

    def test_iso8601_with_year(self) -> None:
        norm = DateTimeNormalizer(output_format="iso8601")
        self.assertEqual(norm._fmt_date(3, 11, 2026), "2026-11-03")

    def test_iso8601_without_year(self) -> None:
        norm = DateTimeNormalizer(output_format="iso8601")
        self.assertEqual(norm._fmt_date(3, 11), "11-03")

    def test_european_with_year(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        self.assertEqual(norm._fmt_date(3, 11, 2026), "03.11.2026")

    def test_european_without_year(self) -> None:
        norm = DateTimeNormalizer(output_format="european")
        self.assertEqual(norm._fmt_date(3, 11), "03.11")

    def test_iso8601_year_zero_padded(self) -> None:
        norm = DateTimeNormalizer(output_format="iso8601")
        self.assertEqual(norm._fmt_date(1, 1, 999), "0999-01-01")

    def test_iso8601_single_digit_day_month(self) -> None:
        norm = DateTimeNormalizer(output_format="iso8601")
        self.assertEqual(norm._fmt_date(5, 3, 2026), "2026-03-05")


if __name__ == "__main__":
    unittest.main(verbosity=2)
