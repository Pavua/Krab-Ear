"""Умная коррекция пунктуации для вывода STT.

PunctuationFixer применяется как опциональный этап конвейера после TextUtils.cleanup_transcript.
Поддерживает русский (ru), испанский (es) и английский (en) языки.
"""

import re
import logging
from typing import List

logger = logging.getLogger("KrabEar.PunctuationFixer")

# ── Константы безопасности ────────────────────────────────────────────────────

# Максимальная длина входного текста в символах.
# STT-транскрипты Krab Ear < 2 КБ; 100 000 — щедрый запас для импортированных файлов.
# Backstop защищает от ReDoS-атак через патологически длинный ввод.
_MAX_INPUT_LEN: int = 100_000

# ── Precompiled patterns ────────────────────────────────────────────────────

# W1763 (ReDoS-fix): нормализация всех пробельных символов в единственный пробел.
# Используется ВМЕСТО _MULTI_SPACE_RE в pipeline fix() — O(n), без capture-группы,
# движок CPython не выполняет backtracking на near-miss.
# После этого шага все пробельные серии (включая табуляцию и переводы строки)
# сводятся к одному пробелу — это безопасно для STT-транскриптов.
_ALL_WS_RE = re.compile(r"\s+")

# W1763 (ReDoS-fix): после нормализации пробелов перед знаком препинания
# может быть только ровно один пробел — убираем его простым O(n) паттерном
# без квантификатора (нет backtracking при near-miss).
# Замена: \s+([,.:;!?»]) → ' ([,.:;!?»])'.  Семантика идентична.
_SPACE_BEFORE_PUNCT_RE = re.compile(r" ([,.:;!?»])")

# Отсутствие пробела после знаков препинания (,.:;!? — но не декимальные дроби и не «)
# W1376: включаем двоеточие (:), исключая протокол URL (://)
_NO_SPACE_AFTER_PUNCT_RU_RE = re.compile(r"([,;!?»]|:(?!/))([^\s\d»\"')\]])")
# W1377: паттерн для нахождения точки перед заглавной буквой (обработка через callback)
_NO_SPACE_AFTER_PERIOD_RE = re.compile(r"(?<=[^\s])(\.)([А-ЯA-ZЁ])")

# STT-no-space: period (or ?!) immediately followed by a lowercase ES/EN letter —
# common Whisper output like "dime.como".  Only applied in ES mode.
# Excludes abbreviation-style runs (e.g. "e.g.", "U.S.A") by requiring the
# character BEFORE the period to be a word character (not already a digit).
_NO_SPACE_AFTER_SENT_LOWER_ES_RE = re.compile(r"([.!?])([a-záéíóúüñ¿¡])", re.IGNORECASE)

# Множественные пробелы — используется только в get_fixes_applied() для детектирования.
# В pipeline fix() заменён на _ALL_WS_RE (см. W1763 выше).
_MULTI_SPACE_RE = re.compile(r"  +")

# Конец строки без точки (последний символ не знак)
_MISSING_PERIOD_RE = re.compile(r"([А-Яа-яA-Za-zЁё0-9\)])$")

# Капитализация после конца предложения
_CAPITALIZE_AFTER_SENT_RE = re.compile(r"([.!?…]\s+)([а-яёa-z])")

# Кавычки ASCII вокруг русского текста → «»
_ASCII_QUOTE_BLOCK_RE = re.compile(r'"([^"]{1,80})"')

# Испанский: вопросительное предложение без ¿
# Признак: заканчивается на ? и не начинается с ¿
_ES_QUESTION_MISSING_IQUEST_RE = re.compile(r"^(?!¿)(.+\?)$")

# Испанский: восклицательное без ¡
_ES_EXCL_MISSING_IEXCL_RE = re.compile(r"^(?!¡)(.+!)$")

# Английский: одиночные прямые кавычки вокруг слов → типографские
_EN_APOSTROPHE_RE = re.compile(r"(?<=\w)'(?=\w)")

# Английский: двойные ASCII-кавычки вокруг текста → "…"
_EN_DOUBLE_QUOTE_OPEN_RE = re.compile(r'(?<!\w)"(?=\S)')
_EN_DOUBLE_QUOTE_CLOSE_RE = re.compile(r'(?<=\S)"(?!\w)')


