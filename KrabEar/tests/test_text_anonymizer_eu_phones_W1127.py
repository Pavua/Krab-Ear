"""Тесты EU phone patterns для TextAnonymizer (W1122 N1 HIGH).

Покрывает:
- +44 UK телефоны (мобильные, лондонские, с (0))
- +49 DE телефоны (берлинский, мобильный, с (0))
- +33 FR телефоны (Париж, мобильные, с (0))
- +39 IT телефоны (Рим, мобильные, с (0))
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.text_anonymizer import TextAnonymizer  # noqa: E402


class TestEUPhonesUK(unittest.TestCase):
    """UK (+44) phone number detection."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_uk_mobile_basic(self) -> None:
        """Стандартный UK мобильный: +44 7911 123456."""
        result = self.a.anonymize("Call me at +44 7911 123456 tomorrow.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertNotIn("+44 7911 123456", result.anonymized_text)
        self.assertEqual(result.redactions[0].category, "phone_uk")

    def test_uk_london_landline(self) -> None:
        """Лондонский стационарный: +44 20 7946 0958."""
        result = self.a.anonymize("Office: +44 20 7946 0958.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_uk_with_zero_prefix(self) -> None:
        """UK с (0): +44(0)7911 123456."""
        result = self.a.anonymize("Reach me at +44(0)7911 123456.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_uk_dashes_format(self) -> None:
        """UK с дефисами: +44-7700-900123."""
        result = self.a.anonymize("Number: +44-7700-900123 for details.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)


class TestEUPhonesDE(unittest.TestCase):
    """DE (+49) phone number detection."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_de_mobile_basic(self) -> None:
        """Немецкий мобильный: +49 151 12345678."""
        result = self.a.anonymize("Ruf mich an: +49 151 12345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertNotIn("+49 151 12345678", result.anonymized_text)
        self.assertEqual(result.redactions[0].category, "phone_de")

    def test_de_berlin_landline(self) -> None:
        """Берлинский стационарный: +49 30 12345678."""
        result = self.a.anonymize("Büro: +49 30 12345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_de_with_zero_prefix(self) -> None:
        """DE с (0): +49(0)30 12345678."""
        result = self.a.anonymize("Kontakt: +49(0)30 12345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_de_no_spaces(self) -> None:
        """DE без пробелов: +4915112345678."""
        result = self.a.anonymize("Tel: +4915112345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)


class TestEUPhonesFR(unittest.TestCase):
    """FR (+33) phone number detection."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_fr_paris_landline(self) -> None:
        """Парижский стационарный: +33 1 23 45 67 89."""
        result = self.a.anonymize("Appelez-moi: +33 1 23 45 67 89.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertNotIn("+33 1 23 45 67 89", result.anonymized_text)
        self.assertEqual(result.redactions[0].category, "phone_fr")

    def test_fr_mobile(self) -> None:
        """Французский мобильный: +33 6 12 34 56 78."""
        result = self.a.anonymize("Mobile: +33 6 12 34 56 78.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_fr_with_zero_prefix(self) -> None:
        """FR с (0): +33(0)6 12 34 56 78."""
        result = self.a.anonymize("Contact: +33(0)6 12 34 56 78.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_fr_no_spaces(self) -> None:
        """FR без пробелов: +33612345678."""
        result = self.a.anonymize("Tel: +33612345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)


class TestEUPhonesIT(unittest.TestCase):
    """IT (+39) phone number detection."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_it_rome_landline(self) -> None:
        """Римский стационарный: +39 06 12345678."""
        result = self.a.anonymize("Chiamami: +39 06 12345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertNotIn("+39 06 12345678", result.anonymized_text)
        self.assertEqual(result.redactions[0].category, "phone_it")

    def test_it_mobile(self) -> None:
        """Итальянский мобильный: +39 333 1234567."""
        result = self.a.anonymize("Cellulare: +39 333 1234567.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_it_with_zero_prefix(self) -> None:
        """IT с (0): +39(0)6 12345678."""
        result = self.a.anonymize("Ufficio: +39(0)6 12345678.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)

    def test_it_no_spaces(self) -> None:
        """IT без пробелов: +393331234567."""
        result = self.a.anonymize("Tel: +393331234567.")
        self.assertEqual(result.redaction_count, 1)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)


class TestEUPhonesRussianTextMixed(unittest.TestCase):
    """Смешанный EU+RU текст с несколькими номерами."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_mixed_ru_and_eu_phones(self) -> None:
        """RU + UK + DE номера в одном тексте."""
        text = "Россия: +7 999 123-45-67, Лондон: +44 7911 123456, Берлин: +49 30 12345678."
        result = self.a.anonymize(text)
        self.assertEqual(result.redaction_count, 3)
        self.assertNotIn("+7 999 123-45-67", result.anonymized_text)
        self.assertNotIn("+44 7911 123456", result.anonymized_text)
        self.assertNotIn("+49 30 12345678", result.anonymized_text)

    def test_all_four_eu_countries(self) -> None:
        """Все 4 EU страны в одном тексте."""
        text = (
            "UK: +44 7700 900123, "
            "DE: +49 151 12345678, "
            "FR: +33 6 12 34 56 78, "
            "IT: +39 333 1234567."
        )
        result = self.a.anonymize(text)
        self.assertEqual(result.redaction_count, 4)
        categories = {r.category for r in result.redactions}
        self.assertIn("phone_uk", categories)
        self.assertIn("phone_de", categories)
        self.assertIn("phone_fr", categories)
        self.assertIn("phone_it", categories)


if __name__ == "__main__":
    unittest.main()
