"""test_smart_vocab_redos_wave1769.py — регресс-тесты ReDoS-фикса W1769.

Покрывает квадратичный ReDoS в `_RE_TECH_WITH_DIGITS`:

- Вторая ветка паттерна раньше содержала перекрывающийся жадный класс
  `[A-Za-zА-Яа-я0-9\\-]*` непосредственно перед `[0-9]+` → O(n²) backtracking
  на чисто-цифровом токене (L=64000 → 14.6 c; 250k цифр ≈ 230 c CPU).
- Достижимо через IPC: `get_smart_vocabulary_suggestions` →
  `SmartVocabularyBuilder.get_vocabulary_suggestions()` → `finditer` по
  неограниченному source_text/text.

Фикс (двойная защита):
  (1) `raw_text[:_MAX_REGEX_TEXT_LEN]` перед regex-проходом в per-item цикле;
  (2) переписана вторая ветка паттерна без перекрытия по `[0-9]`.

Дублированный паттерн в `core/term_extractor.py` исправлен так же.

Тесты fail-before / pass-after:
  - 250k-символьный чисто-цифровой текст → get_vocabulary_suggestions < 0.3 c.
  - извлечение реальных tech-токенов (qwen3 / gpt4o / 3B / GPT4 / ...) идентично.
"""

from __future__ import annotations

import re
import sys
import time
import unittest
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tests.timing_budgets import REDOS_BUDGET_SEC  # noqa: E402

