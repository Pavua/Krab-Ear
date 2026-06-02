"""Тесты для wave-20 исправлений PII-обходов в TextAnonymizer.

Покрывает три исправления:
  MED-1 (phone_es)    — испанские телефоны не редактировались (+34 / без кода).
  MED-2 (iban)        — IBAN с пробельными разделителями 4-знак не редактировался.
  LOW-3 (credit_card) — 15-значные Amex + точечные разделители пропускались.

Каждый класс проверяет:
  a) Позитивные случаи — PII теперь редактируется.
  b) Негативные / false-positive-guard — нет ложных срабатываний.
  c) ReDoS guard — adversarial длинный ввод завершается < 200 ms.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.text_anonymizer import TextAnonymizer  # noqa: E402

_PHONE_TOKEN = "[ТЕЛЕФОН]"
_IBAN_TOKEN = "[IBAN]"
_CARD_TOKEN = "[КАРТА]"


# ── FINDING 1 (MED): испанские телефоны ─────────────────────────────────────

class TestESPhoneRedaction(unittest.TestCase):
    """phone_es rule redacts Spanish mobile and landline numbers."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    # ── Позитивные случаи ─────────────────────────────────────────────────

    def test_es_mobile_with_country_code(self) -> None:
        """+34 612 34 56 78 должен редактироваться."""
        r = self.a.anonymize("+34 612 34 56 78")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_PHONE_TOKEN, r.anonymized_text)
        self.assertNotIn("+34 612 34 56 78", r.anonymized_text)

    def test_es_mobile_no_spaces(self) -> None:
        """+34612345678 без пробелов."""
        r = self.a.anonymize("+34612345678")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_PHONE_TOKEN, r.anonymized_text)

    def test_es_mobile_dashes(self) -> None:
        """+34-612-34-56-78 с дефисами."""
        r = self.a.anonymize("Llama a +34-612-34-56-78 por favor.")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_PHONE_TOKEN, r.anonymized_text)

    def test_es_landline_local(self) -> None:
        """918 123 456 — мадридский стационарный без кода страны."""
        r = self.a.anonymize("918 123 456")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_PHONE_TOKEN, r.anonymized_text)

    def test_es_mobile_local_no_sep(self) -> None:
        """612345678 — испанский мобильный без разделителей."""
        r = self.a.anonymize("Mi numero es 612345678")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_PHONE_TOKEN, r.anonymized_text)

    def test_es_category_name(self) -> None:
        """Категория редактирования — phone_es."""
        r = self.a.anonymize("+34 912 345 678")
        self.assertEqual(r.redaction_count, 1)
        self.assertEqual(r.redactions[0].category, "phone_es")

    def test_es_in_mixed_text(self) -> None:
        """Испанский + русский телефоны в одном тексте — оба редактируются."""
        text = "Москва: +7 999 123-45-67, Madrid: +34 912 345 678."
        r = self.a.anonymize(text)
        self.assertEqual(r.redaction_count, 2)
        self.assertNotIn("+7 999", r.anonymized_text)
        self.assertNotIn("+34 912", r.anonymized_text)

    # ── Негативные / false-positive guard ────────────────────────────────

    def test_es_does_not_match_short_number(self) -> None:
        """123456 — слишком короткий, не должен редактироваться."""
        r = self.a.anonymize("Код 123456 от банка")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("phone_es", cats)

    def test_es_does_not_match_ru_phone(self) -> None:
        """RU +7 номер не должен попасть в phone_es."""
        r = self.a.anonymize("+79991234567")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("phone_es", cats)
        # Должен редактироваться как phone (RU)
        self.assertEqual(r.redaction_count, 1)

    def test_es_does_not_match_de_phone(self) -> None:
        """DE +49 номер не должен дублироваться в phone_es."""
        r = self.a.anonymize("+4915112345678")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("phone_es", cats)

    # ── ReDoS guard ──────────────────────────────────────────────────────

    def test_es_redos_safe(self) -> None:
        """Adversarial ввод из +34 и пробелов завершается < 200 ms."""
        adversarial = "+34" + " " * 200 + "6"
        start = time.monotonic()
        self.a.anonymize(adversarial)
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 200, f"phone_es took {elapsed_ms:.1f} ms (ReDoS?)")


# ── FINDING 2 (MED): IBAN с пробельными разделителями ───────────────────────

