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

# STT-no-space: period (or ?!) immediately followed by a lowercase ES/EN letter —
# common Whisper output like "dime.como".  Only applied in ES mode.
# Excludes abbreviation-style runs (e.g. "e.g.", "U.S.A") by requiring the
# character BEFORE the period to be a word character (not already a digit).
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(r"([.!?])([a-záéíóúüñ¿¡])", re.IGNORECASE)

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

        # Нормализация STT «без пробела после знака»: Whisper иногда выводит
        # "dime.como te llamas?" (без пробела после точки перед строчной буквой).
        # Вставляем пробел, чтобы сплиттер предложений мог корректно разбить текст.
        # Применяется только для ES, до marker-insertion.
        result = _NO_SPACE_AFTER_SENT_LOWER_ES_RE.sub(r"\1 \2", result)

        # Капитализировать первое слово
        if result and result[0].islower():
            result = result[0].upper() + result[1:]

        # Добавить ¿/¡ к каждому предложению отдельно, а не ко всему тексту.
        # Разбиваем на токены: разделители (.!?) сохраняются в выводе.
        result = self._apply_inverted_markers_per_sentence(result)

        return result

    # Pattern splits on sentence-ending punctuation, keeping the delimiter in
    # the list via a capturing group.  E.g. "Hola. cómo estás?" →
    # ["Hola", ".", " cómo estás", "?", ""]
    _SENT_SPLIT_RE = re.compile(r"([.!?…]+)")

    def _apply_inverted_markers_per_sentence(self, text: str) -> str:
        """Prepend ¿/¡ to each individual sentence that ends with ?/! only."""
        parts = self._SENT_SPLIT_RE.split(text)
        # parts alternates: [sentence_body, delimiter, sentence_body, delimiter, …, tail]
        # Reconstruct, adding markers to each (body, delimiter) pair.
        out: List[str] = []
        i = 0
        while i < len(parts):
            body = parts[i]
            # Try to get the following delimiter (if any).
            if i + 1 < len(parts):
                delim = parts[i + 1]
                i += 2
            else:
                # Last tail with no trailing delimiter.
                out.append(body)
                break

            stripped_body = body.strip()
            # Determine the effective end character for this sentence.
            last_char = delim[-1] if delim else ""

            if last_char == "?" and stripped_body and not stripped_body.startswith("¿"):
                # Prepend ¿ right before the first non-whitespace character in body.
                leading_ws = len(body) - len(body.lstrip())
                body = body[:leading_ws] + "¿" + body[leading_ws:]
            elif last_char == "!" and stripped_body and not stripped_body.startswith("¡"):
                leading_ws = len(body) - len(body.lstrip())
                body = body[:leading_ws] + "¡" + body[leading_ws:]

            out.append(body)
            out.append(delim)

        return "".join(out)

    def get_fixes_applied(self, original: str, fixed: str, language: str = "ru") -> List[str]:
        """Возвращает список описаний применённых изменений.

        Args:
            original: Исходный текст до коррекции.
            fixed: Текст после коррекции.
            language: Код языка ("ru" / "es"), нужен для language-specific проверок
                      (например, я-капитализация — только для RU).

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

        # Я-капитализация специфична для русского языка (W1348 R2 gate)
        if language == "ru" and _STANDALONE_YA_RE.search(original) and "Я" not in original:
            fixes.append("capitalized standalone 'я'")

        if '"' in original and "«" in fixed:
            fixes.append("fixed quotation marks to «»")

        if _CAPITALIZE_AFTER_SENT_RE.search(original):
            fixes.append("capitalized after sentence ending")

        # Испанский: добавление ¿/¡ (per-sentence — достаточно найти хоть один маркер в fixed)
        if "?" in original and "¿" in fixed and "¿" not in original:
            fixes.append("added ¿ before question")

        if "!" in original and "¡" in fixed and "¡" not in original:
            fixes.append("added ¡ before exclamation")

        if not fixes:
            fixes.append("punctuation corrected")

        return fixes
