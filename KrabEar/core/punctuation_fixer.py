"""Умная коррекция пунктуации для вывода STT.

PunctuationFixer применяется как опциональный этап конвейера после TextUtils.cleanup_transcript.
Поддерживает русский (ru) и испанский (es) языки.
"""

import re
import logging
from typing import List

logger = logging.getLogger("KrabEar.PunctuationFixer")

# ── Precompiled patterns ────────────────────────────────────────────────────

# Пробел перед знаками препинания (,.:;!?)
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.:;!?»])")

# Отсутствие пробела после знаков препинания (,.:;!? — но не декимальные дроби и не «)
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»])([^\s\d»\"')\]])")
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(\.)([А-ЯA-ZЁ])")

# Множественные пробелы
_MULTI_SPACE_RE = re.compile(r"  +")

# Конец строки без точки (последний символ не знак)
_MISSING_PERIOD_RE = re.compile(r"([А-Яа-яA-Za-zЁё0-9\)])$")

# Капитализация после конца предложения
_CAPITALIZE_AFTER_SENT_RE = re.compile(r"([.!?…]\s+)([а-яёa-z])")

# Одиночное «я» (личное местоимение) должно быть с большой буквы
_STANDALONE_YA_RE = re.compile(r"(?<!\w)(я)(?!\w)")

# Кавычки ASCII вокруг русского текста → «»
_ASCII_QUOTE_BLOCK_RE = re.compile(r'"([^"]{1,80})"')

# Испанский: вопросительное предложение без ¿
# Признак: заканчивается на ? и не начинается с ¿
_ES_QUESTION_MISSING_IQUEST_RE = re.compile(r"^(?!¿)(.+\?)$")

# Испанский: восклицательное без ¡
_ES_EXCL_MISSING_IEXCL_RE = re.compile(r"^(?!¡)(.+!)$")


class PunctuationFixer:
    """Детерминированная коррекция пунктуации для вывода STT.

    Использование:
        fixer = PunctuationFixer()
        result = fixer.fix("привет как дела", language="ru")
        changes = fixer.get_fixes_applied("привет как дела", result)
    """

    def fix(self, text: str, language: str = "ru") -> str:
        """Применяет набор пунктуационных правил к тексту.

        Args:
            text: Исходный текст.
            language: Код языка: "ru" (русский) или "es" (испанский).

        Returns:
            Откорректированный текст.
        """
        if not text or not text.strip():
            return text

        result = text

        # Общие правила (применяются для всех языков)
        # Порядок важен: сначала добавить пробелы после знаков (шаг A),
        # затем убрать пробелы перед знаками (шаг B) — иначе пробелы,
        # введённые шагом A, не попадут под очистку шага B (W1348 R1).
        result = _MULTI_SPACE_RE.sub(" ", result)
        result = _NO_SPACE_AFTER_PUNCT_RU_RE.sub(r"\1 \2", result)
        result = _NO_SPACE_AFTER_PERIOD_RE.sub(r"\1 \2", result)
        result = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", result)
        result = _CAPITALIZE_AFTER_SENT_RE.sub(lambda m: m.group(1) + m.group(2).upper(), result)

        if language == "ru":
            result = self._fix_russian(result)
        elif language == "es":
            result = self._fix_spanish(result)

        # Добавить точку в конце если её нет (для всех языков)
        result = _MISSING_PERIOD_RE.sub(r"\1.", result)

        return result.strip()

    def _fix_russian(self, text: str) -> str:
        """Правила, специфичные для русского языка."""
        result = text

        # «я» как самостоятельное слово → «Я»
        result = _STANDALONE_YA_RE.sub("Я", result)

        # ASCII-кавычки вокруг текста → «»
        result = _ASCII_QUOTE_BLOCK_RE.sub(r"«\1»", result)

        # Капитализировать первое слово предложения
        if result and result[0].islower():
            result = result[0].upper() + result[1:]

        return result

    def _fix_spanish(self, text: str) -> str:
        """Правила, специфичные для испанского языка."""
        result = text

        # Капитализировать первое слово
        if result and result[0].islower():
            result = result[0].upper() + result[1:]

        # Добавить ¿ к вопросам
        if result.rstrip().endswith("?") and not result.lstrip().startswith("¿"):
            result = "¿" + result.lstrip()

        # Добавить ¡ к восклицаниям
        if result.rstrip().endswith("!") and not result.lstrip().startswith("¡"):
            result = "¡" + result.lstrip()

        return result

    def get_fixes_applied(self, original: str, fixed: str) -> List[str]:
        """Возвращает список описаний применённых изменений.

        Args:
            original: Исходный текст до коррекции.
            fixed: Текст после коррекции.

        Returns:
            Список строк-описаний изменений (пустой, если изменений нет).
        """
        if original == fixed:
            return []

        fixes: List[str] = []

        if _MULTI_SPACE_RE.search(original):
            fixes.append("removed double spaces")

        if _SPACE_BEFORE_PUNCT_RE.search(original):
            fixes.append("removed space before punctuation")

        if _MISSING_PERIOD_RE.search(original) and not original.rstrip().endswith(
            (".", "!", "?", "…")
        ):
            fixes.append("added missing period")

        if original and original[0].islower() and fixed and fixed[0].isupper():
            fixes.append("capitalized first letter")

        if _STANDALONE_YA_RE.search(original) and "Я" not in original:
            fixes.append("capitalized standalone 'я'")

        if '"' in original and "«" in fixed:
            fixes.append("fixed quotation marks to «»")

        if _CAPITALIZE_AFTER_SENT_RE.search(original):
            fixes.append("capitalized after sentence ending")

        # Испанский: добавление ¿/¡
        if original.rstrip().endswith("?") and not original.lstrip().startswith("¿") and fixed.startswith("¿"):
            fixes.append("added ¿ before question")

        if original.rstrip().endswith("!") and not original.lstrip().startswith("¡") and fixed.startswith("¡"):
            fixes.append("added ¡ before exclamation")

        if not fixes:
            fixes.append("punctuation corrected")

        return fixes
