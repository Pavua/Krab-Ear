"""Regression: normalize_entities — IndexError на QN-токене в нижнем регистре.

Wave 1750: `_BRAND_REPLACEMENTS_RAW` содержал запись

    (r"\bQN\\s*\\d+B?\\b", lambda m: "Qwen " + m.group(0).split("QN")[1].lstrip())

Паттерн компилируется с `re.IGNORECASE`, поэтому совпадает с "qn14b", "Qn5" и т.д.
Однако лямбда вызывала `.split("QN")` (без IGNORECASE), из-за чего в случае
строчного совпадения "qn14b" возвращался список с одним элементом: ["qn14b"].
Доступ к индексу [1] -> IndexError.

Исправление: паттерн заменён на r"\bQN\\s*(\\d+B?)\\b" с заменой r"Qwen \\1" —
нет лямбды, нет строковой манипуляции, нет возможности выйти за пределы.

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest \
        KrabEar/tests/test_utils_normalize_entities_qn_crash_W1750.py -v
"""
from __future__ import annotations

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.utils import TextUtils  # noqa: E402


# ---------------------------------------------------------------------------
# Regression: the exact inputs that triggered IndexError before the fix
# ---------------------------------------------------------------------------

class TestQNNormalizeEntitiesNoIndexError:
    """Проверяем, что normalize_entities не поднимает IndexError на QN-токенах."""

    # Именно эти входы вызывали IndexError (lowercase qn + IGNORECASE pattern):
    CRASHING_INPUTS = [
        "купил qn14b модель",
        "тест qn7 производительность",
        "запустил qn32b на маке",
        "устанавливаю qn14B весит много",
        "Qn5 быстрый",
    ]

    @pytest.mark.parametrize("text", CRASHING_INPUTS)
    def test_no_index_error_on_lowercase_qn(self, text):
        """normalize_entities не должна поднимать IndexError при lowercase qn-токене."""
        # fail-before: старый код поднимал IndexError; pass-after: возвращает строку
        result = TextUtils.normalize_entities(text)
        assert isinstance(result, str), "Expected str, got {}".format(type(result))

    @pytest.mark.parametrize("text", CRASHING_INPUTS)
    def test_qn_lowercase_replaced_to_qwen(self, text):
        """Lowercase qn-токен заменяется на 'Qwen <num>' без ошибки."""
        result = TextUtils.normalize_entities(text)
        assert "Qwen" in result, (
            "Expected 'Qwen' in result for {!r}, got: {!r}".format(text, result)
        )


# ---------------------------------------------------------------------------
# Correctness: replacement produces the expected canonical form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_sub", [
    # uppercase (was working before fix too, now via simpler path)
    ("Запустил QN14B модель", "Qwen 14B"),
    ("тест QN7 быстрый", "Qwen 7"),
    ("QN1 самый маленький", "Qwen 1"),
    ("QN32B огромный", "Qwen 32B"),
    # lowercase — was crashing before fix
    ("купил qn14b модель", "Qwen 14b"),
    ("тест qn7 производительность", "Qwen 7"),
    ("qn32b на маке работает", "Qwen 32b"),
    # mixed case
    ("устанавливаю qn14B весит много", "Qwen 14B"),
    ("Qn5 быстрый", "Qwen 5"),
    # with space between QN and digits (pattern: \bQN\s*(\d+B?)\b)
    ("тест QN 14B сейчас", "Qwen 14B"),
])
def test_qn_canonical_form(text, expected_sub):
    """Проверяем, что QN-токен заменяется в каноническую форму 'Qwen <num>'."""
    result = TextUtils.normalize_entities(text)
    assert expected_sub in result, (
        "Expected {!r} in result, got: {!r}".format(expected_sub, result)
    )


# ---------------------------------------------------------------------------
# Negative: QN without trailing digits must NOT be replaced
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "таблица QN значений",        # QN без цифры — не трогать
    "переменная qn используется",  # то же в строчном
])
def test_qn_without_digits_not_replaced(text):
    """QN без следующей цифры не должен заменяться на Qwen."""
    result = TextUtils.normalize_entities(text)
    assert "Qwen" not in result, (
        "Expected 'Qwen' NOT in result for {!r}, got: {!r}".format(text, result)
    )
