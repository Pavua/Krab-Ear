"""Wave 118 — unit tests for DateTimeNormalizer.

Coverage targets:
    test_ru_inflected_date      — «первого января» → «01.01»
    test_ru_time                — «в десять часов» → «10:00»
    test_es_date                — «primero de enero» → «01.01»
    test_es_time                — «las diez» → «10:00»
    test_numeric_format_preserved — already-numeric strings unchanged
    test_iso_8601_output_shape  — DD.MM / DD.MM.YYYY and HH:MM patterns
    test_ambiguous_text_returns_original — non-date text unchanged

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_datetime_normalizer.py -v
"""

import re
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.datetime_normalizer import DateTimeNormalizer


class TestDateTimeNormalizerRuInflectedDate(unittest.TestCase):
    """Russian inflected date forms → DD.MM[.YYYY]."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    # --- test_ru_inflected_date ---

    def test_ru_inflected_date_genitive_1st_january(self):
        """«первого января» → «01.01»."""
        result = self._ru("первого января")
        self.assertIn("01.01", result)

    def test_ru_inflected_date_genitive_3rd_november(self):
        """«третьего ноября» → «03.11»."""
        result = self._ru("третьего ноября")
        self.assertIn("03.11", result)

    def test_ru_inflected_date_nominative_15th_january(self):
        """«пятнадцатого января» → «15.01»."""
        result = self._ru("пятнадцатого января")
        self.assertIn("15.01", result)

    def test_ru_inflected_date_with_year(self):
        """«пятнадцатого января 2026 года» → «15.01.2026»."""
        result = self._ru("пятнадцатого января 2026 года")
        self.assertIn("15.01.2026", result)

    def test_ru_inflected_date_first_may(self):
        """«первое мая» → «01.05»."""
        result = self._ru("первое мая")
        self.assertIn("01.05", result)

    def test_ru_inflected_date_31st_december(self):
        """«тридцать первого декабря» → «31.12»."""
        result = self._ru("тридцать первого декабря")
        self.assertIn("31.12", result)

    def test_ru_inflected_date_in_sentence(self):
        """Normalizes date inside a full sentence."""
        result = self._ru("Встреча назначена на третье ноября в Москве")
        self.assertIn("03.11", result)

    def test_ru_inflected_date_digital_day(self):
        """«3 ноября» (digital day + month word) → «03.11»."""
        result = self._ru("3 ноября")
        self.assertIn("03.11", result)

    def test_ru_inflected_date_digital_day_month_year(self):
        """«3 января 2026 года» → «03.01.2026»."""
        result = self._ru("3 января 2026 года")
        self.assertIn("03.01.2026", result)


class TestDateTimeNormalizerRuTime(unittest.TestCase):
    """Russian time expressions → HH:MM."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    # --- test_ru_time ---

    def test_ru_time_ten_oclock(self):
        """«в десять часов» → «10:00»."""
        result = self._ru("в десять часов")
        self.assertIn("10:00", result)

    def test_ru_time_nine_morning(self):
        """«девять часов утра» → «09:00»."""
        result = self._ru("девять часов утра")
        self.assertIn("09:00", result)

    def test_ru_time_seven_evening(self):
        """«семь часов вечера» → «19:00»."""
        result = self._ru("семь часов вечера")
        self.assertIn("19:00", result)

    def test_ru_time_with_minutes(self):
        """«девять часов тридцать минут» → «09:30»."""
        result = self._ru("девять часов тридцать минут")
        self.assertIn("09:30", result)

    def test_ru_time_noon(self):
        """«двенадцать часов дня» → «12:00»."""
        result = self._ru("двенадцать часов дня")
        self.assertIn("12:00", result)

    def test_ru_time_in_sentence(self):
        """Time normalisation inside a full sentence."""
        result = self._ru("Позвони мне в десять часов утра пожалуйста")
        self.assertIn("10:00", result)

    def test_ru_time_two_pm(self):
        """«два часа дня» → «14:00» (pm_mild)."""
        result = self._ru("два часа дня")
        self.assertIn("14:00", result)


