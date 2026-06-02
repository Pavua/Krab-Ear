"""Регрессионные тесты утечки PII на границе обрезки TextAnonymizer (wave1766 MED).

Уязвимость (до исправления):
    Если PII-токен начинается до символа _MAX_ANONYMIZE_LEN и выходит за его пределы,
    anonymize() обрезал сканируемый текст на _MAX_ANONYMIZE_LEN → регулярные выражения
    не видели токен → «хвост» text[_MAX_ANONYMIZE_LEN:] присоединялся нередактированным.

Исправление (wave1766):
    Окно сканирования расширено до _MAX_ANONYMIZE_LEN + _MAX_PII_LEN (= 500_064),
    так что токены, пересекающие исходную границу, полностью попадают в поле зрения
    регулярных выражений. «Хвост» отсчитывается от расширенной границы.

Тесты:
    test_card_straddling_boundary     — 16-значный номер карты (Luhn-valid Visa) начинается
                                        за несколько символов до границы → должен редактироваться
    test_phone_straddling_boundary    — российский телефон (+79991234567) пересекает границу
    test_email_straddling_boundary    — email пересекает границу
    test_normal_short_text_unchanged  — короткий текст: редактирование и несекретный контент
                                        работают как раньше
    test_pii_wholly_in_tail_untouched — PII целиком за пределами расширенного окна
                                        (т.е. >500_064) — это допустимо: хвост не сканируется
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.text_anonymizer import TextAnonymizer, _MAX_ANONYMIZE_LEN, _MAX_PII_LEN  # noqa: E402

# Visa test card — проходит Luhn
_VISA_CARD = "4532015112830366"          # 16 символов
_VISA_SPACED = "4532 0151 1283 0366"     # 19 символов (с пробелами)

# Российский телефон без разделителей: 12 символов
_RU_PHONE = "+79991234567"

# Email: 20 символов
_TEST_EMAIL = "user@example.com"


class TestAnonymizerBoundaryLeakW1766(unittest.TestCase):
    """Проверка: PII-токен, пересекающий границу обрезки, не утекает в вывод."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    # ── Номер карты пересекает границу _MAX_ANONYMIZE_LEN ────────────────────

    def test_card_straddling_boundary(self) -> None:
        """Luhn-valid Visa card начинается за 8 символов до _MAX_ANONYMIZE_LEN.

        До исправления: первые 8 цифр входили в scan_text, остальные 8 — в tail;
        regex не матчил неполный токен → card утекал нередактированным.
        После исправления: вся карта попадает в расширенное окно → редактируется.

        Примечание: regex credit_card использует \\b → перед первой цифрой нужен
        не-word символ. Последний символ padding — пробел (' ').
        """
        # Позиционируем начало карты за 8 символов до границы.
        # Отступаем ещё 1 символ для пробела-разделителя перед картой.
        offset = _MAX_ANONYMIZE_LEN - 9  # 8 цифр карты до границы + 1 символ пробела
        padding = "a" * offset + " "     # пробел обеспечивает \b перед цифрами
        text = padding + _VISA_CARD + " конец транскрипции"

        result = self.a.anonymize(text)

        self.assertNotIn(
            _VISA_CARD,
            result.anonymized_text,
            "Номер карты утёк нередактированным через границу обрезки (wave1766)",
        )
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "credit_card")

    # ── Телефон пересекает границу ───────────────────────────────────────────

    def test_phone_straddling_boundary(self) -> None:
        """Российский телефон начинается за 5 символов до _MAX_ANONYMIZE_LEN.

        +79991234567 — 12 символов; первые 5 — в исходном scan_text,
        остальные 7 — за исходной границей.
        """
        offset = _MAX_ANONYMIZE_LEN - 5
        padding = "b" * offset
        text = padding + _RU_PHONE + " — позвони"

        result = self.a.anonymize(text)

        self.assertNotIn(
            _RU_PHONE,
            result.anonymized_text,
            "Номер телефона утёк нередактированным через границу обрезки (wave1766)",
        )
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        self.assertGreaterEqual(result.redaction_count, 1)

    # ── Email пересекает границу ─────────────────────────────────────────────

    def test_email_straddling_boundary(self) -> None:
        """Email начинается за 4 символа до _MAX_ANONYMIZE_LEN.

        user@example.com — 16 символов; первые 4 — в исходном scan_text.
        """
        offset = _MAX_ANONYMIZE_LEN - 4
        padding = "c" * offset
        text = padding + _TEST_EMAIL + " обращайтесь"

        result = self.a.anonymize(text)

        self.assertNotIn(
            _TEST_EMAIL,
            result.anonymized_text,
            "Email утёк нередактированным через границу обрезки (wave1766)",
        )
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "email")

    # ── Нормальный короткий текст — поведение не изменилось ──────────────────

    def test_normal_short_text_unchanged(self) -> None:
        """Короткий текст: редактирование PII и сохранение обычного контента."""
        text = "Карта клиента: 4532 0151 1283 0366, телефон +79991234567"
        result = self.a.anonymize(text)

        self.assertNotIn(_VISA_SPACED, result.anonymized_text)
        self.assertNotIn(_RU_PHONE, result.anonymized_text)
        self.assertIn("[КАРТА]", result.anonymized_text)
        self.assertIn("[ТЕЛЕФОН]", result.anonymized_text)
        # Несекретный контент сохранён
        self.assertIn("Карта клиента:", result.anonymized_text)
        self.assertIn("телефон", result.anonymized_text)

    def test_empty_text_no_crash(self) -> None:
        """Пустая строка возвращает пустой результат без ошибок."""
        result = self.a.anonymize("")
        self.assertEqual(result.anonymized_text, "")
        self.assertEqual(result.redaction_count, 0)

    # ── PII целиком за пределами расширенного окна — это допустимо ───────────

    def test_pii_wholly_beyond_extended_window(self) -> None:
        """PII целиком за пределами _MAX_ANONYMIZE_LEN + _MAX_PII_LEN не сканируется.

        Это задокументированное поведение backstop: для экстремально длинных
        входов хвост за расширенным окном присоединяется verbatim.
        """
        # Карта начинается ровно за расширенным окном.
        # Пробел перед картой нужен для \b, но он сам уже в зоне «хвоста».
        beyond_boundary = _MAX_ANONYMIZE_LEN + _MAX_PII_LEN
        padding = "d" * (beyond_boundary - 1) + " "  # итого beyond_boundary симв.
        text = padding + _VISA_CARD

        result = self.a.anonymize(text)
        # Хвост за расширенным окном не сканируется — карта в выводе как есть
        # (ожидаемое поведение backstop для экстремально длинных входов)
        self.assertIn(_VISA_CARD, result.anonymized_text)

    # ── Карта прямо на исходной границе, но внутри расширенного окна ─────────

    def test_card_starting_exactly_at_original_boundary(self) -> None:
        """Карта начинается ровно на символе _MAX_ANONYMIZE_LEN.

        Без исправления: первый символ карты — в tail → не сканировался.
        С исправлением: вся карта в расширенном окне → редактируется.

        Примечание: пробел перед картой обеспечивает \\b для regex \\b\\d{16}\\b.
        padding = (_MAX_ANONYMIZE_LEN - 1) символов + пробел → итого _MAX_ANONYMIZE_LEN
        символов перед первой цифрой карты.
        """
        padding = "e" * (_MAX_ANONYMIZE_LEN - 1) + " "
        text = padding + _VISA_CARD + " платёж"

        result = self.a.anonymize(text)

        self.assertNotIn(
            _VISA_CARD,
            result.anonymized_text,
            "Карта, начинающаяся ровно на границе обрезки, утекла (wave1766)",
        )
        self.assertIn("[КАРТА]", result.anonymized_text)


if __name__ == "__main__":
    unittest.main()
