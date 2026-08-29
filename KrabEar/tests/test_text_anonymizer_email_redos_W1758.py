"""Регрессионные тесты ReDoS в email-правиле TextAnonymizer (W1758 HIGH).

Подтверждённая уязвимость:
    Старый паттерн `[a-zA-Z0-9._%+\\-]+@[a-zA-Z0-9.\\-]+\\.[a-zA-Z]{2,}`
    страдает катастрофическим backtracking на двух классах входных данных:
    - `x@` + `a.` * N  (точка-буква повтор)
    - `q@` + `-` * N   (дефисный хвост без TLD)

    Оба пути достижимы из IPC:
    (1) post_process_text(text=<hostile>, steps=['anonymize'])
    (2) generate_auto_title / batch_generate — anonymizer вызывается до truncation

Тесты:
    test_hostile_dot_a_pattern_fast   — fail-before / pass-after timing guard
    test_hostile_hyphen_pattern_fast  — fail-before / pass-after timing guard
    test_legit_email_still_redacted   — john.doe@example.com → [EMAIL]
    test_subdomain_email_redacted     — sub.domain.co.uk, плюс-знак, подчёркивание
    test_backstop_oversized_text      — текст > 500 KB не вызывает зависания,
                                        legitные email-адреса в пределах backstop редактируются
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402

from core.text_anonymizer import TextAnonymizer  # noqa: E402

# Порог для timing-тестов (секунды).
# Линейный паттерн завершается за <30 мс; 250 мс — щедрый запас с 8× margin.
_TIMING_LIMIT_S = 0.25


class TestEmailReDoSW1758(unittest.TestCase):
    """Regression: email ReDoS — катастрофическое backtracking устранено (W1758)."""

    def setUp(self) -> None:
        self.a = TextAnonymizer()

    # ── Timing guards (fail-before / pass-after) ─────────────────────────────

    def test_hostile_dot_a_pattern_fast(self) -> None:
        """`x@` + `a.`*20000 должен завершиться быстрее 250 мс.

        Старый паттерн занимал >500 мс на n=5000 (O(n^2)); новый — O(n), <30 мс.
        """
        hostile = "normal text x@" + "a." * 20000
        t0 = time.perf_counter()
        result = self.a.anonymize(hostile)
        elapsed = time.perf_counter() - t0
        self.assertLess(
            elapsed,
            _TIMING_LIMIT_S,
            f"email ReDoS (dot-a): завершился за {elapsed*1000:.0f} мс > {_TIMING_LIMIT_S*1000:.0f} мс",
        )
        # Убеждаемся, что вызов вернул валидный объект
        self.assertIsNotNone(result.anonymized_text)

    def test_hostile_hyphen_pattern_fast(self) -> None:
        """`q@` + `-`*30000 должен завершиться быстрее 250 мс.

        Дефисный вектор: домен без TLD → старый движок бэктрекал по hyphen-символам.
        """
        hostile = "q@" + "-" * 30000
        t0 = time.perf_counter()
        result = self.a.anonymize(hostile)
        elapsed = time.perf_counter() - t0
        self.assertLess(
            elapsed,
            _TIMING_LIMIT_S,
            f"email ReDoS (hyphen): завершился за {elapsed*1000:.0f} мс > {_TIMING_LIMIT_S*1000:.0f} мс",
        )
        self.assertIsNotNone(result.anonymized_text)

    # ── Корректность редактирования легитных адресов ─────────────────────────

    def test_legit_email_still_redacted(self) -> None:
        """john.doe@example.com должен быть заменён на [EMAIL]."""
        text = "contact john.doe@example.com please"
        result = self.a.anonymize(text)
        self.assertNotIn(
            "john.doe@example.com",
            result.anonymized_text,
            "Легитный email не был редактирован после патча W1758",
        )
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "email")

    def test_subdomain_email_redacted(self) -> None:
        """a_b+c%d@sub.domain.co.uk (субдомен, +, %, _) должен быть редактирован."""
        text = "write to a_b+c%d@sub.domain.co.uk for support"
        result = self.a.anonymize(text)
        self.assertNotIn(
            "a_b+c%d@sub.domain.co.uk",
            result.anonymized_text,
            "Субдоменный email не был редактирован",
        )
        self.assertIn("[EMAIL]", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)

    def test_multiple_emails_redacted(self) -> None:
        """Несколько email-адресов в тексте — все должны быть заменены."""
        text = "From: alice@corp.io, CC: bob@mail.example.org — срочно!"
        result = self.a.anonymize(text)
        self.assertNotIn("alice@corp.io", result.anonymized_text)
        self.assertNotIn("bob@mail.example.org", result.anonymized_text)
        self.assertEqual(result.anonymized_text.count("[EMAIL]"), 2)

    # ── Backstop: тексты сверх лимита обрабатываются без зависания ───────────

    def test_backstop_oversized_text(self) -> None:
        """Текст длиннее 500 KB не вызывает зависания.

        email в пределах первых 500 KB редактируется; хвост присоединяется verbatim.
        """
        prefix = "contact admin@example.com now. "
        filler = "x" * 600_000  # превышает _MAX_ANONYMIZE_LEN (500_000)
        text = prefix + filler

        t0 = time.perf_counter()
        result = self.a.anonymize(text)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, REDOS_BUDGET_SEC, "backstop: anonymize завис на тексте >500 KB")
        # Email в начале — должен быть редактирован
        self.assertNotIn("admin@example.com", result.anonymized_text)
        self.assertIn("[EMAIL]", result.anonymized_text)
        # Хвост должен присутствовать в выводе
        self.assertIn(filler[-100:], result.anonymized_text)


if __name__ == "__main__":
    unittest.main()