class TestDateTimeNormalizerEsDate(unittest.TestCase):
    """Spanish date expressions → DD.MM[.YYYY]."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _es(self, text):
        return self.d.normalize(text, "es")

    # --- test_es_date ---

    def test_es_date_primero_de_enero(self):
        """«primero de enero» → «01.01»."""
        result = self._es("primero de enero")
        self.assertIn("01.01", result)

    def test_es_date_numeric_day_month(self):
        """«3 de noviembre» → «03.11»."""
        result = self._es("3 de noviembre")
        self.assertIn("03.11", result)

    def test_es_date_with_year(self):
        """«15 de enero de 2026» → «15.01.2026»."""
        result = self._es("15 de enero de 2026")
        self.assertIn("15.01.2026", result)

    def test_es_date_el_prefix(self):
        """«el 5 de marzo» → «05.03»."""
        result = self._es("el 5 de marzo")
        self.assertIn("05.03", result)

    def test_es_date_in_sentence(self):
        """Spanish date normalisation inside a full sentence."""
        result = self._es("La reunión es el 3 de noviembre de 2026")
        self.assertIn("03.11.2026", result)

    def test_es_date_ordinal_tercero(self):
        """«tercero de marzo» → «03.03»."""
        result = self._es("tercero de marzo")
        self.assertIn("03.03", result)


class TestDateTimeNormalizerEsTime(unittest.TestCase):
    """Spanish time expressions → HH:MM."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _es(self, text):
        return self.d.normalize(text, "es")

    # --- test_es_time ---

    def test_es_time_las_diez(self):
        """«las diez» → «10:00»."""
        result = self._es("las diez")
        self.assertIn("10:00", result)

    def test_es_time_a_las_nine(self):
        """«a las 9» → «09:00»."""
        result = self._es("a las 9")
        self.assertIn("09:00", result)

    def test_es_time_morning_marker(self):
        """«nueve de la mañana» → «09:00»."""
        result = self._es("nueve de la mañana")
        self.assertIn("09:00", result)

    def test_es_time_afternoon_marker(self):
        """«tres de la tarde» → «15:00»."""
        result = self._es("tres de la tarde")
        self.assertIn("15:00", result)

    def test_es_time_with_half(self):
        """«nueve y media» → «09:30»."""
        result = self._es("nueve y media")
        self.assertIn("09:30", result)

    def test_es_time_in_sentence(self):
        """Spanish time normalisation inside a full sentence."""
        result = self._es("La reunión empieza a las diez en punto")
        self.assertIn("10:00", result)


