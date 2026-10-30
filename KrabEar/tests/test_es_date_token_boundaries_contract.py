"""Синтетические ES token boundaries; без ASR и разрешения коррекций диалога."""

from pathlib import Path
from types import ModuleType
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "core/datetime_normalizer.py"


class SpanishDateTokenBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module = ModuleType("ear_date_boundary_source")
        module.__file__ = str(SOURCE)
        exec(compile(SOURCE.read_bytes(), str(SOURCE), "exec"), module.__dict__)
        cls.normalizer = module.DateTimeNormalizer(output_format="european")

    def normalize(self, text):
        return self.normalizer.normalize(text, "es")

    def test_embedded_day_is_not_a_date(self):
        for token in ("QA0417", "117", "A17"):
            text = "LunaTest " + token + " de noviembre"
            with self.subTest(token=token):
                self.assertEqual(self.normalize(text), text)

    def test_month_prefix_is_not_a_month(self):
        for token in ("maravilla", "noviembrecito"):
            text = "LunaTest 3 de " + token
            with self.subTest(token=token):
                self.assertEqual(self.normalize(text), text)

    def test_year_prefix_is_not_a_complete_year(self):
        for token in ("20260", "2026abc"):
            text = "LunaTest 17 de noviembre de " + token
            with self.subTest(token=token):
                self.assertEqual(self.normalize(text), text)

    def test_valid_dates_still_normalize(self):
        for day in (17, 19):
            with self.subTest(day=day):
                self.assertEqual(
                    self.normalize("LunaTest el %d de noviembre de 2026" % day),
                    "LunaTest %d.11.2026" % day,
                )

    def test_valid_date_without_year_still_normalizes(self):
        self.assertEqual(self.normalize("LunaTest 3 de noviembre"), "LunaTest 03.11")

    def test_previous_digital_time_fix_is_preserved(self):
        text = "LunaTest a las 16:35 QA0427"
        self.assertEqual(self.normalize(text), text)


if __name__ == "__main__":
    unittest.main()
