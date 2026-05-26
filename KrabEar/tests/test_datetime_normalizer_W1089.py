"""Wave 1089 — regression tests for DateTimeNormalizer fixes (W1083 F1+F2+F3).

F1: EN/ES word-hour requires time-context anchor → bare cardinals not corrupted.
F2: RU month-name regex has right-boundary guard → inflected suffixes not corrupted.
F3: «восемь часов ночи» → «20:00» (night marker adds 12 when hour ∈ [6,11]).

Run:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_datetime_normalizer_W1089.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.datetime_normalizer import DateTimeNormalizer


class TestF1EnBareCardinalNotConverted(unittest.TestCase):
    """F1: EN bare cardinals without time anchor MUST NOT be converted."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _en(self, text):
        return self.d.normalize(text, "en")

    def test_bare_two_in_en_text_not_converted(self):
        """'two people came' must remain unchanged — no time anchor present."""
        text = "two people came"
        result = self._en(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_bare_one_not_converted(self):
        """'one step at a time' must remain unchanged."""
        text = "one step at a time"
        result = self._en(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_bare_five_not_converted(self):
        """'five minutes later' must remain unchanged."""
        text = "five minutes later"
        result = self._en(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_bare_twelve_not_converted(self):
        """'twelve people attended the meeting' must remain unchanged."""
        text = "twelve people attended the meeting"
        result = self._en(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_two_oclock_still_converted(self):
        """'two o'clock' MUST still be converted → '02:00'."""
        result = self._en("two o'clock")
        self.assertIn("02:00", result, f"Got: {result!r}")

    def test_two_pm_still_converted(self):
        """'two pm' MUST still be converted → '14:00'."""
        result = self._en("two pm")
        self.assertIn("14:00", result, f"Got: {result!r}")

    def test_nine_in_the_morning_still_converted(self):
        """'nine in the morning' MUST still be converted → '09:00'."""
        result = self._en("nine in the morning")
        self.assertIn("09:00", result, f"Got: {result!r}")

    def test_seven_am_still_converted(self):
        """'seven am' MUST still be converted → '07:00'."""
        result = self._en("seven am")
        self.assertIn("07:00", result, f"Got: {result!r}")

    def test_eight_in_the_evening_still_converted(self):
        """'eight in the evening' MUST still be converted → '20:00'."""
        result = self._en("eight in the evening")
        self.assertIn("20:00", result, f"Got: {result!r}")


class TestF1EsBareCardinalNotConverted(unittest.TestCase):
    """F1: ES bare cardinals without time anchor MUST NOT be converted."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _es(self, text):
        return self.d.normalize(text, "es")

    def test_una_persona_not_converted(self):
        """'una persona' must remain unchanged — 'una' is a bare cardinal here."""
        text = "una persona llegó tarde"
        result = self._es(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_dos_cosas_not_converted(self):
        """'dos cosas' must remain unchanged."""
        text = "dos cosas importantes"
        result = self._es(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_tres_personas_not_converted(self):
        """'tres personas' must remain unchanged."""
        text = "tres personas en la sala"
        result = self._es(text)
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_nueve_de_la_manana_still_converted(self):
        """'nueve de la mañana' MUST still be converted → '09:00'."""
        result = self._es("nueve de la mañana")
        self.assertIn("09:00", result, f"Got: {result!r}")

    def test_tres_de_la_tarde_still_converted(self):
        """'tres de la tarde' MUST still be converted → '15:00'."""
        result = self._es("tres de la tarde")
        self.assertIn("15:00", result, f"Got: {result!r}")

    def test_nueve_y_media_still_converted(self):
        """'nueve y media' MUST still be converted → '09:30'."""
        result = self._es("nueve y media")
        self.assertIn("09:30", result, f"Got: {result!r}")

    def test_ocho_y_cuarto_de_la_tarde_still_converted(self):
        """'ocho y cuarto de la tarde' MUST still be converted → '20:15'."""
        result = self._es("ocho y cuarto de la tarde")
        self.assertIn("20:15", result, f"Got: {result!r}")


class TestF2RuMonthBoundary(unittest.TestCase):
    """F2: RU month-name regex must not match inflected suffixes beyond word boundary."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_majsky_inflected_not_corrupted(self):
        """'пятое майского' must NOT be converted — 'майского' is not 'мая'."""
        text = "пятое майского праздника"
        result = self._ru(text)
        # Should not produce a date fragment like «05.05»
        self.assertNotIn("05.05", result, f"Got: {result!r}")
        self.assertEqual(result, text, f"Got: {result!r}")

    def test_yanvarskiy_not_corrupted(self):
        """'январский' suffix must not match the month 'январе'."""
        text = "январский мороз"
        result = self._ru(text)
        self.assertNotIn(".01", result, f"Got: {result!r}")

    def test_pyatoe_maya_still_converted(self):
        """'пятое мая' MUST still be converted → '05.05'."""
        result = self._ru("пятое мая")
        self.assertIn("05.05", result, f"Got: {result!r}")

    def test_pervogo_yanvarya_still_converted(self):
        """'первого января' MUST still be converted → '01.01'."""
        result = self._ru("первого января")
        self.assertIn("01.01", result, f"Got: {result!r}")

    def test_3_noyabrya_still_converted(self):
        """'3 ноября' MUST still be converted → '03.11'."""
        result = self._ru("3 ноября")
        self.assertIn("03.11", result, f"Got: {result!r}")


class TestF3NightMarkerHour6to11(unittest.TestCase):
    """F3: 'night' marker must add 12 when hour ∈ [6, 11]."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_evening_marker_adds_12_to_morning_hour(self):
        """'восемь часов ночи' must → '20:00' (8 + 12 = 20)."""
        result = self._ru("восемь часов ночи")
        self.assertIn("20:00", result, f"Got: {result!r}")

    def test_seven_at_night_ru(self):
        """'семь часов ночи' must → '19:00' (7 + 12 = 19)."""
        result = self._ru("семь часов ночи")
        self.assertIn("19:00", result, f"Got: {result!r}")

    def test_eleven_at_night_ru(self):
        """'одиннадцать часов ночи' must → '23:00' (11 + 12 = 23)."""
        result = self._ru("одиннадцать часов ночи")
        self.assertIn("23:00", result, f"Got: {result!r}")

    def test_two_at_night_ru_stays_low(self):
        """'два часа ночи' must remain '02:00' (hour < 6, no +12)."""
        result = self._ru("два часа ночи")
        self.assertIn("02:00", result, f"Got: {result!r}")

    def test_five_at_night_ru_stays_low(self):
        """'пять часов ночи' must remain '05:00' (hour < 6, no +12)."""
        result = self._ru("пять часов ночи")
        self.assertIn("05:00", result, f"Got: {result!r}")

    def test_nine_morning_ru_unchanged_by_night_logic(self):
        """'девять часов утра' must → '09:00' (am marker, unaffected by night fix)."""
        result = self._ru("девять часов утра")
        self.assertIn("09:00", result, f"Got: {result!r}")


if __name__ == "__main__":
    unittest.main()
