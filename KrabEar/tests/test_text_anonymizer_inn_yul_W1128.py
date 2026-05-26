"""Тесты W1128: ИНН ЮЛ (10-digit org TIN) redaction in TextAnonymizer.

Cases:
1. Валидный ИНН ЮЛ заменяется на [ИНН_ЮЛ].
2. 10-значная последовательность с неверной контрольной суммой НЕ заменяется.
3. ИНН ФЛ (12 цифр) по-прежнему заменяется на [ИНН] (регрессия не сломана).
"""

import ast
import os
import sys
import unittest

# Resolve project root so imports work when run standalone or via unittest
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.text_anonymizer import TextAnonymizer, _passes_inn_yul_checksum, _passes_inn_fl_checksum


class InnYulChecksumTests(unittest.TestCase):
    """Unit-тесты алгоритма контрольной суммы ИНН ЮЛ."""

    def test_known_valid_inn_yul(self):
        # ИНН Сбербанка России — публично известный
        self.assertTrue(_passes_inn_yul_checksum("7707083893"))

    def test_known_valid_inn_yul_gazprom(self):
        # ИНН Газпрома — публично известный
        self.assertTrue(_passes_inn_yul_checksum("7736050003"))

    def test_invalid_checksum(self):
        # Последний знак изменён — контрольная сумма не совпадает
        self.assertFalse(_passes_inn_yul_checksum("7707083894"))

    def test_wrong_length_rejected(self):
        self.assertFalse(_passes_inn_yul_checksum("770708389"))   # 9 знаков
        self.assertFalse(_passes_inn_yul_checksum("77070838930"))  # 11 знаков

    def test_non_digits_rejected(self):
        self.assertFalse(_passes_inn_yul_checksum("770708389X"))


class InnFlChecksumTests(unittest.TestCase):
    """Регрессия: проверка контрольной суммы ИНН ФЛ (12 цифр)."""

    def test_known_valid_inn_fl(self):
        # Публично известный тестовый ИНН ФЛ из документации ФНС
        self.assertTrue(_passes_inn_fl_checksum("500100732259"))

    def test_invalid_checksum(self):
        self.assertFalse(_passes_inn_fl_checksum("500100732250"))

    def test_wrong_length_rejected(self):
        self.assertFalse(_passes_inn_fl_checksum("50010073225"))   # 11 знаков


class TextAnonymizerInnYulRedactionTests(unittest.TestCase):
    """Интеграционные тесты: анонимизация ИНН ЮЛ в тексте."""

    def setUp(self):
        self.anonymizer = TextAnonymizer()

    # ── Case 1: валидный ИНН ЮЛ заменяется ──────────────────────────────────

    def test_valid_inn_yul_replaced(self):
        text = "Организация ИНН 7707083893 заключила договор."
        result = self.anonymizer.anonymize(text)
        self.assertIn("[ИНН_ЮЛ]", result.anonymized_text)
        self.assertNotIn("7707083893", result.anonymized_text)
        self.assertEqual(result.redaction_count, 1)
        self.assertEqual(result.redactions[0].category, "inn_yul")

    def test_valid_inn_yul_gazprom_replaced(self):
        text = "Контрагент 7736050003 прислал счёт."
        result = self.anonymizer.anonymize(text)
        self.assertIn("[ИНН_ЮЛ]", result.anonymized_text)
        self.assertNotIn("7736050003", result.anonymized_text)

    # ── Case 2: неверная контрольная сумма — не заменяется ──────────────────

    def test_invalid_checksum_inn_yul_not_categorised_as_inn_yul(self):
        # 7707083894 — неверная контрольная сумма ИНН ЮЛ.
        # Это 10-значная последовательность без пробела, поэтому может
        # подпасть под правило "passport" (паспорт без разделителя).
        # Ключевое требование: категория НЕ должна быть inn_yul.
        text = "Случайное число 7707083894 в тексте."
        result = self.anonymizer.anonymize(text)
        self.assertNotIn("[ИНН_ЮЛ]", result.anonymized_text)
        categories = [r.category for r in result.redactions]
        self.assertNotIn("inn_yul", categories)

    def test_all_zeros_checksum_is_valid_edge_case(self):
        # 0000000000: sum(0 * c for all c) % 11 % 10 == 0 == d[9]
        # Математически проходит контрольную сумму — заменяется как ИНН_ЮЛ.
        text = "Данные: 0000000000."
        result = self.anonymizer.anonymize(text)
        # Либо заменяется как inn_yul (valid checksum), либо как passport
        # В любом случае НЕ должно оставаться необработанным числом
        self.assertTrue(
            "[ИНН_ЮЛ]" in result.anonymized_text
            or "[ПАСПОРТ]" in result.anonymized_text,
            f"Expected redaction but got: {result.anonymized_text}",
        )

    # ── Case 3: ИНН ФЛ (12 цифр) по-прежнему заменяется ────────────────────

    def test_inn_fl_still_replaced(self):
        text = "Клиент ИНН 500100732259 подписал акт."
        result = self.anonymizer.anonymize(text)
        self.assertIn("[ИНН]", result.anonymized_text)
        self.assertNotIn("500100732259", result.anonymized_text)
        # Убедимся, что категория именно inn, а не inn_yul
        categories = [r.category for r in result.redactions]
        self.assertIn("inn", categories)
        self.assertNotIn("inn_yul", categories)

    def test_inn_fl_invalid_checksum_preserved(self):
        # Неверная контрольная сумма → не заменяется
        text = "Число 500100732250 в документе."
        result = self.anonymizer.anonymize(text)
        self.assertNotIn("[ИНН]", result.anonymized_text)
        self.assertIn("500100732250", result.anonymized_text)

    # ── Дополнительные проверки ──────────────────────────────────────────────

    def test_inn_yul_in_rules_list(self):
        self.assertIn("inn_yul", self.anonymizer.list_rules())

    def test_selective_rules_inn_yul_only(self):
        text = "ИНН ЮЛ 7707083893 и ФЛ 500100732259."
        result = self.anonymizer.anonymize(text, rules=["inn_yul"])
        self.assertIn("[ИНН_ЮЛ]", result.anonymized_text)
        # ИНН ФЛ НЕ должен быть заменён (правило inn не выбрано)
        self.assertIn("500100732259", result.anonymized_text)


class AstSanityTest(unittest.TestCase):
    """AST-проверка: модуль парсируется без ошибок."""

    def test_module_parses(self):
        module_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "core", "text_anonymizer.py",
        )
        with open(module_path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        self.assertIsNotNone(tree)

    def test_inn_yul_checksum_function_exists(self):
        module_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "core", "text_anonymizer.py",
        )
        with open(module_path, encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)
        func_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        self.assertIn("_passes_inn_yul_checksum", func_names)
        self.assertIn("_passes_inn_fl_checksum", func_names)


if __name__ == "__main__":
    unittest.main()
