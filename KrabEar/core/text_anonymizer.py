"""Анонимизатор текста Krab Ear.

Редактирует персональные данные из транскрипций: телефоны, email, номера банковских карт,
паспортные данные, даты рождения и произвольные пользовательские паттерны.
Не требует внешних зависимостей — только стандартная библиотека (re).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Максимальный размер текста (символов), который обрабатывается регулярными выражениями.
# Часовая запись в норме <100 KB; backstop 500 KB защищает от DoS независимо от паттерна.
_MAX_ANONYMIZE_LEN = 500_000

# Запас для устранения утечки PII на границе обрезки (wave1766).
# Токен PII, начинающийся до _MAX_ANONYMIZE_LEN и выходящий за её пределы,
# попадает в «хвост» без редактирования, если сканируется только text[:_MAX_ANONYMIZE_LEN].
# Расширяем окно сканирования на _MAX_PII_LEN символов, чтобы любой токен,
# пересекающий исходную границу, был полностью виден регулярным выражениям.
# 64 символа покрывают самый длинный реалистичный PII-токен (IBAN ~34 симв.,
# международный телефон ~20 симв., email ~54 симв. с учётом TLD).
_MAX_PII_LEN = 64


# ── ИНН checksum helpers ─────────────────────────────────────────────────────

def _passes_inn_fl_checksum(digits: str) -> bool:
    """Проверяет контрольную сумму ИНН физического лица (12 цифр).

    Алгоритм:
    - 11-й знак: коэффициенты [7,2,4,10,3,5,9,4,6,8] для знаков 1–10
    - 12-й знак: коэффициенты [3,7,2,4,10,3,5,9,4,6,8] для знаков 1–11
    """
    if len(digits) != 12:
        return False
    try:
        d = [int(c) for c in digits]
    except ValueError:
        return False
    c11 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    c12 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
    check11 = sum(d[i] * c11[i] for i in range(10)) % 11 % 10
    check12 = sum(d[i] * c12[i] for i in range(11)) % 11 % 10
    return d[10] == check11 and d[11] == check12


def _passes_inn_yul_checksum(digits: str) -> bool:
    """Проверяет контрольную сумму ИНН юридического лица (10 цифр).

    Алгоритм:
    - Коэффициенты [2,4,10,3,5,9,4,6,8] для знаков 1–9
    - 10-й знак == sum(d[i]*c[i] for i in range(9)) % 11 % 10
    """
    if len(digits) != 10:
        return False
    try:
        d = [int(c) for c in digits]
    except ValueError:
        return False
    coefs = [2, 4, 10, 3, 5, 9, 4, 6, 8]
    check = sum(d[i] * coefs[i] for i in range(9)) % 11 % 10
    return d[9] == check


# ── Luhn checksum helper ─────────────────────────────────────────────────────

def _passes_luhn(digits: str) -> bool:
    """Verify number passes Luhn algorithm (mod 10 checksum)."""
    try:
        nums = [int(d) for d in digits]
    except ValueError:
        return False
    checksum = 0
    parity = len(nums) % 2
    for i, num in enumerate(nums):
        if i % 2 == parity:
            num *= 2
            if num > 9:
                num -= 9
        checksum += num
    return checksum % 10 == 0


# ── СНИЛС checksum helper (W1022 F1) ────────────────────────────────────────

def _snils_valid(num: str, expected_check: Optional[int] = None) -> bool:
    """Проверяет контрольное число СНИЛС (mod-101).

    Два режима:
    1. _snils_valid(digits9, expected_check) — принимает 9 первых цифр и
       ожидаемое контрольное число; возвращает True если совпадает.
    2. _snils_valid(full_snils) — принимает полный СНИЛС (11 цифр, разделители
       игнорируются); проверяет контрольное число из последних 2 цифр.

    Алгоритм (по ПФРФ):
      sum9 = sum(d[i] * (9 - i) for i in range(9))
      if sum9 < 100:    check2 = sum9
      elif sum9 in (100, 101): check2 = 0
      else:
          r = sum9 % 101
          check2 = 0 if r in (100, 101) else r
    """
    try:
        if expected_check is not None:
            # Режим 1: num — 9 цифр, expected_check — ожидаемый check2
            digits9 = num
            if len(digits9) != 9:
                return False
            digits = [int(c) for c in digits9]
            s = sum(digits[i] * (9 - i) for i in range(9))
            if s < 100:
                check2 = s
            elif s in (100, 101):
                check2 = 0
            else:
                r = s % 101
                check2 = 0 if r in (100, 101) else r
            return check2 == expected_check
        else:
            # Режим 2: num — полный СНИЛС (11 значащих цифр)
            stripped = re.sub(r"[^\d]", "", num)
            if len(stripped) != 11:
                return False
            digits = [int(c) for c in stripped]
            s = sum(digits[i] * (9 - i) for i in range(9))
            if s < 100:
                check2 = s
            elif s in (100, 101):
                check2 = 0
            else:
                r = s % 101
                check2 = 0 if r in (100, 101) else r
            stored = digits[9] * 10 + digits[10]
            return check2 == stored
    except (ValueError, IndexError):
        return False


# ── IBAN mod-97 checksum helper (W1022 F4) ───────────────────────────────────

def _iban_valid(s: str) -> bool:
    """Проверяет контрольную сумму IBAN по ISO 13616 (mod-97).

    Алгоритм:
    1. Убрать пробелы и дефисы, перевести в верхний регистр.
       (W20: дефисы добавлены — паттерн теперь матчит IBAN с дефисами.)
    2. Переместить первые 4 символа в конец.
    3. Заменить буквы: A=10, B=11, …, Z=35.
    4. Вычислить int(строка) % 97 — должно быть равно 1.
    """
    try:
        cleaned = re.sub(r"[\s\-]", "", s).upper()
        if len(cleaned) < 5:
            return False
        rearranged = cleaned[4:] + cleaned[:4]
        numeric = "".join(
            str(ord(c) - ord("A") + 10) if c.isalpha() else c
            for c in rearranged
        )
        return int(numeric) % 97 == 1
    except (ValueError, AttributeError):
        return False


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
    # UK телефоны: +44 7xxx xxxxxx / +44 20 xxxx xxxx / +44(0)7xxx xxxxxx
    (
        "phone_uk",
        r"(?<!\d)"
        r"\+44[\s\-]?(?:\(0\))?[\s\-]?\d{2,5}[\s\-]?\d{3,4}[\s\-]?\d{3,4}"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # DE телефоны: +49 30 xxxxxxxx / +49 171 xxxxxxx / +49(0)30 xxxxxxxx
    (
        "phone_de",
        r"(?<!\d)"
        r"\+49[\s\-]?(?:\(0\))?[\s\-]?\d{2,5}[\s\-]?\d{3,8}"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # FR телефоны: +33 1 xx xx xx xx / +33 6 xx xx xx xx / +33(0)6 xx xx xx xx
    (
        "phone_fr",
        r"(?<!\d)"
        r"\+33[\s\-]?(?:\(0\))?[\s\-]?[1-9](?:[\s\-]?\d{2}){4}"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # IT телефоны: +39 06 xxxxxxxx / +39 333 xxxxxxx / +39(0)6 xxxxxxxx
    (
        "phone_it",
        r"(?<!\d)"
        r"\+39[\s\-]?(?:\(0\))?[\s\-]?\d{1,4}[\s\-]?\d{5,8}"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # ES телефоны (W20): +34 612 34 56 78 / 918 123 456 / +34612345678
    # Испания — первичный язык продукта (RU/ES).
    # Мобильные начинаются с 6-7, стационарные с 8-9 (на уровне страны).
    # Два альтернативных правила вместо одного, чтобы избежать variable-width lookbehind:
    # (a) с явным +34 — матчит любой испанский номер с кодом страны;
    # (b) без кода страны — 9 цифр, начинающихся с [6-9], с lookbehind только на одну цифру.
    # Правило (b) не даёт false-positive на RU/UK/DE/etc., т.к. те правила идут раньше.
    (
        "phone_es",
        r"(?<!\d)"
        r"(?:"
        r"\+34[\s\-]?[6-9]\d{2}(?:[\s\-]?\d{2}){3}"  # +34 6xx xx xx xx (с кодом страны)
        r"|"
        r"[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{3}"  # 9xx / 6xx без кода (9 цифр, разделители)
        r")"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # Общий E.164 fallback для международных номеров не охваченных страновыми правилами.
    # Формат: + код страны (1-3 цифры) + 7-14 цифр с необязательными пробелами/дефисами.
    # Bounded quantifiers: {1,3} и {7,14} → ReDoS-safe.
    (
        "phone_e164",
        r"(?<!\d)"
        r"\+(?!7\b|44\b|49\b|33\b|39\b|34\b)"  # исключаем уже охваченные коды стран
        r"\d{1,3}[\s\-]?(?:\d[\s\-]?){7,14}\d"
        r"(?!\d)",
        "[ТЕЛЕФОН]",
    ),
    # Email-адреса (W1758 — ReDoS-safe переписка).
    #
    # Старый паттерн:  [a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}
    # Проблема: домен [a-zA-Z0-9.\-]+ и TLD \.[a-zA-Z]{2,} пересекаются по
    # символам (буквы + точка), что порождает катастрофическое backtracking:
    # `x@aaaa....` → O(2^n) итераций движка.
    #
    # Новый паттерн строит домен из атомарных меток без точек:
    #   (?:[a-zA-Z0-9\-]{1,63}\.)+  — каждая метка не содержит '.' →
    #   между группой меток и TLD нет пересечения → линейный O(n) обход.
    # RFC-ограничения: local ≤64 символа, метка ≤63 символа, TLD ≤24 символа.
    (
        "email",
        r"[a-zA-Z0-9._%+\-]{1,64}@(?:[a-zA-Z0-9\-]{1,63}\.)+[a-zA-Z]{2,24}",
        "[EMAIL]",
    ),
    # Номера банковских карт: 16-значные Visa/MC/UnionPay и 15-значные Amex.
    # W20: добавлен формат Amex (15 цифр: 4-6-5 с разделителями или без),
    # а разделитель расширен до [\s\-.] (включая точку для речевых форматов).
    # Все совпадения проверяются алгоритмом Luhn в anonymize() → отсеивает false-positives.
    (
        "credit_card",
        r"\b(?:\d{4}[\s\-.]{0,1}){3}\d{4}\b"  # 0000[sep]0000[sep]0000[sep]0000 (16 digits)
        r"|"
        r"\b\d{16}\b"  # 0000000000000000
        r"|"
        r"\b3[47]\d{2}[\s\-.]{0,1}\d{6}[\s\-.]{0,1}\d{5}\b",  # Amex 4-6-5 (15 digits)
        "[КАРТА]",
    ),
    # ИНН юридического лица (10 цифр) — должен идти раньше passport,
    # т.к. правило passport тоже матчит \d{10} (паспорт без пробела).
    # Checksum-валидация в anonymize() отсеивает несовпадения.
    (
        "inn_yul",
        r"\b\d{10}\b",
        "[ИНН_ЮЛ]",
    ),
    # Паспортные номера РФ: серия 0000 № 000000 или 0000000000 (10 цифр)
    # Примечание: \d{10} здесь не поймает ИНН ЮЛ — они уже поглощены выше.
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
    # Контрольное число проверяется алгоритмом mod-101 в anonymize() (W1022 F1)
    (
        "snils",
        r"\b\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{2}\b",
        "[СНИЛС]",
    ),
    # US SSN: XXX-XX-XXXX (с явными ограничениями на невалидные блоки)
    # Checksum не определён стандартом; фильтруем структурно-невалидные диапазоны
    # (area 000, 666, 900-999; group 00; serial 0000)
    (
        "us_ssn",
        r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b",
        "[SSN]",
    ),
    # IBAN: 2 буквы кода страны + 2 контрольных цифры + до 30 символов BBAN.
    # W20: допускаем необязательные пробелы/дефисы между группами символов
    # (IBAN диктуется/пишется по 4 знака: DE89 3704 0044 0532 0130 00).
    # Границы {10,30} → ReDoS-safe (нет вложенных квантификаторов).
    # _iban_valid() уже убирает пробелы и делает mod-97 проверку — гасит false positives.
    (
        "iban",
        r"\b[A-Z]{2}\d{2}(?:[\s\-]?[A-Z0-9]){10,30}\b",
        "[IBAN]",
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

        Защита от DoS (W1758):
            Если len(text) > _MAX_ANONYMIZE_LEN, регулярки применяются только к
            первым _MAX_ANONYMIZE_LEN символам; хвост присоединяется без изменений.
            Факт обрезки логируется структурно (без текста транскрипции).
        """
        if not text:
            return AnonymizeResult(
                anonymized_text=text,
                redactions=[],
                redaction_count=0,
            )

        # ── Backstop: ограничиваем скан при аномально длинном входе ────────
        # wave1766: расширяем окно сканирования на _MAX_PII_LEN символов за
        # исходную границу, чтобы PII-токены, пересекающие _MAX_ANONYMIZE_LEN,
        # полностью попадали в поле зрения регулярных выражений.
        # Хвост присоединяется только начиная с реальной границы расширенного окна.
        scan_text = text
        tail = ""
        if len(text) > _MAX_ANONYMIZE_LEN:
            extended_end = _MAX_ANONYMIZE_LEN + _MAX_PII_LEN
            scan_text = text[:extended_end]
            tail = text[extended_end:]
            logger.warning(
                "anonymize: входной текст обрезан для сканирования",
                extra={
                    "original_len": len(text),
                    "scan_len": extended_end,
                },
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
            for m in pattern.finditer(scan_text):
                if name == "credit_card":
                    # Validate via Luhn checksum — strip spaces, dashes, and dots (W20: dot separator added)
                    digits = re.sub(r"[\s\-.]", "", m.group(0))
                    if not _passes_luhn(digits):
                        continue
                elif name == "inn_yul":
                    # Validate ИНН ЮЛ checksum — skip random 10-digit numbers
                    if not _passes_inn_yul_checksum(m.group(0)):
                        continue
                elif name == "inn":
                    # Validate ИНН ФЛ checksum — skip random 12-digit numbers
                    if not _passes_inn_fl_checksum(m.group(0)):
                        continue
                elif name == "snils":
                    # Validate СНИЛС mod-101 checksum (W1022 F1) — skip bad checksums
                    if not _snils_valid(m.group(0)):
                        continue
                elif name == "iban":
                    # Validate IBAN mod-97 checksum (W1022 F4) — skip bad checksums
                    if not _iban_valid(m.group(0)):
                        continue
                matches.append((m.start(), m.end(), m.group(0), replacement, name))

        if not matches:
            return AnonymizeResult(
                anonymized_text=scan_text + tail,
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
            parts.append(scan_text[cursor:start])
            parts.append(replacement)
            redactions.append(Redaction(
                original=original,
                replacement=replacement,
                category=category,
                position=start,
            ))
            cursor = end
        parts.append(scan_text[cursor:])
        if tail:
            parts.append(tail)

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