class TestSpacedIBANRedaction(unittest.TestCase):
    """IBAN written in 4-char groups (as dictated) is now redacted."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    # ── Позитивные случаи ─────────────────────────────────────────────────

    def test_de_iban_with_spaces(self) -> None:
        """DE89 3704 0044 0532 0130 00 — типичный диктованный формат."""
        r = self.a.anonymize("DE89 3704 0044 0532 0130 00")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_IBAN_TOKEN, r.anonymized_text)
        self.assertNotIn("DE89", r.anonymized_text)

    def test_gb_iban_contiguous(self) -> None:
        """GB82WEST12345698765432 — сплошной (раньше работал, должен работать и сейчас)."""
        r = self.a.anonymize("IBAN: GB82WEST12345698765432")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_IBAN_TOKEN, r.anonymized_text)

    def test_iban_with_dashes(self) -> None:
        """DE89-3704-0044-0532-0130-00 — с дефисами."""
        r = self.a.anonymize("IBAN: DE89-3704-0044-0532-0130-00")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_IBAN_TOKEN, r.anonymized_text)

    def test_iban_category(self) -> None:
        """Категория редактирования — iban."""
        r = self.a.anonymize("DE89 3704 0044 0532 0130 00")
        self.assertEqual(r.redactions[0].category, "iban")

    # ── Mod-97 gate — предотвращает false positives ───────────────────────

    def test_fake_iban_not_redacted(self) -> None:
        """DE12 hello — не является IBAN, mod-97 должен отклонить."""
        r = self.a.anonymize("DE12 hello")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("iban", cats)

    def test_wrong_checkdigits_not_redacted(self) -> None:
        """DE00 3704 0044 0532 0130 00 — неверные check-digits (mod-97 fail)."""
        r = self.a.anonymize("DE00 3704 0044 0532 0130 00")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("iban", cats)

    def test_short_sequence_not_iban(self) -> None:
        """DE12 — слишком короткий (< 14 символов IBAN)."""
        r = self.a.anonymize("Номер DE12 в коде")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("iban", cats)

    # ── ReDoS guard ──────────────────────────────────────────────────────

    def test_iban_redos_safe(self) -> None:
        """Adversarial: DE99 + повторяющиеся 4-char блоки завершается < 200 ms."""
        adversarial = "DE99" + " ABCD" * 50
        start = time.monotonic()
        self.a.anonymize(adversarial)
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 200, f"iban took {elapsed_ms:.1f} ms (ReDoS?)")


# ── FINDING 3 (LOW): Amex + точечные разделители ────────────────────────────

class TestAmexCreditCardRedaction(unittest.TestCase):
    """15-digit Amex cards (valid Luhn) are now redacted."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    # Публично известные тестовые номера Amex (проходят Luhn)
    # 378282246310005 — стандартный Amex тест-номер Visa/Mastercard spec
    # 371449635398431 — второй распространённый тест-номер

    # ── Позитивные случаи ─────────────────────────────────────────────────

    def test_amex_15digit_contiguous(self) -> None:
        """378282246310005 — сплошной 15-значный Amex."""
        r = self.a.anonymize("378282246310005")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_amex_grouped_4_6_5(self) -> None:
        """3782 822463 10005 — стандартная группировка Amex 4-6-5."""
        r = self.a.anonymize("3782 822463 10005")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_amex_dashes_format(self) -> None:
        """3782-822463-10005 — с дефисами."""
        r = self.a.anonymize("Card: 3782-822463-10005")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_amex_34_prefix(self) -> None:
        """340000000000009 — Amex с префиксом 34."""
        r = self.a.anonymize("340000000000009")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_amex_second_test_number(self) -> None:
        """371449635398431 — второй стандартный Amex тест-номер."""
        r = self.a.anonymize("371449635398431")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_16digit_visa_still_works(self) -> None:
        """Существующий 16-значный Visa (4111111111111111) должен работать."""
        r = self.a.anonymize("4111111111111111")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    def test_16digit_grouped_still_works(self) -> None:
        """4111 1111 1111 1111 — группами по 4."""
        r = self.a.anonymize("4111 1111 1111 1111")
        self.assertEqual(r.redaction_count, 1)
        self.assertIn(_CARD_TOKEN, r.anonymized_text)

    # ── Luhn gate — не допускаем false positives ─────────────────────────

    def test_amex_invalid_luhn_not_redacted(self) -> None:
        """378282246310000 — похожий на Amex, но не проходит Luhn."""
        r = self.a.anonymize("378282246310000")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("credit_card", cats)

    def test_random_15digit_not_redacted(self) -> None:
        """100000000000000 — 15 цифр, не Amex-префикс, не проходит Luhn."""
        r = self.a.anonymize("100000000000000")
        cats = {red.category for red in r.redactions}
        self.assertNotIn("credit_card", cats)

    # ── ReDoS guard ──────────────────────────────────────────────────────

    def test_credit_card_redos_safe(self) -> None:
        """Adversarial: 3782 + много пробелов < 200 ms."""
        adversarial = "3782" + " 8" * 100
        start = time.monotonic()
        self.a.anonymize(adversarial)
        elapsed_ms = (time.monotonic() - start) * 1000
        self.assertLess(elapsed_ms, 200, f"credit_card took {elapsed_ms:.1f} ms (ReDoS?)")


# ── Интеграционный тест: все три исправления вместе ─────────────────────────

class TestAllThreeFixesTogether(unittest.TestCase):
    """Интеграция: текст с ES телефоном + пробельным IBAN + Amex картой."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    def test_combined_pii_in_one_text(self) -> None:
        """ES телефон + DE IBAN с пробелами + Amex карта — все три редактируются."""
        text = (
            "Teléfono: +34 912 345 678. "
            "IBAN: DE89 3704 0044 0532 0130 00. "
            "Tarjeta: 3782 822463 10005."
        )
        r = self.a.anonymize(text)

        cats = {red.category for red in r.redactions}
        self.assertIn("phone_es", cats, "ES phone not redacted")
        self.assertIn("iban", cats, "Spaced IBAN not redacted")
        self.assertIn("credit_card", cats, "Amex not redacted")

        self.assertNotIn("+34 912 345 678", r.anonymized_text)
        self.assertNotIn("DE89 3704", r.anonymized_text)
        self.assertNotIn("3782 822463", r.anonymized_text)


if __name__ == "__main__":
    unittest.main()
