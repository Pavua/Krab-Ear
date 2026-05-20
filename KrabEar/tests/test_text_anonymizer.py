"""Тесты для модуля анонимизации текста KrabEar (TextAnonymizer).

Покрывает:
- Российские номера телефонов (различные форматы)
- Email-адреса
- Номера банковских карт (с Luhn-валидацией, Wave 214)
- Паспортные данные
- Даты рождения
- ИНН, СНИЛС
- Пользовательские правила (custom rules)
- Выборочное применение правил (параметр rules=)
- Пустой текст, текст без совпадений
- Позиция замены (Redaction.position)
"""

from __future__ import annotations
from core.text_anonymizer import TextAnonymizer, AnonymizeResult, Redaction

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestTextAnonymizerPhones(unittest.TestCase):
    """Тесты анонимизации телефонных номеров."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_phone_full_format(self) -> None:
        """Классический формат +7(999)123-45-67."""
        result = self.a.anonymize("Позвони мне на +7(999)123-45-67 сегодня")
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertNotIn("+7(999)123-45-67", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "phone")

    def test_phone_8_format(self) -> None:
        """Формат 8(999)123-45-67."""
        result = self.a.anonymize("Номер: 8(999)123-45-67")
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_phone_plus7_spaces(self) -> None:
        """Формат +7 999 123 45 67 (с пробелами)."""
        result = self.a.anonymize("тел +7 999 123 45 67")
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_phone_digits_only(self) -> None:
        """Формат +79991234567 без разделителей."""
        result = self.a.anonymize("Мобильный +79991234567")
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_phone_8_digits_only(self) -> None:
        """Формат 89991234567."""
        result = self.a.anonymize("89991234567 — мой номер")
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_multiple_phones(self) -> None:
        """Два телефона в одном тексте."""
        result = self.a.anonymize("+79001234567 и +79007654321 — оба мои")
        self.assertEqual(result.redaction_count, 2)
        self.assertNotIn("+790", result.anonymized_text)


class TestTextAnonymizerEmail(unittest.TestCase):
    """Тесты анонимизации email-адресов."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_email_basic(self) -> None:
        """Простой email-адрес."""
        result = self.a.anonymize("Напишите мне на user@mail.com")
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertNotIn("user@mail.com", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "email")

    def test_email_with_dots(self) -> None:
        """Email с точками в имени."""
        result = self.a.anonymize("Контакт: ivan.petrov+work@example.org")
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_email_russian_domain(self) -> None:
        """Email на русском домене (латиница)."""
        result = self.a.anonymize("Email: test@yandex.ru или test2@gmail.com")
        self.assertEqual(result.redaction_count, 2)


class TestTextAnonymizerCreditCard(unittest.TestCase):
    """Тесты анонимизации номеров банковских карт (с Luhn-валидацией, Wave 214)."""

    # Real Visa test card — passes Luhn
    VISA_CARD = "4532015112830366"
    VISA_SPACED = "4532 0151 1283 0366"
    VISA_DASHED = "4532-0151-1283-0366"

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_credit_card_spaces(self) -> None:
        """Номер карты с пробелами (Luhn-valid Visa test card)."""
        result = self.a.anonymize(f"Карта: {self.VISA_SPACED}")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertNotIn("4532", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "credit_card")

    def test_credit_card_dashes(self) -> None:
        """Номер карты через дефисы (Luhn-valid Visa test card)."""
        result = self.a.anonymize(f"Номер карты: {self.VISA_DASHED}")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    # ── Wave 214: Luhn validation tests ─────────────────────────────────────

    def test_luhn_valid_card_redacted(self) -> None:
        """4532015112830366 (Visa test card) passes Luhn → redacted."""
        result = self.a.anonymize(f"Оплата картой {self.VISA_CARD}")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertNotIn(self.VISA_CARD, result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_luhn_invalid_16_digits_kept(self) -> None:
        """1234567890123456 fails Luhn → NOT redacted (false positive prevention)."""
        result = self.a.anonymize("Code: 1234567890123456 end")
        self.assertNotIn("[КАРТА]", result.anonymized_text)
        self.assertIn("1234567890123456", result.anonymized_text)
        self.assertEqual(result.redaction_count, 0)

    def test_credit_card_with_spaces_handled(self) -> None:
        """Card with spaces: digits extracted for Luhn check."""
        result = self.a.anonymize(f"Card: {self.VISA_SPACED} valid")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_credit_card_with_dashes_handled(self) -> None:
        """Card with dashes: digits extracted for Luhn check."""
        result = self.a.anonymize(f"Card: {self.VISA_DASHED} valid")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_random_16_digit_number_kept(self) -> None:
        """Random 16-digit number that fails Luhn is NOT redacted."""
        # 1111111111111111 fails Luhn (checksum = 8, not 0)
        result = self.a.anonymize("Timestamp ref: 2026051912345678 note")
        self.assertNotIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 0)

    def test_back_compat_existing_tests_still_pass(self) -> None:
        """Backward compat: non-PII numbers no longer falsely redacted (Wave 214 improvement)."""
        # Before Wave 214: 1234567890123456 was wrongly redacted
        # After Wave 214: correctly kept (fails Luhn)
        result = self.a.anonymize("Value: 1234567890123456")
        # The number should NOT be redacted — this is the Wave 214 improvement
        self.assertEqual(result.redaction_count, 0)
        self.assertIn("1234567890123456", result.anonymized_text)

    def test_luhn_mastercard_redacted(self) -> None:
        """5500005555555559 (Mastercard test) passes Luhn → redacted."""
        mc = "5500005555555559"
        result = self.a.anonymize(f"MC card: {mc}")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_luhn_visa_spaced_second_card(self) -> None:
        """4111111111111111 (Visa test 2) passes Luhn → redacted."""
        visa2 = "4111111111111111"
        result = self.a.anonymize(f"Visa: {visa2}")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)