from backend.smart_vocabulary import (  # noqa: E402
    _MAX_REGEX_TEXT_LEN,
    _RE_TECH_WITH_DIGITS,
    SmartVocabularyBuilder,
)
from core.term_extractor import (  # noqa: E402
    _RE_TECH_WITH_DIGITS as TE_RE_TECH_WITH_DIGITS,
    TermExtractor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item(text: str, source_text: str = "", confidence: float = 1.0) -> dict:
    return {"text": text, "source_text": source_text, "confidence": confidence}


# Реальные технические токены, которые ДОЛЖНЫ извлекаться (паритет до/после).
_REAL_TECH_TOKENS = [
    "qwen3", "gpt4o", "3B", "GPT4", "Python3", "iPhone13", "H2O",
    "x86", "win10", "mp3", "4K", "2FA", "S3", "ipv6", "utf8",
    "проект2", "версия3х",
]

# Токены, которые НЕ должны матчиться (чистые числа, годы, дробные, слова).
_NON_TECH_TOKENS = ["123", "456", "2024", "3.14", "abc", "Москва", "GPT-4", "ChatGPT"]


# ---------------------------------------------------------------------------
# 1. Производительность — главный fail-before / pass-after тест
# ---------------------------------------------------------------------------

class TestRedosPerformance(unittest.TestCase):
    """Гигантская серия цифр не должна вызывать квадратичный взрыв."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder(min_word_length=3)

    def test_250k_pure_digits_fast(self) -> None:
        """250k-символьный чисто-цифровой текст → < 0.3 c (раньше ≈ 230 c)."""
        huge_digits = "0" * 250_000
        items = [_item(text=huge_digits, confidence=1.0)]

        t0 = time.perf_counter()
        result = self.builder.get_vocabulary_suggestions(items, min_frequency=1, top_k=30)
        elapsed = time.perf_counter() - t0

        self.assertLess(
            elapsed,
            REDOS_BUDGET_SEC,
            f"get_vocabulary_suggestions заняло {elapsed:.3f}c на 250k цифр "
            f"(лимит {REDOS_BUDGET_SEC}c) — квадратичный ReDoS не устранён",
        )
        # Чистые цифры не дают валидных слов-кандидатов.
        self.assertEqual(result, [])

    def test_digit_run_in_source_text_fast(self) -> None:
        """source_text (приоритетное поле) с длинной серией цифр — тоже быстро."""
        items = [_item(text="заглушка", source_text="9" * 250_000)]
        t0 = time.perf_counter()
        self.builder.get_vocabulary_suggestions(items, min_frequency=1)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"source_text путь занял {elapsed:.3f}c")

    def test_regex_finditer_linear(self) -> None:
        """Сам паттерн линеен: 8k цифр обрабатываются за миллисекунды."""
        s = "0" * 8000
        t0 = time.perf_counter()
        list(_RE_TECH_WITH_DIGITS.finditer(s))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"finditer на 8k цифр занял {elapsed:.4f}c")


# ---------------------------------------------------------------------------
# 2. Паритет паттерна на реальных токенах
# ---------------------------------------------------------------------------

class TestTechTokenParity(unittest.TestCase):
    """Переписанный паттерн матчит ровно те же реальные tech-токены."""

    def test_real_tech_tokens_still_matched(self) -> None:
        for tok in _REAL_TECH_TOKENS:
            with self.subTest(token=tok):
                matches = _RE_TECH_WITH_DIGITS.findall(tok)
                self.assertEqual(
                    matches, [tok],
                    f"Реальный tech-токен {tok!r} больше не извлекается",
                )

    def test_non_tech_tokens_not_matched(self) -> None:
        for tok in _NON_TECH_TOKENS:
            with self.subTest(token=tok):
                # GPT-4 / ChatGPT покрываются другими паттернами, но НЕ этим.
                matches = _RE_TECH_WITH_DIGITS.findall(tok)
                self.assertEqual(
                    matches, [],
                    f"Не-tech токен {tok!r} ошибочно матчится паттерном tech-with-digits",
                )

    def test_sentence_extraction_parity(self) -> None:
        """finditer по предложениям даёт ожидаемый набор токенов."""
        cases = [
            ("Модель GPT4 даёт хорошие результаты", ["GPT4"]),
            ("GPT4 model and Python3 language", ["GPT4", "Python3"]),
            ("Купил iPhone13 и H2O воду, версия 3B готова", ["iPhone13", "H2O", "3B"]),
            ("123 456 789 тест тест тест", []),
            ("2024 2024 2024 дата дата дата", []),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                got = [m.group(1) for m in _RE_TECH_WITH_DIGITS.finditer(text)]
                self.assertEqual(got, expected)

    def test_smart_vocab_extracts_gpt4(self) -> None:
        """Сквозной путь: GPT4 попадает в предложения словаря."""
        builder = SmartVocabularyBuilder(min_word_length=3)
        items = [_item(text=f"Модель GPT4 даёт результат {i}") for i in range(4)]
        sugg = builder.get_vocabulary_suggestions(items, min_frequency=2, top_k=30)
        words = {s["word"] for s in sugg}
        self.assertIn("gpt4", words)


# ---------------------------------------------------------------------------
# 3. Отсечка длины (defense-in-depth)
# ---------------------------------------------------------------------------

class TestLengthCap(unittest.TestCase):
    """raw_text усекается до _MAX_REGEX_TEXT_LEN перед regex."""

    def test_constant_reasonable(self) -> None:
        self.assertGreaterEqual(_MAX_REGEX_TEXT_LEN, 2000)
        self.assertLessEqual(_MAX_REGEX_TEXT_LEN, 50_000)

    def test_token_after_cap_ignored(self) -> None:
        """Валидный tech-токен за пределами отсечки не извлекается."""
        builder = SmartVocabularyBuilder(min_word_length=3)
        # Заполнитель из пробелов до отсечки, затем токен — он обрезается.
        padding = "ы " * (_MAX_REGEX_TEXT_LEN)  # заведомо длиннее лимита
        text = padding + " GPT4HIDDEN9 GPT4HIDDEN9 GPT4HIDDEN9"
        items = [_item(text=text) for _ in range(4)]
        sugg = builder.get_vocabulary_suggestions(items, min_frequency=2, top_k=50)
        words = {s["word"] for s in sugg}
        self.assertNotIn("gpt4hidden9", words)

    def test_token_before_cap_kept(self) -> None:
        """Токен в начале (внутри отсечки) извлекается нормально."""
        builder = SmartVocabularyBuilder(min_word_length=3)
        text = "GPT4 " + ("ы " * 100)
        items = [_item(text=text) for _ in range(4)]
        sugg = builder.get_vocabulary_suggestions(items, min_frequency=2, top_k=50)
        words = {s["word"] for s in sugg}
        self.assertIn("gpt4", words)


# ---------------------------------------------------------------------------
# 4. term_extractor.py — дублированный паттерн исправлен так же
# ---------------------------------------------------------------------------

class TestTermExtractorRedos(unittest.TestCase):
    """Идентичный паттерн в core/term_extractor.py также безопасен и совпадает."""

    def test_pattern_identical_to_smart_vocab(self) -> None:
        self.assertEqual(
            TE_RE_TECH_WITH_DIGITS.pattern.replace("\n", "").replace(" ", ""),
            _RE_TECH_WITH_DIGITS.pattern.replace("\n", "").replace(" ", ""),
            "Паттерны tech-with-digits в term_extractor и smart_vocabulary разошлись",
        )

    def test_finditer_linear(self) -> None:
        s = "0" * 8000
        t0 = time.perf_counter()
        list(TE_RE_TECH_WITH_DIGITS.finditer(s))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"term_extractor finditer 8k занял {elapsed:.4f}c")

    def test_extract_terms_tech_digit_still_works(self) -> None:
        extractor = TermExtractor(min_term_length=3)
        terms = extractor.extract_terms(
            "GPT4 model and Python3 language are widely used.", language="en"
        )
        names = {t.term.lower() for t in terms}
        self.assertTrue(
            any(any(c.isdigit() for c in n) for n in names),
            "Tech-термины с цифрами должны извлекаться",
        )

    def test_extract_terms_huge_digits_fast(self) -> None:
        """extract_terms на огромной серии цифр не зависает."""
        extractor = TermExtractor(min_term_length=3)
        t0 = time.perf_counter()
        extractor.extract_terms("0" * 60_000, language="ru")
        elapsed = time.perf_counter() - t0
        # extract_terms делает несколько проходов; даём 1.0 c запаса (раньше десятки секунд).
        self.assertLess(elapsed, REDOS_BUDGET_SEC, f"extract_terms на 60k цифр занял {elapsed:.3f}c")


# ---------------------------------------------------------------------------
# 5. Throttle: handler классифицируется как medium
# ---------------------------------------------------------------------------

class TestThrottleMembership(unittest.TestCase):
    """get_smart_vocabulary_suggestions включён в MEDIUM_METHODS (30/min)."""

    def test_in_medium_methods(self) -> None:
        from backend.ipc_throttle import MEDIUM_METHODS, _classify_method
        self.assertIn("get_smart_vocabulary_suggestions", MEDIUM_METHODS)
        self.assertEqual(_classify_method("get_smart_vocabulary_suggestions"), "medium")

    def test_throttle_eventually_blocks(self) -> None:
        """31-й вызов в минуту отклоняется (лимит medium = 30)."""
        from backend.ipc_throttle import IPCThrottle
        throttle = IPCThrottle()
        allowed = sum(
            1 for _ in range(40)
            if throttle.check_rate("get_smart_vocabulary_suggestions")
        )
        self.assertLessEqual(allowed, 30)
        self.assertGreater(allowed, 0)


# ---------------------------------------------------------------------------
# Self-check: убеждаемся, что СТАРЫЙ уязвимый паттерн действительно квадратичен
# (документирует, почему фикс нужен; не зависит от продакшн-кода).
# ---------------------------------------------------------------------------

class TestVulnerablePatternIsQuadratic(unittest.TestCase):
    """Документирующий тест: старый паттерн O(n²), новый — линеен."""

    _OLD = re.compile(
        r"\b([A-Za-zА-Яа-я]+[0-9]+[A-Za-zА-Яа-я0-9\-]*"
        r"|[A-Za-zА-Яа-я0-9\-]*[0-9]+[A-Za-zА-Яа-я]+)\b"
    )

    def test_old_slower_than_new(self) -> None:
        s = "0" * 6000
        t0 = time.perf_counter()
        list(self._OLD.finditer(s))
        old_dt = time.perf_counter() - t0
        t0 = time.perf_counter()
        list(_RE_TECH_WITH_DIGITS.finditer(s))
        new_dt = time.perf_counter() - t0
        # Новый паттерн должен быть на порядки быстрее старого на этом входе.
        self.assertLess(new_dt * 10, old_dt, "Новый паттерн не быстрее старого — фикс не применён")


class TestPrivateExtractorsTruncate(unittest.TestCase):
    """W1769 defense-in-depth parity: the private extractors (_extract_misrecognized_words
    / _extract_domain_terms) run _RE_WORD.findall on history-item text just like the
    public get_vocabulary_suggestions, but originally skipped the _MAX_REGEX_TEXT_LEN
    truncation the public path applies. They must cap raw input to the same bound so a
    giant injected transcript can't drive an unbounded regex scan if auto_update/
    build_vocabulary get wired."""

    def setUp(self) -> None:
        self.builder = SmartVocabularyBuilder(min_word_length=3)
        # фиктивное слово, размещённое ЗА границей _MAX_REGEX_TEXT_LEN
        filler = "abc " * ((_MAX_REGEX_TEXT_LEN // 4) + 200)  # > cap символов
        self.marker = "markerwordxyz"
        self.long_text = filler + self.marker
        self.assertGreater(len(self.long_text), _MAX_REGEX_TEXT_LEN)

    def test_misrecognized_truncates_past_cap(self):
        # confidence < _LOW_CONFIDENCE_THRESHOLD (0.65) → запись обрабатывается
        item = {"text": self.long_text, "confidence": 0.4}
        words = self.builder._extract_misrecognized_words([item], min_frequency=1)
        self.assertNotIn(
            self.marker, [w.lower() for w in words],
            "слово за пределами _MAX_REGEX_TEXT_LEN не должно извлекаться (нет усечения)",
        )

    def test_domain_terms_truncates_past_cap(self):
        item = {"text": self.long_text, "confidence": 0.9}
        words = self.builder._extract_domain_terms([item], min_frequency=1)
        self.assertNotIn(
            self.marker, [w.lower() for w in words],
            "слово за пределами _MAX_REGEX_TEXT_LEN не должно извлекаться (нет усечения)",
        )

    def test_word_before_cap_still_extracted(self):
        # регрессия: слово ДО границы должно по-прежнему извлекаться
        item = {"text": "uniqueterm uniqueterm uniqueterm", "confidence": 0.4}
        words = self.builder._extract_misrecognized_words([item], min_frequency=1)
        self.assertIn("uniqueterm", [w.lower() for w in words])


if __name__ == "__main__":
    unittest.main()