class TestDateTimeNormalizerNumericFormatPreserved(unittest.TestCase):
    """Already-numeric dates/times must pass through unchanged (idempotency)."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_numeric_format_preserved_ru_date(self):
        """«03.11» is not modified by RU normalizer."""
        text = "Встреча 03.11"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_format_preserved_ru_time(self):
        """«09:00» is not modified by RU normalizer."""
        text = "В 09:00 совещание"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_numeric_format_preserved_es_date(self):
        """«03.11» is not modified by ES normalizer."""
        text = "Reunión el 03.11"
        self.assertEqual(self.d.normalize(text, "es"), text)

    def test_numeric_format_preserved_en_date(self):
        """«03.11» is not modified by EN normalizer."""
        text = "Meeting on 03.11"
        self.assertEqual(self.d.normalize(text, "en"), text)

    def test_numeric_format_preserved_en_time(self):
        """«09:00» is not modified by EN normalizer."""
        text = "At 09:00 sharp"
        self.assertEqual(self.d.normalize(text, "en"), text)

    def test_double_normalize_stable_ru(self):
        """Applying RU normalize twice gives same result."""
        text = "третье ноября"
        first = self.d.normalize(text, "ru")
        second = self.d.normalize(first, "ru")
        self.assertEqual(first, second)

    def test_double_normalize_stable_es(self):
        """Applying ES normalize twice gives same result."""
        text = "3 de noviembre"
        first = self.d.normalize(text, "es")
        second = self.d.normalize(first, "es")
        self.assertEqual(first, second)


class TestDateTimeNormalizerIso8601OutputShape(unittest.TestCase):
    """Output must match DD.MM or DD.MM.YYYY and HH:MM patterns."""

    _DATE_SHORT = re.compile(r"^\d{2}\.\d{2}$")
    _DATE_LONG = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
    _TIME = re.compile(r"^\d{2}:\d{2}$")

    def setUp(self):
        self.d = DateTimeNormalizer()

    def test_iso_8601_output_shape_date_short(self):
        """Output of 'третье ноября' matches DD.MM."""
        result = self.d.normalize("третье ноября", "ru")
        self.assertRegex(result, self._DATE_SHORT)

    def test_iso_8601_output_shape_date_long(self):
        """Output of '15 января 2026 года' matches DD.MM.YYYY."""
        result = self.d.normalize("15 января 2026 года", "ru")
        self.assertRegex(result, self._DATE_LONG)

    def test_iso_8601_output_shape_time(self):
        """Output of 'девять часов утра' matches HH:MM."""
        result = self.d.normalize("девять часов утра", "ru")
        self.assertRegex(result, self._TIME)

    def test_iso_8601_output_shape_es_date(self):
        """Output of '3 de noviembre' matches DD.MM."""
        result = self.d.normalize("3 de noviembre", "es")
        self.assertRegex(result, self._DATE_SHORT)

    def test_iso_8601_output_shape_es_date_long(self):
        """Output of '15 de enero de 2026' matches DD.MM.YYYY."""
        result = self.d.normalize("15 de enero de 2026", "es")
        self.assertRegex(result, self._DATE_LONG)

    def test_iso_8601_output_shape_es_time(self):
        """Output of 'las diez' contains HH:MM."""
        result = self.d.normalize("las diez", "es")
        # result may have surrounding text; check substring
        self.assertRegex(result, re.compile(r"\d{2}:\d{2}"))

    def test_iso_8601_output_shape_en_date(self):
        """Output of 'third of November' matches DD.MM."""
        result = self.d.normalize("third of November", "en")
        self.assertRegex(result, self._DATE_SHORT)


class TestDateTimeNormalizerAmbiguousText(unittest.TestCase):
    """Non-date/time text must pass through unchanged."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    # --- test_ambiguous_text_returns_original ---

    def test_ambiguous_text_no_date_ru(self):
        """Pure Russian text without date → unchanged."""
        text = "Привет как дела"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_ambiguous_text_no_date_es(self):
        """Pure Spanish text without date → unchanged."""
        text = "Hola cómo estás"
        self.assertEqual(self.d.normalize(text, "es"), text)

    def test_ambiguous_text_no_date_en(self):
        """Pure English text without date → unchanged."""
        text = "Hello how are you"
        self.assertEqual(self.d.normalize(text, "en"), text)

    def test_ambiguous_empty_string(self):
        """Empty string → empty string (all langs)."""
        for lang in ("ru", "es", "en"):
            with self.subTest(lang=lang):
                self.assertEqual(self.d.normalize("", lang), "")

    def test_ambiguous_unknown_language_passthrough(self):
        """Unknown language code → text unchanged."""
        text = "何もしない"
        self.assertEqual(self.d.normalize(text, "ja"), text)

    def test_ambiguous_pure_digits_unchanged(self):
        """Plain digit sequences are not rewritten."""
        text = "12345"
        self.assertEqual(self.d.normalize(text, "ru"), text)

    def test_ambiguous_partial_match_not_mangled(self):
        """A word that partially matches a month name is not rewritten."""
        # «январский» is not a month entry in the lookup table
        text = "январский мороз"
        result = self.d.normalize(text, "ru")
        # Should not produce a date fragment like «.01»
        self.assertNotIn(".01", result)


if __name__ == "__main__":
    unittest.main()
