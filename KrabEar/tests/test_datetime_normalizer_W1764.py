"""Wave 1764 — регрессионные тесты для исправления corruption русского года.

Подтверждённый баг: year_words_pat использовал «двух?», которое не матчило
«две тысячи» (самую частую разговорную форму).  В результате год не захватывался
группой 3, и re.sub заменял «двадцать третье мая » → «23.05», приклеивая
«две тысячи двадцать четвёртого года» прямо без пробела.

Примеры corruption до исправления:
    «двадцать третье мая две тысячи двадцать четвёртого года»
        → «23.05две тысячи двадцать четвёртого года»  # CORRUPTION
    «пятнадцатого января две тысячи двадцать шестого года»
        → «15.01две тысячи двадцать шестого года»     # CORRUPTION

Инварианты после исправления:
    1. Словесный год «две тысячи ХХ-ого» нормализуется корректно.
    2. Нет corruption (цифровая дата не склеивается со словесными остатками).
    3. Формы без «года» тоже работают.
    4. Fail-safe: нераспознанный год → исходный текст без изменений.
    5. Числовые даты с годом (регрессия) работают по-прежнему.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_datetime_normalizer_W1764.py -v
"""

import re
import sys
import os
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.datetime_normalizer import DateTimeNormalizer


# ---------------------------------------------------------------------------
# Вспомогательная утилита: паттерн «corruption» = цифровая дата впритык
# к кириллице, напр. «23.05две» или «15.01тысяча».
# ---------------------------------------------------------------------------
_CORRUPTION_RE = re.compile(r"\d{2}\.\d{2}[а-яёА-ЯЁ]", re.IGNORECASE)


def _has_corruption(text: str) -> bool:
    """True, если в тексте есть признак склейки даты со словами."""
    return bool(_CORRUPTION_RE.search(text))


