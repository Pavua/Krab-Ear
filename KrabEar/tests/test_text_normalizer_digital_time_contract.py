"""Синтетический post-STT контракт; не ASR, backend или состояние диалога.

Запускать отдельно stdlib unittest после согласованного START. Загрузка по
точному пути обходит core/__init__ и pytest/conftest. Два настоящих модуля
исполняются целиком; порядок соответствует engine Number → DateTime.
"""

from pathlib import Path
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_source(name):
    path = ROOT / "core" / (name + ".py")
    module = ModuleType("ear_contract_" + name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class DigitalTimeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.numbers = load_source("number_normalizer").NumberNormalizer()
        cls.dates = load_source("datetime_normalizer").DateTimeNormalizer(
            output_format="european"
        )

    def normalize(self, text, language):
        return self.dates.normalize(
            self.numbers.normalize(text, language), language
        )

    def test_es_anchored_digital_minutes_are_preserved(self):
        for fragment in ("16:35", "9:35", "1234", "9:5", "16:350"):
            text = "LunaTest, cita a las " + fragment + "; código QA0472."
            with self.subTest(fragment=fragment):
                self.assertEqual(self.normalize(text, "es"), text)

    def test_es_bare_hour_still_normalizes(self):
        self.assertEqual(self.normalize("a las 16", "es"), "16:00")

    def test_existing_digital_atoms_are_preserved(self):
        text = "LunaTest QA0472 QA0427 16:35 17.11.2026 19.11.2026"
        for language in ("ru", "es"):
            with self.subTest(language=language):
                self.assertEqual(self.normalize(text, language), text)

    def test_es_correction_keeps_both_mentions_and_codes(self):
        text = (
            "LunaTest, el 17 de noviembre de 2026; no, el 19 de noviembre "
            "de 2026. QA0472; corrijo, QA0427."
        )
        expected = (
            "LunaTest, 17.11.2026; no, 19.11.2026. "
            "QA0472; corrijo, QA0427."
        )
        self.assertEqual(self.normalize(text, "es"), expected)

    def test_truncation_does_not_invent_missing_fields(self):
        for language, text in (
            ("es", "LunaTest, el dieci..."),
            ("ru", "LunaTest, девят..."),
        ):
            with self.subTest(language=language):
                self.assertEqual(self.normalize(text, language), text)


if __name__ == "__main__":
    unittest.main()
