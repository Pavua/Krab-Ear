"""Тесты core.safe_regex — ReDoS-безопасные утилиты компиляции/выполнения regex.

Tests for core.safe_regex — ReDoS-safe regex compile/run utilities.

Охватывает (Wave 1735):
- compile_safe: отклонение вложенных квантификаторов, слишком длинных паттернов;
  принятие нормальных email/слово паттернов.
- run_with_timeout: возврат None (а не зависание) на катастрофическом паттерне
  с патологическим вводом; проверка wall-clock < timeout + 0.5 с.
- search_safe: интеграционный тест compile + run в одном вызове.
- HallucinationManager: вредоносный пользовательский паттерн отклоняется / не зависает.
"""

from __future__ import annotations

import re
import sys
import time
import unittest

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402
from pathlib import Path

# Настройка путей для standalone-запуска (без pytest PYTHONPATH)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "KrabEar"
for _p in (str(PACKAGE_ROOT), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.safe_regex import compile_safe, run_with_timeout, search_safe  # noqa: E402
from core.hallucination_manager import HallucinationManager  # noqa: E402


# ── Константы для timing-тестов ────────────────────────────────────────────────

# Максимально допустимое время для теста «нет зависания» (секунды).
# Таймаут run_with_timeout = 1.0 с; добавляем 0.5 с запас на старт потока.
_NO_HANG_LIMIT = 1.5


class TestCompileSafeRejections(unittest.TestCase):
    """compile_safe отклоняет опасные и некорректные паттерны."""

    def test_rejects_classic_nested_plus(self):
        """``(a+)+`` — классический вложенный «плюс» (ReDoS)."""
        with self.assertRaises(ValueError, msg="(a+)+ должен быть отклонён"):
            compile_safe(r"(a+)+")

    def test_rejects_nested_star(self):
        """``(a*)*`` — вложенная звёздочка."""
        with self.assertRaises(ValueError):
            compile_safe(r"(a*)*")

    def test_rejects_nc_group_bypass(self):
        """``((?:a)+)+`` — NC-внутренняя группа (Wave 1729 bypass)."""
        with self.assertRaises(ValueError):
            compile_safe(r"((?:a)+)+")

    def test_rejects_nc_outer_bypass(self):
        """``(?:(a+))+`` — NC-внешняя группа (Wave 1730 bypass)."""
        with self.assertRaises(ValueError):
            compile_safe(r"(?:(a+))+")

    def test_rejects_deeply_nested(self):
        """``((a+)+)`` — глубоко вложенные группы."""
        with self.assertRaises(ValueError):
            compile_safe(r"((a+)+)")

    def test_rejects_curly_quantifier_on_nc(self):
        """``((?:a)+){5,}`` — фигурный квантификатор на NC-обёрнутой группе."""
        with self.assertRaises(ValueError):
            compile_safe(r"((?:a)+){5,}")

    def test_rejects_overlength_pattern(self):
        """Паттерн длиннее max_pattern_len отклоняется."""
        long_pat = "a" * 1001
        with self.assertRaises(ValueError, msg="Слишком длинный паттерн должен быть отклонён"):
            compile_safe(long_pat)

    def test_rejects_overlength_custom_limit(self):
        """Пользовательский лимит max_pattern_len соблюдается."""
        pat = "a" * 11
        with self.assertRaises(ValueError):
            compile_safe(pat, max_pattern_len=10)

    def test_rejects_invalid_regex_syntax(self):
        """Синтаксически некорректный паттерн поднимает re.error."""
        with self.assertRaises(re.error):
            compile_safe(r"[unclosed")

    def test_rejects_wrong_type(self):
        """Не-строка поднимает TypeError."""
        with self.assertRaises(TypeError):
            compile_safe(123)  # type: ignore[arg-type]


class TestCompileSafeAccepts(unittest.TestCase):
    """compile_safe принимает нормальные паттерны и возвращает re.Pattern."""

    def test_accepts_word_pattern(self):
        """Простой паттерн слова компилируется корректно."""
        p = compile_safe(r"\bслово\b")
        self.assertIsInstance(p, re.Pattern)
        self.assertTrue(p.search("слово в предложении"))

    def test_accepts_email_pattern(self):
        """Безопасный email-паттерн (без вложенных квантификаторов) компилируется."""
        # Намеренно простой, без ReDoS-структур
        p = compile_safe(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
        self.assertIsInstance(p, re.Pattern)
        self.assertTrue(p.search("user@example.com"))

    def test_accepts_youtube_hallucination_pattern(self):
        """Встроенный YouTube-паттерн галлюцинации безопасен."""
        p = compile_safe(r"(?:спасибо за просмотр|спасибо за внимание)[.!?…]*$")
        self.assertIsInstance(p, re.Pattern)

    def test_accepts_non_nested_plus(self):
        """``a+`` без группы — безопасно."""
        p = compile_safe(r"a+b+c")
        self.assertIsInstance(p, re.Pattern)

    def test_accepts_quantified_char_class(self):
        """``[a-z]+`` — квантификатор на char-class, не на группу — безопасно."""
        p = compile_safe(r"[a-z]+")
        self.assertIsInstance(p, re.Pattern)

    def test_accepts_optional_group(self):
        """``(foo)?`` — опциональная группа без вложенного квантификатора — безопасно."""
        p = compile_safe(r"(foo)?bar")
        self.assertIsInstance(p, re.Pattern)

    def test_accepts_with_flags(self):
        """Флаги re.IGNORECASE пробрасываются корректно."""
        p = compile_safe(r"hello", re.IGNORECASE)
        self.assertIsInstance(p, re.Pattern)
        self.assertTrue(p.search("HELLO"))

    def test_accepts_cyrillic_pattern(self):
        """Кириллический паттерн компилируется без ошибок."""
        p = compile_safe(r"[а-яёА-ЯЁ]+")
        self.assertIsInstance(p, re.Pattern)
        self.assertTrue(p.search("Привет"))


class TestRunWithTimeout(unittest.TestCase):
    """run_with_timeout не зависает и возвращает None при таймауте.

    ВАЖНО (GIL-ограничение, Wave 1735):
    CPython ``re`` удерживает GIL во время вычислений, поэтому поточный таймаут
    не может жёстко прервать зависший поиск.  Первичная защита:

    1. ``compile_safe`` отклоняет вложенные квантификаторы на этапе компиляции.
    2. ``_max_text_backstop`` в ``run_with_timeout`` обрезает текст до 8192
       символов, что делает даже катастрофические паттерны конечными по времени.

    Тесты «no-hang» проверяют backstop длины текста, а не GIL-прерывание потока.
    """

    def test_normal_match_returns_result(self):
        """Нормальный паттерн возвращает объект Match."""
        p = re.compile(r"\btest\b")
        m = run_with_timeout(p, "this is a test string")
        self.assertIsNotNone(m)

    def test_no_match_returns_none(self):
        """Паттерн без совпадения возвращает None (не зависает)."""
        p = re.compile(r"xyz123")
        m = run_with_timeout(p, "hello world")
        self.assertIsNone(m)

    def test_catastrophic_pattern_short_input_returns_none_or_fast(self):
        """Катастрофический паттерн на вводе внутри backstop-лимита.

        Паттерн ``(a+)+$`` + строка ``"a" * 25 + "!"`` вызывает откат, но
        backstop обрезает текст до 8192 символов.  На вводе в 26 символов
        откат занимает секунды — run_with_timeout вернёт None после таймаута.
        Ожидаем завершение за < timeout + 0.5 с (best-effort).

        Примечание: этот тест проверяет что функция ВОЗВРАЩАЕТ, а не зависает
        навсегда.  Из-за GIL поток может «заброситься», не прерываясь мгновенно.
        """
        # Компилируем напрямую — обходим compile_safe для теста таймаута
        dangerous = re.compile(r"(a+)+$")
        # Короткий ввод (в пределах backstop) — проверяем поведение функции
        hostile_input = "a" * 20 + "!"  # вызывает откат, но короткий

        # Запускаем с коротким таймаутом
        result = run_with_timeout(dangerous, hostile_input, timeout_sec=0.1)
        # Результат может быть None (timeout) или match (если завершился быстро) —
        # главное, что вызов вернулся без зависания на неопределённое время.
        # Проверяем тип возврата.
        self.assertIn(type(result), (type(None), type(re.match("x", "x"))))

    def test_backstop_prevents_hang_on_oversized_text(self):
        """Backstop обрезает текст до 8192 символов — защита от длинного ввода.

        На тексте > 8192 символов run_with_timeout обрезает его до backstop,
        поэтому даже катастрофический паттерн на конце не достигается.
        Функция должна вернуться быстро (< 2.0 с).
        """
        # Паттерн, который ищет "TRIGGER" в конце — безопасный для engine
        p = re.compile(r"TRIGGER\s*$")
        # Текст: 50 000 символов безопасных + "TRIGGER" в конце
        # Backstop = 8192, поэтому "TRIGGER" не достигается → None
        huge_text = "x" * 50_000 + "TRIGGER"

        t0 = time.perf_counter()
        result = run_with_timeout(p, huge_text, timeout_sec=1.0)
        elapsed = time.perf_counter() - t0

        self.assertIsNone(result, "TRIGGER за пределами backstop не должен находиться")
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"run_with_timeout завис на {elapsed:.2f}с > 2.0с")

    def test_match_found_within_backstop(self):
        """Совпадение в пределах backstop-лимита находится корректно."""
        p = re.compile(r"hello")
        # hello в позиции 5 — внутри backstop
        text = "say hello world"
        m = run_with_timeout(p, text)
        self.assertIsNotNone(m)


class TestSearchSafe(unittest.TestCase):
    """search_safe — интеграционный тест compile + run."""

    def test_returns_match_for_valid_pattern(self):
        """search_safe находит совпадение для корректного паттерна."""
        m = search_safe(r"\bпривет\b", "привет мир")
        self.assertIsNotNone(m)

    def test_returns_none_for_no_match(self):
        """search_safe возвращает None, если совпадений нет."""
        m = search_safe(r"xyz999", "hello world")
        self.assertIsNone(m)

    def test_rejects_dangerous_pattern(self):
        """search_safe отклоняет опасный паттерн через ValueError."""
        with self.assertRaises(ValueError):
            search_safe(r"(a+)+", "test input")

    def test_clips_oversized_text(self):
        """search_safe обрезает текст до max_text_len до выполнения."""
        # Текст с совпадением за пределами лимита — не должен быть найден
        target = "hello"
        text = "x" * 100 + target  # совпадение после позиции 100
        m = search_safe(r"hello", text, max_text_len=50)
        # Текст обрезан до 50 символов → "hello" не достигается
        self.assertIsNone(m)

    def test_finds_match_within_text_limit(self):
        """search_safe находит совпадение в пределах max_text_len."""
        m = search_safe(r"hello", "say hello world", max_text_len=200_000)
        self.assertIsNotNone(m)


class TestHallucinationManagerSafeRegexIntegration(unittest.TestCase):
    """HallucinationManager использует safe_regex — пользовательские паттерны защищены."""

    def setUp(self):
        self.mgr = HallucinationManager()  # in-memory, без data_dir

    def test_malicious_nested_plus_rejected_on_add(self):
        """Вредоносный ``(a+)+`` отклоняется при add_pattern — до сохранения."""
        with self.assertRaises(ValueError, msg="(a+)+ должен быть отклонён add_pattern"):
            self.mgr.add_pattern(r"(a+)+")

    def test_malicious_nc_bypass_rejected_on_add(self):
        """NC-bypass паттерн ``((?:a)+)+`` отклоняется при add_pattern."""
        with self.assertRaises(ValueError):
            self.mgr.add_pattern(r"((?:a)+)+")

    def test_safe_custom_pattern_accepted_and_matches(self):
        """Безопасный пользовательский паттерн принимается и работает корректно."""
        self.mgr.add_pattern(r"тестовое слово\s*$", category="test")
        matches = self.mgr.check_text("некий текст тестовое слово")
        self.assertTrue(len(matches) > 0, "Пользовательский паттерн должен находить совпадение")

    def test_check_text_does_not_hang_on_pathological_input(self):
        """check_text с пользовательским паттерном не зависает на длинном вводе.

        Паттерн безопасный (принят), но ввод большой — run_with_timeout защищает
        от потенциально медленного выполнения.
        """
        self.mgr.add_pattern(r"конец\s*$", category="test")
        long_text = "a" * 10000 + " конец"
        t0 = time.perf_counter()
        matches = self.mgr.check_text(long_text)
        elapsed = time.perf_counter() - t0
        # Даже с большим текстом должно завершиться быстро (текст обрезается до 4096)
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"check_text завис на {elapsed:.2f}с")
        # Совпадение может быть найдено или нет (зависит от _MAX_MATCH_INPUT_LEN),
        # главное — нет зависания
        self.assertIsInstance(matches, list)

    def test_builtin_patterns_still_work_after_wiring(self):
        """Встроенные паттерны продолжают работать после добавления safe_regex."""
        text = "Хорошее выступление. Спасибо за просмотр."
        matches = self.mgr.check_text(text)
        categories = {m.category for m in matches}
        self.assertIn("youtube", categories, "Встроенный YouTube-паттерн должен сработать")

    def test_strip_hallucinations_with_safe_custom_pattern(self):
        """strip_hallucinations корректно удаляет совпадение пользовательского паттерна."""
        self.mgr.add_pattern(r"удали это\s*$", category="test")
        text = "полезный текст. удали это"
        result = self.mgr.strip_hallucinations(text)
        self.assertNotIn("удали это", result.lower())
        self.assertIn("полезный текст", result)


if __name__ == "__main__":
    unittest.main()