def _insert_period_space(match: "re.Match[str]") -> str:
    """W1377: context-aware period+capital boundary insertion.

    Inserts a space between a period and a following capital letter ONLY when
    it is a real sentence boundary.  Skips:
      - Single letter before dot (abbreviation): т.е.П → т.е.П  (no space)
      - Digit before dot (version/decimal):      v1.0.Beta → v1.0.Beta (no space)

    A "single letter before dot" means the character immediately before the dot
    is a letter AND the character before THAT is NOT also a plain letter
    (i.e. it is a dot, non-word char, or start of string) — this identifies
    abbreviation-style sequences like т.е., e.g., etc.

    Full words ending in a letter (Текст, США, Конец) have at least two
    consecutive letters before the dot, so they are treated as sentence boundaries
    and a space is inserted.
    """
    full = match.string
    dot_pos = match.start(1)   # position of '.'
    capital = match.group(2)

    # Check char immediately before the dot
    if dot_pos > 0:
        pre_dot = full[dot_pos - 1]

        # Digit before dot → version / decimal (v1.0.Beta, 2.3.5)
        if pre_dot.isdigit():
            return match.group(0)  # no space inserted

        # Single letter before dot: only an abbreviation if the char before THAT
        # is not also a plain letter (i.e. it's a dot, space, non-word char, or
        # start of string).  Example: т.е.П → dot_pos-2 is '.', so skip.
        # Counter-example: Текст.С → dot_pos-2 is 'с' (letter), so insert.
        if pre_dot.isalpha() and dot_pos >= 2:
            pre_pre_dot = full[dot_pos - 2]
            if not pre_pre_dot.isalpha():
                # Abbreviation pattern: X.Capital where X is single letter
                return match.group(0)  # no space inserted

    # Real sentence boundary — insert space
    return match.group(1) + " " + capital


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
            language: Код языка: "ru" (русский), "es" (испанский) или "en" (английский).

        Returns:
            Откорректированный текст.
        """
        if not text or not text.strip():
            return text

        # W1763: backstop длины — защита от ReDoS через патологически длинный ввод.
        # Обрезаем до _MAX_INPUT_LEN перед любыми regex-операциями.
        if len(text) > _MAX_INPUT_LEN:
            logger.warning(
                "Входной текст обрезан до _MAX_INPUT_LEN символов (W1763 backstop)",
                extra={"original_len": len(text), "max_len": _MAX_INPUT_LEN},
            )
            text = text[:_MAX_INPUT_LEN]

        result = text

        # W1763 (ReDoS-fix): нормализация ВСЕХ пробельных символов в один пробел.
        # Заменяет _MULTI_SPACE_RE.sub(" ", ...) — более широкая нормализация (табы, \n).
        # _ALL_WS_RE не имеет capture-группы → O(n), нет backtracking при near-miss.
        # После этого шага _SPACE_BEFORE_PUNCT_RE (без \s+) гарантированно O(n).
        result = _ALL_WS_RE.sub(" ", result)

        # W1348: add-space-after FIRST, then remove-space-before — so newly
        # inserted spaces before punct (e.g. «стоп».) get cleaned up correctly.
        result = _NO_SPACE_AFTER_PUNCT_RU_RE.sub(r"\1 \2", result)   # add space after ,;!?»:
        result = _NO_SPACE_AFTER_PERIOD_RE.sub(_insert_period_space, result)  # W1377 callback
        result = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", result)            # remove space before punct
        result = _CAPITALIZE_AFTER_SENT_RE.sub(lambda m: m.group(1) + m.group(2).upper(), result)

        if language == "ru":
            result = self._fix_russian(result)
        elif language == "es":
            result = self._fix_spanish(result)
        elif language == "en":
            result = self._fix_english(result)

        # Добавить точку в конце если её нет (для всех языков)
        result = _MISSING_PERIOD_RE.sub(r"\1.", result)

        return result.strip()

    def _fix_russian(self, text: str) -> str:
        """Правила, специфичные для русского языка."""
        result = text

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

    def _fix_english(self, text: str) -> str:
        """Rules specific to English."""
        result = text

        # Capitalize first letter of the text
        if result and result[0].islower():
            result = result[0].upper() + result[1:]

        # Typographic apostrophes in contractions (it's, don't, I'm, …)
        result = _EN_APOSTROPHE_RE.sub("’", result)

        # Straight double-quotes → curly open/close
        result = _EN_DOUBLE_QUOTE_OPEN_RE.sub("“", result)
        result = _EN_DOUBLE_QUOTE_CLOSE_RE.sub("”", result)

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

        if '"' in original and "«" in fixed:
            fixes.append("fixed quotation marks to «»")

        if '"' in original and "“" in fixed:
            fixes.append("fixed quotation marks to “”")

        if "'" in original and "’" in fixed:
            fixes.append("fixed apostrophes to curly form")

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
