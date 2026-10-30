"""Не нормализовать часть составной/неполной порядковой даты."""
from pathlib import Path
from types import ModuleType
import unittest
ROOT = Path(__file__).resolve().parents[1]
SPACES = (" ", "  ", "\t", "\n", "\u00a0")
def load_source(name):
    path = ROOT / "core" / (name + ".py")
    module = ModuleType("ear_partial_" + name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module
class OrdinalPartialDateRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.numbers = load_source("number_normalizer").NumberNormalizer()
        cls.dates = load_source("datetime_normalizer").DateTimeNormalizer(output_format="european")
    def assert_preserved_after_numbers(self, text):
        intermediate = self.numbers.normalize(text, "ru")
        self.assertEqual(self.dates.normalize(intermediate, "ru"), intermediate,
                         "Number output: " + repr(intermediate))
    def test_compound_day_is_not_partially_normalized(self):
        for space in SPACES:
            with self.subTest(space=repr(space)):
                self.assert_preserved_after_numbers("двадцать" + space + "третьего ноября")
    def test_unconsumed_word_year_is_not_partially_normalized(self):
        for space in SPACES:
            with self.subTest(space=repr(space)):
                self.assert_preserved_after_numbers("первое мая" + space + "две тысячи шестнадцатого года")
    def test_numeric_year_continuations_are_preserved(self):
        for year in ("20260 года", "2026 годами", "2026xyz года"):
            with self.subTest(year=year):
                self.assert_preserved_after_numbers("третьего ноября " + year)
    def test_unambiguous_ordinal_dates_still_normalize(self):
        for text, expected in (
            ("третье ноября", "03.11"), ("третьего ноября", "03.11"),
            ("первое мая", "01.05"), ("первого мая", "01.05"),
            ("третьего ноября 2026 года", "03.11.2026"),
            ("первого мая 2026 года", "01.05.2026"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.dates.normalize(self.numbers.normalize(text, "ru"), "ru"), expected)
    def test_clock_code_decimal_prefixes_do_not_block_dates(self):
        for prefix in ("16:35", "09:00", "QA20", "QA-20", "20.5", "20,5"):
            for route in ("direct", "composed"):
                with self.subTest(prefix=prefix, route=route):
                    text = prefix + (" 3-го ноября" if route == "direct" else " третьего ноября")
                    if route == "composed":
                        text = self.numbers.normalize(text, "ru")
                    self.assertEqual(self.dates.normalize(text, "ru"), prefix + " 03.11")
        for route in ("direct", "composed"):
            with self.subTest(prefix="spoken clock", route=route):
                text = "в девять часов утра третьего ноября"
                if route == "composed":
                    text = self.numbers.normalize(text, "ru")
                self.assertEqual(self.dates.normalize(text, "ru"), "09:00 03.11")
