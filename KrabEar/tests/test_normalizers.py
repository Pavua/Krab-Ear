"""Тесты для NumberNormalizer и DateTimeNormalizer.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_normalizers.py -v
"""

import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.number_normalizer import NumberNormalizer
from core.datetime_normalizer import DateTimeNormalizer


class TestNumberNormalizerRU(unittest.TestCase):
    def setUp(self):
        self.n = NumberNormalizer()

    def _ru(self, text):
        return self.n.normalize(text, "ru")

    # --- Базовые числа ---
    def test_simple_digits(self):
        self.assertEqual(self._ru("сто двадцать три"), "123")

    def test_single_digit(self):
        self.assertEqual(self._ru("пять"), "5")

    def test_tens(self):
        self.assertEqual(self._ru("двадцать"), "20")

    def test_hundreds(self):
        self.assertEqual(self._ru("триста"), "300")

    def test_thousands(self):
        self.assertEqual(self._ru("две тысячи"), "2000")

    def test_compound_thousands(self):
        result = self._ru("три тысячи двести")
        self.assertEqual(result, "3200")

    # --- Порядковые числительные ---
    def test_ordinal_first(self):
        self.assertEqual(self._ru("первый"), "1-й")

    def test_ordinal_third(self):
        self.assertEqual(self._ru("третий"), "3-й")

    def test_ordinal_genitive(self):
        # «первого» → «1-го»
        result = self._ru("первого")
        self.assertIn("1", result)

    # --- Проценты ---
    def test_percent(self):
        self.assertEqual(self._ru("тридцать процентов"), "30%")

    def test_percent_five(self):
        self.assertEqual(self._ru("пять процентов"), "5%")

    # --- Отрицательные числа ---
    def test_negative(self):
        result = self._ru("минус пять")
        self.assertIn("-5", result)

    # --- Смешанный текст ---
    def test_mixed_text(self):
        result = self._ru("Цена составляет двести рублей за штуку")
        self.assertIn("200", result)

    # --- Идемпотентность ---
    def test_idempotent_digits(self):
        text = "Мне нужно 123 штуки"
        self.assertEqual(self._ru(text), text)

    def test_idempotent_percent(self):
        text = "Скидка 30%"
        self.assertEqual(self._ru(text), text)

    # --- Дроби ---
    def test_fraction_half(self):
        result = self._ru("половина")
        self.assertIn("1/2", result)

    # --- Большие числа ---
    def test_million(self):
        result = self._ru("один миллион")
        self.assertIn("1000000", result)


class TestNumberNormalizerES(unittest.TestCase):
    def setUp(self):
        self.n = NumberNormalizer()

    def _es(self, text):
        return self.n.normalize(text, "es")

    def test_basic_123(self):
        result = self._es("ciento veintitres")
        self.assertIn("123", result)

    def test_simple_ten(self):
        self.assertEqual(self._es("diez"), "10")

    def test_fifty(self):
        self.assertEqual(self._es("cincuenta"), "50")

    def test_negative(self):
        result = self._es("menos cinco")
        self.assertIn("-5", result)

    def test_idempotent(self):
        text = "Precio: 123 euros"
        self.assertEqual(self._es(text), text)

    def test_percent(self):
        result = self._es("treinta por ciento")
        self.assertIn("30", result)


class TestNumberNormalizerEN(unittest.TestCase):
    def setUp(self):
        self.n = NumberNormalizer()

    def _en(self, text):
        return self.n.normalize(text, "en")

    def test_basic_123(self):
        result = self._en("one hundred twenty three")
        self.assertIn("123", result)

    def test_twenty_three(self):
        result = self._en("twenty-three")
        self.assertIn("23", result)

    def test_ordinal_first(self):
        result = self._en("first")
        self.assertIn("1", result)

    def test_ordinal_third(self):
        result = self._en("third")
        self.assertIn("3", result)

    def test_thousand(self):
        result = self._en("one thousand")
        self.assertIn("1000", result)

    def test_million(self):
        result = self._en("two million")
        self.assertIn("2000000", result)

    def test_percent(self):
        result = self._en("thirty percent")
        self.assertIn("30%", result)

    def test_negative(self):
        result = self._en("minus five")
        self.assertIn("-5", result)

    def test_idempotent_digits(self):
        text = "I have 42 apples"
        self.assertEqual(self._en(text), text)

    def test_idempotent_percent(self):
        text = "50% off sale"
        self.assertEqual(self._en(text), text)

    def test_hundred_and_five(self):
        result = self._en("one hundred and five")
        self.assertIn("105", result)


