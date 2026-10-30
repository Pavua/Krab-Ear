"""Number → DateTime: поддержанный словесный дательный не теряет календарный смысл."""
from pathlib import Path
from types import ModuleType
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPACES = (' ', '  ', '\t', '\n', '\u00a0')


def load_source(name):
    path = ROOT / 'core' / (name + '.py')
    module = ModuleType('ear_dative_' + name)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


class DativeOrdinalDates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.numbers = load_source('number_normalizer').NumberNormalizer()
        cls.dates = load_source('datetime_normalizer').DateTimeNormalizer(output_format='european')

    def composed(self, text):
        return self.dates.normalize(self.numbers.normalize(text, 'ru'), 'ru')

    def test_existing_spoken_dative_survives_number_stage(self):
        for text, expected in (
            ('к первому мая', 'к 01.05'),
            ('ко второму ноября', 'ко 02.11'),
            ('к третьему января 2026 года', 'к 03.01.2026'),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.dates.normalize(text, 'ru'), expected)
                self.assertEqual(self.composed(text), expected)

    def test_numeric_dative_uses_existing_calendar_validation(self):
        for text, expected in (
            ('к 1-му мая', 'к 01.05'), ('ко 2-му ноября', 'ко 02.11'),
            ('к 3-му января 2026 года', 'к 03.01.2026'),
            ('к 31-му декабря', 'к 31.12'), ('29-му февраля', '29.02'),
            ('16:35 3-му ноября', '16:35 03.11'),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.dates.normalize(text, 'ru'), expected)
                self.assertEqual(self.composed(text), expected)

    def test_composition_is_idempotent(self):
        for text in ('к первому мая', 'ко второму ноября', 'к третьему января 2026 года',
                     'к 31-му декабря', '16:35 3-му ноября'):
            with self.subTest(text=text):
                once = self.composed(text)
                self.assertEqual(self.composed(once), once)

    def test_adjacent_numeric_prefix_does_not_create_partial_date(self):
        for space in SPACES:
            for text in ('20' + space + '3-му ноября', 'к двадцать' + space + 'третьему ноября'):
                with self.subTest(text=text):
                    intermediate = self.numbers.normalize(text, 'ru')
                    self.assertEqual(self.dates.normalize(intermediate, 'ru'), intermediate)

    def test_unconsumed_year_continuations_remain_unchanged(self):
        for space in SPACES:
            for tail in ('20260 года', '2026 годами', '2026xyz года',
                         'две тысячи шестнадцатого года'):
                with self.subTest(space=repr(space), tail=tail):
                    text = self.numbers.normalize('к третьему ноября' + space + tail, 'ru')
                    self.assertEqual(self.dates.normalize(text, 'ru'), text)

    def test_invalid_tokens_dates_and_non_date_datives_are_preserved(self):
        for text in ('QA3-му ноября', '113-му ноября', '32-му ноября', '0-му ноября',
                     '31-му апреля', '30-му февраля', '3-му ноябрят', '3-мух ноября',
                     '3-му месту', 'к четвёртому мая', '3-го ноябряX'):
            with self.subTest(text=text):
                self.assertEqual(self.dates.normalize(text, 'ru'), text)
                self.assertEqual(self.composed(text), text)

    def test_non_date_spoken_dative_keeps_number_suffix(self):
        self.assertEqual(self.composed('к третьему месту'), 'к 3-му месту')


if __name__ == '__main__':
    unittest.main(argv=[__file__], verbosity=2)
