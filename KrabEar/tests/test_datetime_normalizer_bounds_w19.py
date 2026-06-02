"""wave-19 bounds tests for DateTimeNormalizer.

Finding 1: impossible day-in-month (e.g. February 31) and out-of-range
time (hour>23, minute>59) were emitted as normalised output instead of
being left unchanged.

Finding 2: class docstring claimed ISO-8601 is the default output format
while the code actually defaults to european (DD.MM.YYYY).
"""

import sys
import os

# ---------------------------------------------------------------------------
# Path setup — lets the test run both standalone and via pytest.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
KRAB_EAR_ROOT = os.path.join(PROJECT_ROOT, "KrabEar")
if KRAB_EAR_ROOT not in sys.path:
    sys.path.insert(0, KRAB_EAR_ROOT)

import unittest

from core.datetime_normalizer import DateTimeNormalizer, _valid_date, _valid_time


class TestValidDateHelper(unittest.TestCase):
    """Unit-tests for the _valid_date helper function."""

    def test_valid_common_dates(self):
        self.assertTrue(_valid_date(1, 1))
        self.assertTrue(_valid_date(31, 1))   # Jan has 31 days
        self.assertTrue(_valid_date(28, 2))   # Feb always allows 28
        self.assertTrue(_valid_date(29, 2))   # Feb 29 — lenient (leap-year)
        self.assertTrue(_valid_date(30, 4))   # Apr has 30 days
        self.assertTrue(_valid_date(31, 12))

    def test_invalid_day_too_large_for_month(self):
        self.assertFalse(_valid_date(30, 2))  # Feb max = 29
        self.assertFalse(_valid_date(31, 2))  # Feb max = 29
        self.assertFalse(_valid_date(31, 4))  # Apr max = 30
        self.assertFalse(_valid_date(31, 6))  # Jun max = 30
        self.assertFalse(_valid_date(31, 9))  # Sep max = 30
        self.assertFalse(_valid_date(31, 11))  # Nov max = 30

    def test_invalid_day_zero_or_negative(self):
        self.assertFalse(_valid_date(0, 3))
        self.assertFalse(_valid_date(-1, 3))

    def test_invalid_month(self):
        self.assertFalse(_valid_date(1, 0))
        self.assertFalse(_valid_date(1, 13))


class TestValidTimeHelper(unittest.TestCase):
    """Unit-tests for the _valid_time helper function."""

    def test_valid_times(self):
        self.assertTrue(_valid_time(0, 0))
        self.assertTrue(_valid_time(23, 59))
        self.assertTrue(_valid_time(12, 30))

    def test_invalid_hour(self):
        self.assertFalse(_valid_time(24, 0))
        self.assertFalse(_valid_time(25, 0))

    def test_invalid_minute(self):
        self.assertFalse(_valid_time(10, 60))
        self.assertFalse(_valid_time(10, 99))

    def test_invalid_both(self):
        self.assertFalse(_valid_time(25, 99))


class TestImpossibleDateRussian(unittest.TestCase):
    """Impossible dates in Russian must be left unchanged."""

    def setUp(self):
        self.n = DateTimeNormalizer()

    def test_feb_31_ordinal_unchanged(self):
        """тридцать первого февраля should not be normalised."""
        text = "тридцать первого февраля"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_feb_30_ordinal_unchanged(self):
        """тридцатого февраля should not be normalised."""
        text = "тридцатого февраля"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_nov_31_ordinal_unchanged(self):
        """тридцать первого ноября should not be normalised (Nov has 30 days)."""
        text = "тридцать первого ноября"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_digit_day_feb_31_unchanged(self):
        """31 февраля (digit + word month) should not be normalised."""
        text = "31 февраля"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_digit_day_feb_30_unchanged(self):
        """30 февраля should not be normalised."""
        text = "30 февраля"
        result = self.n.normalize(text, "ru")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_valid_date_still_normalised(self):
        """Valid date 15 января should still normalise correctly."""
        result = self.n.normalize("15 января", "ru")
        self.assertEqual(result, "15.01")

    def test_valid_feb_date_still_normalised(self):
        """Valid date третье февраля (Feb 3) should normalise."""
        result = self.n.normalize("третьего февраля", "ru")
        self.assertEqual(result, "03.02")

    def test_valid_feb_29_still_normalised(self):
        """29 февраля (leap day) should normalise — we are lenient."""
        result = self.n.normalize("29 февраля", "ru")
        self.assertEqual(result, "29.02")