class TestW1764SpokenYearCorrectNormalization(unittest.TestCase):
    """Словесный год «две тысячи ХХ-ого» должен нормализоваться в цифры."""

    def setUp(self):
        self.d = DateTimeNormalizer(output_format="european")
        self.d_iso = DateTimeNormalizer(output_format="iso8601")

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def _ru_iso(self, text):
        return self.d_iso.normalize(text, "ru")

    # --- основной сценарий W1764 ---

    def test_23rd_may_2024_eu(self):
        """«двадцать третье мая две тысячи двадцать четвёртого года» → «23.05.2024»."""
        inp = "двадцать третье мая две тысячи двадцать четвёртого года"
        result = self._ru(inp)
        self.assertIn("23.05.2024", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_15th_january_2026_eu(self):
        """«пятнадцатого января две тысячи двадцать шестого года» → «15.01.2026»."""
        inp = "пятнадцатого января две тысячи двадцать шестого года"
        result = self._ru(inp)
        self.assertIn("15.01.2026", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_23rd_may_2024_without_goda(self):
        """«двадцать третье мая две тысячи двадцать четвёртого» (без «года») → «23.05.2024»."""
        inp = "двадцать третье мая две тысячи двадцать четвёртого"
        result = self._ru(inp)
        self.assertIn("23.05.2024", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_3rd_november_2021_eu(self):
        """«третье ноября две тысячи двадцать первого года» → «03.11.2021»."""
        inp = "третье ноября две тысячи двадцать первого года"
        result = self._ru(inp)
        self.assertIn("03.11.2021", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_1st_january_2000_eu(self):
        """«первого января две тысячи» → «01.01.2000» (год 2000, «две тысячи» = 2000)."""
        inp = "первого января две тысячи"
        result = self._ru(inp)
        # «две тысячи» без десятков/единиц = 2000
        self.assertIn("01.01.2000", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_iso_format_spoken_year(self):
        """ISO-8601: «пятнадцатого января две тысячи двадцать шестого» → «2026-01-15»."""
        inp = "пятнадцатого января две тысячи двадцать шестого"
        result = self._ru_iso(inp)
        self.assertIn("2026-01-15", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_in_full_sentence_no_corruption(self):
        """Дата в предложении не создаёт corruption вокруг соседних слов."""
        inp = "Встреча была двадцать третьего мая две тысячи двадцать четвёртого года в Москве"
        result = self._ru(inp)
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")
        self.assertIn("23.05.2024", result, f"Date not normalized: {result!r}")

    # --- 20xx в разных формах ---

    def test_2010_spoken_year(self):
        """«первого марта две тысячи десятого года» → «01.03.2010»."""
        inp = "первого марта две тысячи десятого года"
        result = self._ru(inp)
        self.assertIn("01.03.2010", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_2020_spoken_year(self):
        """«второго апреля две тысячи двадцатого года» → «02.04.2020»."""
        inp = "второго апреля две тысячи двадцатого года"
        result = self._ru(inp)
        self.assertIn("02.04.2020", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")


class TestW1764CorruptionNeverOccurs(unittest.TestCase):
    """Основной инвариант: corruption недопустима ни при каких входных данных."""

    def setUp(self):
        self.d = DateTimeNormalizer(output_format="european")

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_no_corruption_spoken_year_primary(self):
        """Главный кейс W1764: «23.05две тысячи...» НИКОГДА не должно появляться."""
        inp = "двадцать третье мая две тысячи двадцать четвёртого года"
        result = self._ru(inp)
        # Проверяем прямо строку corruption:
        self.assertNotIn("23.05две", result, f"Corruption! Got: {result!r}")
        self.assertNotIn("23.05тысяч", result, f"Corruption! Got: {result!r}")

    def test_no_corruption_january_2026(self):
        """«15.01две тысячи...» НИКОГДА не должно появляться."""
        inp = "пятнадцатого января две тысячи двадцать шестого года"
        result = self._ru(inp)
        self.assertNotIn("15.01две", result, f"Corruption! Got: {result!r}")

    def test_no_corruption_spoken_year_variants(self):
        """Все типичные разговорные варианты года не дают corruption."""
        variants = [
            ("третье марта две тысячи двадцать второго года", "03.03.2022"),
            ("седьмого июля две тысячи двадцать третьего", "07.07.2023"),
            ("двадцать пятого декабря две тысячи двадцать пятого года", "25.12.2025"),
        ]
        for inp, expected in variants:
            result = self._ru(inp)
            self.assertFalse(
                _has_corruption(result),
                f"Corruption for {inp!r}: {result!r}"
            )
            self.assertIn(expected, result, f"Wrong normalization for {inp!r}: {result!r}")


class TestW1764FailSafe(unittest.TestCase):
    """Fail-safe: нераспознанный год → исходный текст без изменений."""

    def setUp(self):
        self.d = DateTimeNormalizer(output_format="european")

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_unrecognized_year_no_corruption(self):
        """Невалидный год-фрагмент не вызывает corruption (дата без пробела к словам)."""
        # «тысячи» — невалидная форма (должно быть «тысяча»), не распознаётся
        # как год, поэтому дата нормализуется изолированно без склейки.
        inp = "третьего апреля тысячи пятого года"
        result = self._ru(inp)
        # Ключевой инвариант: нет corruption (цифры не склеиваются с кириллицей).
        self.assertFalse(_has_corruption(result), f"Corruption detected: {result!r}")

    def test_date_without_year_still_normalizes(self):
        """Дата без года нормализуется как обычно."""
        result = self._ru("третье ноября")
        self.assertIn("03.11", result)
        self.assertFalse(_has_corruption(result))


class TestW1764NumericRegressions(unittest.TestCase):
    """Регрессия: числовые даты работают по-прежнему (до исправления тоже работали)."""

    def setUp(self):
        self.d = DateTimeNormalizer(output_format="european")
        self.d_iso = DateTimeNormalizer(output_format="iso8601")

    def _ru(self, text):
        return self.d.normalize(text, "ru")

    def test_numeric_year_still_works(self):
        """«15 января 2026 года» → «15.01.2026»."""
        result = self._ru("15 января 2026 года")
        self.assertIn("15.01.2026", result, f"Got: {result!r}")

    def test_numeric_date_no_year(self):
        """«3 ноября» → «03.11»."""
        result = self._ru("3 ноября")
        self.assertIn("03.11", result)

    def test_ordinal_date_no_year(self):
        """«первого января» → «01.01»."""
        result = self._ru("первого января")
        self.assertIn("01.01", result)

    def test_31st_december_no_year(self):
        """«тридцать первого декабря» → «31.12»."""
        result = self._ru("тридцать первого декабря")
        self.assertIn("31.12", result)

    def test_19xx_spoken_year(self):
        """«двенадцатое февраля тысяча девятьсот девяностого года» → «12.02.1990»."""
        result = self._ru("двенадцатое февраля тысяча девятьсот девяностого года")
        self.assertIn("12.02.1990", result, f"Got: {result!r}")
        self.assertFalse(_has_corruption(result), f"Corruption: {result!r}")

    def test_numeric_year_iso(self):
        """ISO: «первого марта 2024 года» → «2024-03-01»."""
        result = self.d_iso.normalize("первого марта 2024 года", "ru")
        self.assertIn("2024-03-01", result, f"Got: {result!r}")


class TestW1764ParseYearRuDirect(unittest.TestCase):
    """Юнит-тесты для _parse_year_ru: покрываем обе формы «две тысячи» и «двух тысяч»."""

    def setUp(self):
        self.d = DateTimeNormalizer()

    def _parse(self, text):
        return self.d._parse_year_ru(text)

    def test_dve_tysyachi_dvadtsat_chetvertogo(self):
        """«две тысячи двадцать четвёртого года» → 2024."""
        self.assertEqual(self._parse("две тысячи двадцать четвёртого года"), 2024)

    def test_dve_tysyachi_dvadtsat_shestogo(self):
        """«две тысячи двадцать шестого года» → 2026."""
        self.assertEqual(self._parse("две тысячи двадцать шестого года"), 2026)

    def test_dve_tysyachi_without_goda(self):
        """«две тысячи двадцать четвёртого» (без года) → 2024."""
        self.assertEqual(self._parse("две тысячи двадцать четвёртого"), 2024)

    def test_dvukh_tysyach_dvadtsat_shestogo(self):
        """«двух тысяч двадцать шестого года» → 2026 (ранее возвращал None)."""
        self.assertEqual(self._parse("двух тысяч двадцать шестого года"), 2026)

    def test_numeric_year(self):
        """«2026 года» → 2026."""
        self.assertEqual(self._parse("2026 года"), 2026)

    def test_empty_returns_none(self):
        """Пустая строка → None."""
        self.assertIsNone(self._parse(""))

    def test_unrecognized_returns_none(self):
        """Нераспознанный текст → None."""
        self.assertIsNone(self._parse("бесконечно давно"))

    def test_tysyacha_devyatset_90(self):
        """«тысяча девятьсот девяностого года» → 1990."""
        self.assertEqual(self._parse("тысяча девятьсот девяностого года"), 1990)


if __name__ == "__main__":
    unittest.main()
