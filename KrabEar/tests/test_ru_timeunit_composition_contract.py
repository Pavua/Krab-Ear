"""Синтетический контракт RU clock/unit composition, без backend/ASR."""
from pathlib import Path
from types import ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLOCK_CASES = (
    ("в десять часов", "10:00"),
    ("девять часов утра", "09:00"),
    ("семь часов вечера", "19:00"),
    ("в девять часов тридцать минут утра", "09:30"),
)


def load_source(name):
    path = ROOT / "core" / (name + ".py")
    module = ModuleType("ear_ru_timeunit_" + name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class RussianTimeUnitComposition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.numbers = load_source("number_normalizer").NumberNormalizer()
        cls.dates = load_source("datetime_normalizer").DateTimeNormalizer(
            output_format="european"
        )

    def composed(self, text, language="ru"):
        return self.dates.normalize(self.numbers.normalize(text, language), language)

    def test_documented_anchored_clock_direct(self):
        for text, expected in CLOCK_CASES:
            with self.subTest(text=text):
                self.assertEqual(self.dates.normalize(text, "ru"), expected)

    def test_number_stage_preserves_anchored_clock(self):
        for text, expected in CLOCK_CASES:
            with self.subTest(text=text):
                intermediate = self.numbers.normalize(text, "ru")
                self.assertEqual(
                    self.dates.normalize(intermediate, "ru"), expected,
                    "Number output: " + repr(intermediate),
                )

    def test_durations_keep_number_unit_format(self):
        for text, expected in (
            ("через два часа", "через 2 ч"),
            ("за тридцать минут", "за 30 мин"),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.composed(text), expected)

    def test_existing_digital_atoms_preserved(self):
        text = "LunaTest QA0472 03.11 16:35"
        for language in ("ru", "es"):
            with self.subTest(language=language):
                self.assertEqual(self.composed(text, language), text)

    def test_abbreviation_negative_controls(self):
        for text in (
            "9 ч", "9 ч 30 мин", "через 2 ч", "за 30 мин",
            "в 9 ч 750 мин утра", "в 109 ч утра", "QA9 ч вечера",
            "в 9 чай", "9 ч утрамбовка", "в 24 ч утра",
            "в 9 ч 123 мин утра", "в 9 чXYZ утра",
            "в 9 ч 30 минус утра", "QA109 ч утра",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.composed(text), text)

    def test_invalid_and_code_tokens_preserved(self):
        for text in (
            "QA9 ч утра", "109 ч утра",
            "в 25 ч утра", "в 9 ч 75 мин утра",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.composed(text), text)


if __name__ == "__main__":
    unittest.main()