class TestDateTimeNormalizerRU(unittest.TestCase):
    def setUp(self):
        self.d = DateTimeNormalizer()

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_day_month(self):
        result = self._ru("третье ноября")
        self.assertIn("03.11", result)

    def test_day_month_january(self):
        result = self._ru("пятнадцатого января")
        self.assertIn("15.01", result)

    def test_day_month_with_year(self):
        result = self._ru("пятнадцатого января 2026 года")
        self.assertIn("15.01.2026", result)

    def test_time_morning(self):
        result = self._ru("девять часов утра")
        self.assertIn("09:00", result)

    def test_time_evening(self):
        result = self._ru("семь часов вечера")
        # 7 + 12 = 19
        self.assertIn("19:00", result)

    def test_time_noon_twelve(self):
        result = self._ru("двенадцать часов дня")
        # 12 дня = 12:00
        self.assertIn("12:00", result)

    def test_time_with_minutes(self):
        result = self._ru("девять часов тридцать минут")
        self.assertIn("09:30", result)

    def test_idempotent_date(self):
        text = "Встреча 03.11"
        self.assertEqual(self._ru(text), text)

    def test_idempotent_time(self):
        text = "В 09:00 совещание"
        self.assertEqual(self._ru(text), text)

    def test_first_may(self):
        result = self._ru("первое мая")
        self.assertIn("01.05", result)

    def test_digital_day_plus_month(self):
        result = self._ru("3 ноября")
        self.assertIn("03.11", result)

    def test_digital_day_month_year(self):
        result = self._ru("3 января 2026 года")
        self.assertIn("03.01.2026", result)


class TestDateTimeNormalizerES(unittest.TestCase):
    def setUp(self):
        self.d = DateTimeNormalizer()

    def _es(self, text):
        return self.d.normalize(text, "es")

    def test_day_month(self):
        result = self._es("3 de noviembre")
        self.assertIn("03.11", result)

    def test_day_month_year(self):
        result = self._es("15 de enero de 2026")
        self.assertIn("15.01.2026", result)

    def test_idempotent(self):
        text = "Reunión el 03.11"
        self.assertEqual(self._es(text), text)


class TestDateTimeNormalizerEN(unittest.TestCase):
    def setUp(self):
        self.d = DateTimeNormalizer()

    def _en(self, text):
        return self.d.normalize(text, "en")

    def test_ordinal_month(self):
        result = self._en("third of November")
        self.assertIn("03.11", result)

    def test_month_day_year(self):
        result = self._en("January 15 2026")
        self.assertIn("15.01.2026", result)

    def test_month_day_ordinal(self):
        result = self._en("November 3rd")
        self.assertIn("03.11", result)

    def test_idempotent_date(self):
        text = "Meeting on 03.11"
        self.assertEqual(self._en(text), text)

    def test_idempotent_time(self):
        text = "At 09:00 sharp"
        self.assertEqual(self._en(text), text)


class TestNormalizersConfigFlags(unittest.TestCase):
    """Тест интеграции с config.py — флаги должны быть определены."""

    def test_number_normalization_flag_exists(self):
        from core.config import settings, DEFAULT_SETTINGS
        self.assertTrue(hasattr(settings, "NUMBER_NORMALIZATION_ENABLED"))
        self.assertIn("number_normalization_enabled", DEFAULT_SETTINGS)
        self.assertTrue(DEFAULT_SETTINGS["number_normalization_enabled"])

    def test_datetime_normalization_flag_exists(self):
        from core.config import settings, DEFAULT_SETTINGS
        self.assertTrue(hasattr(settings, "DATETIME_NORMALIZATION_ENABLED"))
        self.assertIn("datetime_normalization_enabled", DEFAULT_SETTINGS)
        self.assertTrue(DEFAULT_SETTINGS["datetime_normalization_enabled"])

    def test_settings_defaults_are_true(self):
        from core.config import settings
        self.assertTrue(settings.NUMBER_NORMALIZATION_ENABLED)
        self.assertTrue(settings.DATETIME_NORMALIZATION_ENABLED)


class TestEdgeCases(unittest.TestCase):
    """Edge cases: пустой текст, уже нормализованный, смешанный."""

    def setUp(self):
        self.n = NumberNormalizer()
        self.d = DateTimeNormalizer()

    def test_empty_string_number(self):
        self.assertEqual(self.n.normalize("", "ru"), "")

    def test_empty_string_datetime(self):
        self.assertEqual(self.d.normalize("", "ru"), "")

    def test_no_numbers(self):
        text = "Привет мир"
        self.assertEqual(self.n.normalize(text, "ru"), text)

    def test_unknown_language_passthrough(self):
        text = "text unchanged"
        self.assertEqual(self.n.normalize(text, "ja"), text)
        self.assertEqual(self.d.normalize(text, "ja"), text)

    def test_ru_idempotent_number_in_context(self):
        # Текст уже содержит цифры — не трогать
        text = "Купил 5 яблок и 3 груши"
        result = self.n.normalize(text, "ru")
        self.assertIn("5", result)
        self.assertIn("3", result)

    def test_double_normalize_stable(self):
        """Двойное применение даёт тот же результат."""
        text = "сто двадцать три"
        first = self.n.normalize(text, "ru")
        second = self.n.normalize(first, "ru")
        self.assertEqual(first, second)

    def test_date_double_normalize(self):
        text = "третье ноября"
        first = self.d.normalize(text, "ru")
        second = self.d.normalize(first, "ru")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