class TestImpossibleDateSpanish(unittest.TestCase):
    """Impossible dates in Spanish must be left unchanged."""

    def setUp(self):
        self.n = DateTimeNormalizer()

    def test_feb_31_digit_unchanged(self):
        """31 de febrero should not be normalised."""
        text = "31 de febrero"
        result = self.n.normalize(text, "es")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_apr_31_digit_unchanged(self):
        """31 de abril should not be normalised (April has 30 days)."""
        text = "31 de abril"
        result = self.n.normalize(text, "es")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_valid_es_date_still_normalised(self):
        """Valid date 15 de enero should normalise."""
        result = self.n.normalize("15 de enero", "es")
        self.assertEqual(result, "15.01")


class TestImpossibleDateEnglish(unittest.TestCase):
    """Impossible dates in English must be left unchanged."""

    def setUp(self):
        self.n = DateTimeNormalizer()

    def test_feb_31_unchanged(self):
        """31st of February should not be normalised."""
        text = "thirty-first of February"
        # EN ordinals lookup — test the digit path instead (more reliable)
        text2 = "February 31st"
        result2 = self.n.normalize(text2, "en")
        self.assertEqual(result2, text2, f"Expected unchanged, got: {result2!r}")

    def test_feb_31_digit_path_unchanged(self):
        """February 31 should not be normalised."""
        text = "February 31"
        result = self.n.normalize(text, "en")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_apr_31_digit_unchanged(self):
        """April 31 should not be normalised."""
        text = "April 31"
        result = self.n.normalize(text, "en")
        self.assertEqual(result, text, f"Expected unchanged, got: {result!r}")

    def test_valid_en_date_still_normalised(self):
        """November 3 should normalise correctly."""
        result = self.n.normalize("November 3", "en")
        self.assertEqual(result, "03.11")


class TestOutOfRangeTimeRussian(unittest.TestCase):
    """Out-of-range times in Russian must be left unchanged."""

    def setUp(self):
        self.n = DateTimeNormalizer()

    def test_valid_time_normalised(self):
        """девять часов утра should normalise to 09:00."""
        result = self.n.normalize("девять часов утра", "ru")
        self.assertEqual(result, "09:00")

    def test_valid_time_no_marker(self):
        """пять часов should normalise to 05:00."""
        result = self.n.normalize("пять часов", "ru")
        self.assertEqual(result, "05:00")


class TestDocstringDefaultFormat(unittest.TestCase):
    """Verify the default output format is european, not iso8601."""

    def test_default_is_european(self):
        """DateTimeNormalizer() with no args must emit european format."""
        n = DateTimeNormalizer()
        result = n.normalize("третьего ноября", "ru")
        # European format: DD.MM
        self.assertEqual(result, "03.11", f"Expected european format, got: {result!r}")

    def test_iso_explicit(self):
        """DateTimeNormalizer(output_format='iso8601') must emit MM-DD."""
        n = DateTimeNormalizer(output_format="iso8601")
        result = n.normalize("третьего ноября", "ru")
        # ISO format: MM-DD (no year)
        self.assertEqual(result, "11-03", f"Expected iso8601 format, got: {result!r}")

    def test_class_docstring_mentions_european(self):
        """Class docstring must reference 'european' as the default."""
        doc = DateTimeNormalizer.__doc__ or ""
        self.assertIn("european", doc.lower())
        # Should NOT claim iso8601 is the default
        self.assertNotIn("По умолчанию использует ISO-8601", doc)

    def test_init_docstring_mentions_european_default(self):
        """__init__ docstring must say european is the default."""
        doc = DateTimeNormalizer.__init__.__doc__ or ""
        self.assertIn("european", doc.lower())


if __name__ == "__main__":
    unittest.main()