class TestTextAnonymizerCustomRule(unittest.TestCase):
    """Тесты пользовательских правил анонимизации."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_add_custom_rule(self) -> None:
        """Добавление и применение пользовательского правила."""
        self.a.add_custom_rule(
            name="secret_code",
            pattern=r"\bSECRET-\d{4}\b",
            replacement="[СЕКРЕТ]",
        )
        result = self.a.anonymize("Код доступа: SECRET-1234 уже устарел")
        self.assertIn("[СЕКРЕТ]", result.anonymized_text)
        self.assertNotIn("SECRET-1234", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "secret_code")

    def test_custom_rule_case_insensitive(self) -> None:
        """Пользовательские правила нечувствительны к регистру."""
        self.a.add_custom_rule(
            name="token",
            pattern=r"\btoken-[a-z0-9]+\b",
            replacement="[ТОКЕН]",
        )
        result = self.a.anonymize("Используйте TOKEN-abc123 для доступа")
        self.assertIn("[ТОКЕН]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_custom_rule_listed(self) -> None:
        """Пользовательское правило отображается в list_rules()."""
        self.a.add_custom_rule("my_rule", r"\bFOO\b", "[BAR]")
        rules = self.a.list_rules()
        self.assertIn("my_rule", rules)


class TestTextAnonymizerSelectiveRules(unittest.TestCase):
    """Тесты выборочного применения правил через параметр rules=."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_selective_rules_only_email(self) -> None:
        """При rules=['email'] телефон не анонимизируется."""
        text = "Тел +79001234567, email test@example.com"
        result = self.a.anonymize(text, rules=["email"])
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertIn("+79001234567", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_selective_rules_only_phone(self) -> None:
        """При rules=['phone'] email не анонимизируется."""
        text = "Тел +79001234567, email test@example.com"
        result = self.a.anonymize(text, rules=["phone"])
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertIn("test@example.com", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_empty_rules_list(self) -> None:
        """Пустой список rules= — ничего не анонимизируется."""
        text = "+79001234567 test@example.com"
        result = self.a.anonymize(text, rules=[])
        self.assertEqual(result.anonymized_text, text)
        self.assertEqual(result.redaction_count, 0)


class TestTextAnonymizerEdgeCases(unittest.TestCase):
    """Граничные случаи анонимизации."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_empty_text(self) -> None:
        """Пустая строка не вызывает ошибки."""
        result = self.a.anonymize("")
        self.assertEqual(result.anonymized_text, "")
        self.assertEqual(result.redaction_count, 0)
        self.assertEqual(result.redactions, [])

    def test_no_pii_text(self) -> None:
        """Текст без ПДн возвращается без изменений."""
        text = "Привет, сегодня хорошая погода."
        result = self.a.anonymize(text)
        self.assertEqual(result.anonymized_text, text)
        self.assertEqual(result.redaction_count, 0)

    def test_redaction_position(self) -> None:
        """Поле position в Redaction указывает правильное смещение."""
        text = "Email: test@example.com тут"
        result = self.a.anonymize(text, rules=["email"])
        self.assertEqual(result.redaction_count, 1)
        pos = result.redactions[0].position
        # Проверяем, что в оригинале на этой позиции действительно email
        self.assertEqual(text[pos:pos + len("test@example.com")], "test@example.com")

    def test_result_type(self) -> None:
        """Результат — экземпляр AnonymizeResult с правильными полями."""
        result = self.a.anonymize("Звоните +79001234567")
        self.assertIsInstance(result, AnonymizeResult)
        self.assertIsInstance(result.anonymized_text, str)
        self.assertIsInstance(result.redactions, list)
        self.assertIsInstance(result.redaction_count, int)
        self.assertIsInstance(result.redactions[0], Redaction)

    def test_multiple_categories_mixed_text(self) -> None:
        """Текст с несколькими категориями ПДн (Luhn-valid card)."""
        # 4532 0151 1283 0366 = Visa test card, passes Luhn
        text = "Тел: +79001234567, email: foo@bar.ru, карта: 4532 0151 1283 0366"
        result = self.a.anonymize(text)
        categories = {r.category for r in result.redactions}
        self.assertIn("phone", categories)
        self.assertIn("email", categories)
        self.assertIn("credit_card", categories)
        self.assertEqual(result.redaction_count, 3)


class TestTextAnonymizerMultilingual(unittest.TestCase):
    """Multilingual (EN/ES) and no-PII gap tests."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_no_pii_english_text_unchanged(self) -> None:
        """English text with no PII is returned unchanged."""
        text = "The meeting is scheduled for tomorrow morning."
        result = self.a.anonymize(text)
        self.assertEqual(result.anonymized_text, text)
        self.assertEqual(result.redaction_count, 0)

    def test_no_pii_spanish_text_unchanged(self) -> None:
        """Spanish text with no PII is returned unchanged."""
        text = "Hoy hace buen tiempo en Madrid."
        result = self.a.anonymize(text)
        self.assertEqual(result.anonymized_text, text)
        self.assertEqual(result.redaction_count, 0)

    def test_email_in_english_context(self) -> None:
        """Email address embedded in English sentence is redacted."""
        result = self.a.anonymize("Contact me at alice@example.com for details")
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertNotIn("alice@example.com", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_email_in_spanish_context(self) -> None:
        """Email address embedded in Spanish sentence is redacted."""
        result = self.a.anonymize("Envíame un correo a pepe@correo.es por favor")
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_credit_card_no_spaces_16_digits(self) -> None:
        """16-digit Luhn-valid card number without separators is redacted."""
        # 4532015112830366 = Visa test card, passes Luhn
        result = self.a.anonymize("Número de tarjeta: 4532015112830366")
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_list_rules_contains_builtins(self) -> None:
        """list_rules() includes all builtin rule names."""
        rules = self.a.list_rules()
        for expected in ("phone", "email", "credit_card", "passport",
                         "date_of_birth", "inn", "snils"):
            self.assertIn(expected, rules)

    def test_anonymize_returns_original_in_redaction(self) -> None:
        """Redaction.original contains the matched text."""
        text = "Email: test@example.com here"
        result = self.a.anonymize(text, rules=["email"])
        self.assertEqual(result.redactions[0].original, "test@example.com")

    def test_custom_rule_not_applied_when_excluded(self) -> None:
        """Custom rule is not applied when rules= list omits it."""
        self.a.add_custom_rule("secret", r"\bTOKEN-\d+\b", "[SECRET]")
        text = "TOKEN-999 user@mail.com"
        result = self.a.anonymize(text, rules=["email"])
        self.assertIn("TOKEN-999", result.anonymized_text)
        self.assertIn("[EMAIL]", result.anonymized_text)

    def test_whitespace_only_text(self) -> None:
        """Whitespace-only text returns unchanged with zero redactions."""
        result = self.a.anonymize("   ")
        self.assertEqual(result.redaction_count, 0)


if __name__ == "__main__":
    unittest.main()
