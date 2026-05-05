"""Тесты расширения _BRAND_REPLACEMENTS_RAW (batch-8, 2026-05-05).

Покрывает новые записи: Llama+version, OpenAI доп. варианты, GPT доп. варианты,
Mistral, DeepSeek, Qwen доп. варианты, LM Studio доп. варианты, Hugging Face,
а также контекстно-зависимые Claude-модели (Opus/Sonnet/Haiku + версия).

Запуск:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_brand_replacements_v2.py -v
"""
from __future__ import annotations

import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.utils import TextUtils  # noqa: E402


def ne(text: str) -> str:
    """Shortcut: normalize_entities."""
    return TextUtils.normalize_entities(text)


# ---------------------------------------------------------------------------
# Positive cases — must be replaced
# ---------------------------------------------------------------------------
POSITIVE_CASES: list[tuple[str, str, str]] = [
    # (id, input, expected_substring)
    # Llama + version number
    ("llama4_lama_capital", "Лама 4 модель", "Llama 4"),
    ("llama4_lama_lower", "лама 4 быстрая", "Llama 4"),
    ("llama4_yama_en", "Yama 4 benchmark", "Llama 4"),
    ("llama4_yama_lower", "yama 4 test", "Llama 4"),
    ("llama4_version_decimal", "Лама 4.1 модель", "Llama 4.1"),
    # OpenAI additional variants
    ("openai_open_ai_dot", "Опен А.И. сделала модель", "OpenAI"),
    ("openai_open_ai_space", "это от Опен А И", "OpenAI"),
    ("openai_openai_merged", "опенаи выпустила гпт", "OpenAI"),
    ("openai_open_aj", "Опен ай запустил", "OpenAI"),
    # GPT additional variants
    ("gpt_capital_ru", "Гпт четыре", "GPT"),
    ("gpt_lower_ru", "вот гпт ответил", "GPT"),
    ("gpt_zht_ru", "жпт написал текст", "GPT"),
    ("gpt_djipiti", "джипити галлюцинирует", "GPT"),
    # Mistral
    ("mistral_mishear_mistra", "Мистраль быстрый", "Mistral"),
    ("mistral_mishear_mitral", "Митраль нейронка", "Mistral"),
    ("mistral_mishear_mitra", "Митра модель", "Mistral"),
    ("mistral_lower_mistra", "мистраль версия", "Mistral"),
    # DeepSeek
    ("deepseek_dipsik", "Дипсик лучше", "DeepSeek"),
    ("deepseek_dipsek", "дипсек отвечает", "DeepSeek"),
    # Qwen additional variants
    ("qwen_kyuen", "Кьюэн модель", "Qwen"),
    ("qwen_kuen_lower", "куэн быстрый", "Qwen"),
    # LM Studio additional variants
    ("lmstudio_el_em_dash", "Эл-эм Студио запущен", "LM Studio"),
    ("lmstudio_lyem", "лэм студио работает", "LM Studio"),
    # Hugging Face
    ("huggingface_hagin_feis", "Хагин фейс репо", "Hugging Face"),
    ("huggingface_haging_feis", "хагинг фейс датасет", "Hugging Face"),
    ("huggingface_hagging_feis", "хаггинг фейс модель", "Hugging Face"),
    # Anthropic model family — ONLY when followed by version number
    ("opus_with_version", "Опус 4 лучший", "Opus 4"),
    ("sonnet_with_version", "Соннет 4.5 быстрый", "Sonnet 4.5"),
    ("haiku_with_version", "Хайку 3.5 маленький", "Haiku 3.5"),
    ("opus_lower_with_version", "опус 4 тест", "Opus 4"),
    ("haiku_lower_with_version", "хайку 3.5 тест", "Haiku 3.5"),
]


@pytest.mark.parametrize("case_id,text,expected", POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES])
def test_positive_replacement(case_id: str, text: str, expected: str) -> None:
    """Проверяем, что мишир заменяется на каноническое написание."""
    result = ne(text)
    assert expected in result, (
        f"[{case_id}] Expected '{expected}' in normalized text, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Negative cases — must NOT be replaced (false-positive guard)
# ---------------------------------------------------------------------------
NEGATIVE_CASES: list[tuple[str, str, str]] = [
    # (id, input, must_not_be_in_result)
    # «лама» without version stays as-is (it's a real animal)
    ("lama_standalone_no_replace", "Лама — это животное", "Llama"),
    ("lama_without_digit", "Лама пасётся в горах", "Llama"),
    # «соннет» without version stays as-is (it's a Russian word for poem)
    ("sonnet_standalone_no_replace", "Это красивый соннет Шекспира", "Sonnet"),
    ("sonnet_uppercase_no_replace", "Соннет написан в 16 веке", "Sonnet"),
    # «опус» without version stays as-is (musical opus)
    ("opus_standalone_no_replace", "Девятый опус Бетховена", "Opus"),
    ("opus_uppercase_no_replace", "Опус сороковой", "Opus"),
    # «хайку» without version stays as-is (Japanese poetry form)
    ("haiku_standalone_no_replace", "Хайку — японский жанр", "Haiku"),
    # «митра» without brand context - but we do replace it, so skip — task says Митра → Mistral
    # «мистраль» (wind name) - the task explicitly lists it as a mishear to replace → no negative test needed
]


@pytest.mark.parametrize("case_id,text,must_not", NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES])
def test_negative_no_replacement(case_id: str, text: str, must_not: str) -> None:
    """Проверяем, что нейтральный контекст НЕ даёт ложного срабатывания."""
    result = ne(text)
    assert must_not not in result, (
        f"[{case_id}] Expected '{must_not}' NOT in result, got: {result!r}"
    )
