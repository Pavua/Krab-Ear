"""Анонимизатор текста Krab Ear.

Редактирует персональные данные из транскрипций: телефоны, email, номера банковских карт,
паспортные данные, даты рождения и произвольные пользовательские паттерны.
Не требует внешних зависимостей — только стандартная библиотека (re).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── Датаклассы результата ────────────────────────────────────────────────────

@dataclass
class Redaction:
    """Одна замена в тексте."""
    original: str
    replacement: str
    category: str
    position: int  # смещение в символах в оригинальном тексте


@dataclass
class AnonymizeResult:
    """Результат анонимизации."""
    anonymized_text: str
    redactions: list[Redaction]
    redaction_count: int


# ── Встроенные правила ───────────────────────────────────────────────────────

# Формат: (name, compiled_pattern, replacement_label)
_BUILTIN_RULES_RAW: list[tuple[str, str, str]] = [
    # Российские телефоны: +7/8 и различные форматы скобок/дефисов/пробелов
    (
        "phone",
        r"(?<!\d)"
        r"(?:"
        # +7 (999) 123-45-67  /  +7(999)1234567  /  +79991234567
        r"\+7[\s\-]?[\(\s]?\d{3}[\)\s]?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
        r"|"
        # 8 (999) 123-45-67  /  8(999)1234567  /  89991234567
        r"8[\s\-]?[\(\s]?\d{3}[\)\s]?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
        r"|"
        # Короткий локальный: (999) 123-45-67
        r"\(\d{3,4}\)[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
        r")"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # Email-адреса
    (
        "email",
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        "[EMAIL]",
    ),
    # Номера банковских карт: 16 цифр (с пробелами/дефисами или без)
    (
        "credit_card",
        r"\b(?:\d{4}[\s\-]){3}\d{4}\b"  # 0000 0000 0000 0000
        r"|"
        r"\b\d{16}\b",  # 0000000000000000
        "[КАРТА]",
    ),
    # Паспортные номера РФ: серия 0000 № 000000 или 0000000000 (10 цифр)
    (
        "passport",
        r"\b(?:\d{4}[\s\-]\d{6}|\d{10})\b",
        "[ПАСПОРТ]",
    ),
    # Дата рождения: ДД.ММ.ГГГГ  /  ДД/ММ/ГГГГ  /  ДД-ММ-ГГГГ
    (
        "date_of_birth",
        r"\b(?:0?[1-9]|[12]\d|3[01])[.\-/](?:0?[1-9]|1[0-2])[.\-/](?:19|20)\d{2}\b",
        "[ДАТА_РОЖДЕНИЯ]",
    ),
    # ИНН физического лица (12 цифр)
    (
        "inn",
        r"\b\d{12}\b",
        "[ИНН]",
    ),
    # СНИЛС: XXX-XXX-XXX XX  /  XXXXXXXXXXX (11 цифр)
    (
        "snils",
        r"\b\d{3}[\s\-]\d{3}[\s\-]\d{3}[\s\-]?\d{2}\b",
        "[СНИЛС]",
    ),
]


def _compile_rules(raw: list[tuple[str, str, str]]) -> list[tuple[str, re.Pattern, str]]:
    return [(name, re.compile(pattern, re.IGNORECASE), repl) for name, pattern, repl in raw]


class TextAnonymizer:
    """Анонимизатор персональных данных в тексте транскрипции.

    Использует регулярные выражения — нет внешних зависимостей.
    Поддерживает пользовательские правила через add_custom_rule().
    """

    def __init__(self) -> None:
        # Список (name, compiled_pattern, replacement)
        self._rules: list[tuple[str, re.Pattern, str]] = _compile_rules(_BUILTIN_RULES_RAW)
        # Дополнительные правила пользователя (добавляются в конец)
        self._custom_rules: list[tuple[str, re.Pattern, str]] = []

    # ── Публичный API ────────────────────────────────────────────────────────

    def anonymize(
        self,
        text: str,
        rules: Optional[list[str]] = None,
    ) -> AnonymizeResult:
        """Анонимизирует текст, заменяя персональные данные плейсхолдерами.

        Аргументы:
            text  — исходный текст.
            rules — список имён правил для применения. Если None, применяются все правила.

        Возвращает AnonymizeResult с анонимизированным текстом, списком замен и их числом.
        """
        if not text:
            return AnonymizeResult(
                anonymized_text=text,
                redactions=[],
                redaction_count=0,
            )

        all_rules = self._rules + self._custom_rules

        # Выбираем нужные правила
        if rules is not None:
            rule_set = set(rules)
            selected_rules = [(n, p, r) for n, p, r in all_rules if n in rule_set]
        else:
            selected_rules = all_rules

        # Собираем все совпадения со смещениями (в оригинальном тексте)
        matches: list[tuple[int, int, str, str, str]] = []  # (start, end, original, replacement, category)
        for name, pattern, replacement in selected_rules:
            for m in pattern.finditer(text):
                matches.append((m.start(), m.end(), m.group(0), replacement, name))

        if not matches:
            return AnonymizeResult(
                anonymized_text=text,
                redactions=[],
                redaction_count=0,
            )

        # Сортируем по позиции; при пересечении берём длиннее
        matches.sort(key=lambda x: (x[0], -(x[1] - x[0])))

        # Убираем перекрывающиеся совпадения (жадный левый выбор)
        non_overlapping: list[tuple[int, int, str, str, str]] = []
        cursor = 0
        for start, end, original, replacement, category in matches:
            if start < cursor:
                continue
            non_overlapping.append((start, end, original, replacement, category))
            cursor = end

        # Строим результат
        redactions: list[Redaction] = []
        parts: list[str] = []
        cursor = 0
        for start, end, original, replacement, category in non_overlapping:
            parts.append(text[cursor:start])
            parts.append(replacement)
            redactions.append(Redaction(
                original=original,
                replacement=replacement,
                category=category,
                position=start,
            ))
            cursor = end
        parts.append(text[cursor:])

        anonymized = "".join(parts)
        return AnonymizeResult(
            anonymized_text=anonymized,
            redactions=redactions,
            redaction_count=len(redactions),
        )

    def add_custom_rule(
        self,
        name: str,
        pattern: str,
        replacement: str,
    ) -> None:
        """Добавляет пользовательское правило анонимизации.

        Аргументы:
            name        — уникальное имя правила (используется при выборе rules=).
            pattern     — регулярное выражение Python.
            replacement — строка-заменитель (например "[CUSTOM]").
        """
        compiled = re.compile(pattern, re.IGNORECASE)
        self._custom_rules.append((name, compiled, replacement))

    def list_rules(self) -> list[str]:
        """Возвращает имена всех активных правил (встроенных + пользовательских)."""
        all_rules = self._rules + self._custom_rules
        return [name for name, _, _ in all_rules]
