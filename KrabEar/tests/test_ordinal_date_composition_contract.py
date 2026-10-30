"""Контракт порядковых дат: actual Number → DateTime, без backend/ASR."""
from pathlib import Path
from types import ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    ("es", "tercero de marzo", "03.03"),
    ("es", "primero de enero", "01.01"),
    ("ru", "третье ноября", "03.11"),
    ("ru", "третьего ноября", "03.11"),
)


def load_source(name):
    path = ROOT / "core" / (name + ".py")
    module = ModuleType("ear_ordinal_" + name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class OrdinalDateComposition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.numbers = load_source("number_normalizer").NumberNormalizer()
        cls.dates = load_source("datetime_normalizer").DateTimeNormalizer(
            output_format="european"
        )

    def composed(self, text, language):
        return self.dates.normalize(self.numbers.normalize(text, language), language)

    def test_documented_ordinal_dates_direct(self):
        for language, text, expected in CASES:
            with self.subTest(language=language, text=text):
                self.assertEqual(self.dates.normalize(text, language), expected)

    def test_number_stage_preserves_ordinal_date_normalizability(self):
        for language, text, expected in CASES:
            with self.subTest(language=language, text=text):
                intermediate = self.numbers.normalize(text, language)
                self.assertEqual(
                    self.dates.normalize(intermediate, language), expected,
                    "Number output: " + repr(intermediate),
                )

    def test_valid_digit_dates_still_normalize(self):
        for language, text in (("es", "3 de noviembre"), ("ru", "3 ноября")):
            with self.subTest(language=language):
                self.assertEqual(self.composed(text, language), "03.11")

    def test_digital_date_time_codes_preserved(self):
        text = "LunaTest QA0472 03.11 16:35"
        for language in ("ru", "es"):
            with self.subTest(language=language):
                self.assertEqual(self.composed(text, language), text)

    def test_ordinal_date_negative_controls(self):
        for language, text in (
            ("ru", "QA3-го ноября"), ("es", "QA3.º de noviembre"),
            ("ru", "113-го ноября"), ("es", "113.º de noviembre"),
            ("ru", "3-xyz ноября"), ("es", "3.xyz de noviembre"),
            ("ru", "32-го ноября"), ("es", "32.º de noviembre"),
            ("ru", "3-го ноябрят"), ("es", "3.º de noviembrecito"),
        ):
            with self.subTest(language=language, text=text):
                self.assertEqual(self.composed(text, language), text)

    def test_non_date_ordinals_keep_number_format(self):
        for language, text, expected in (
            ("ru", "третье место", "3-е место"),
            ("es", "tercero puesto", "3.º puesto"),
        ):
            with self.subTest(language=language):
                self.assertEqual(self.composed(text, language), expected)

    def test_es_bare_hour_control(self):
        self.assertEqual(self.composed("a las 16", "es"), "16:00")


if __name__ == "__main__":
    unittest.main()
