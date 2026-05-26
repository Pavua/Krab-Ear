"""Лёгкий эвристический детектор языка для Krab Ear.

Не требует внешних зависимостей: работает на анализе символов Unicode.
Поддерживаемые языки: ru, uk, es, en (+ fallback «und» — undetermined).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Публичные типы
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LanguageResult:
    """Результат определения языка."""

    language: str   # ISO 639-1: «ru», «uk», «es», «en», «und»
    confidence: float  # 0.0 – 1.0
    script: str     # «cyrillic», «latin», «mixed», «unknown»


# ---------------------------------------------------------------------------
# Константы — символы-маркеры
# ---------------------------------------------------------------------------

# Кириллические буквы, характерные только для украинского
_UK_ONLY_CHARS = frozenset("іїєґІЇЄҐ")

# Символы, характерные для испанского (не встречаются в базовом ASCII-EN)
_ES_MARKERS = frozenset("ñáéíóúüÑÁÉÍÓÚÜ¿¡")

# Символы-маркеры французского — ç и œ уникальны; «»  типичны
_FR_MARKERS = frozenset("çœÇŒ«»")
# Слова-маркеры французского (нижний регистр)
_FR_WORD_MARKERS = frozenset({"c'est", "très", "être", "mais", "avec", "dans",
                               "pour", "sur", "les", "des", "une", "est"})

# Символы-маркеры турецкого — ş, ğ, İ, ı уникальны для TR
_TR_MARKERS = frozenset("şŞğĞıİ")

# Символы-маркеры португальского — ã, õ уникальны; ç общий с FR/TR
_PT_MARKERS = frozenset("ãõÃÕ")
# Слова-маркеры португальского (нижний регистр)
_PT_WORD_MARKERS = frozenset({"não", "você", "então", "também", "isso",
                               "para", "com", "uma", "que", "são"})

# Минимальная доля маркерных букв от общего числа букв для классификации «es»
_ES_DENSITY_THRESHOLD = 0.02  # 2 %
# Минимальная длина текста (символов) для применения порога плотности
_ES_MIN_LEN_FOR_DENSITY = 10

# Базовые латинские диапазоны (ASCII a-z / A-Z)
_LATIN_RANGE = (0x0041, 0x005A, 0x0061, 0x007A)
# Дополнительные латинские блоки (Latin-1 Supplement, Latin Extended-A/B …)
_LATIN_EXTENDED_START = 0x00C0
_LATIN_EXTENDED_END = 0x024F

# Кирилличный блок Unicode
_CYRILLIC_START = 0x0400
_CYRILLIC_END = 0x04FF


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _classify_char(ch: str) -> str:
    """Возвращает «cyrillic», «latin» или «other» для одного символа."""
    cp = ord(ch)
    if _CYRILLIC_START <= cp <= _CYRILLIC_END:
        return "cyrillic"
    if (
        (_LATIN_RANGE[0] <= cp <= _LATIN_RANGE[1])
        or (_LATIN_RANGE[2] <= cp <= _LATIN_RANGE[3])
        or (_LATIN_EXTENDED_START <= cp <= _LATIN_EXTENDED_END)
    ):
        return "latin"
    return "other"


def _count_scripts(text: str) -> tuple[int, int]:
    """Возвращает (кирилличных_букв, латинских_букв) в тексте."""
    cyrillic = 0
    latin = 0
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith("L"):  # только буквы
            kind = _classify_char(ch)
            if kind == "cyrillic":
                cyrillic += 1
            elif kind == "latin":
                latin += 1
    return cyrillic, latin


# ---------------------------------------------------------------------------
# Основной класс
# ---------------------------------------------------------------------------

class LanguageDetector:
    """Эвристический детектор языка без внешних зависимостей."""

    # Минимальное число букв для уверенного определения
    _MIN_LETTERS = 3

    def detect(self, text: str) -> LanguageResult:
        """Определяет язык строки *text*.

        Алгоритм:
        1. Подсчитываем кириллические и латинские буквы.
        2. По доминирующему скрипту выбираем ветку.
        3. Внутри кирилличной ветки проверяем украинские маркеры.
        4. Внутри латинской ветки проверяем испанские маркеры.
        5. Confidence = (буквы_доминирующего_скрипта) / (все_буквы).
        """
        stripped = text.strip()
        if not stripped:
            return LanguageResult(language="und", confidence=0.0, script="unknown")

        cyrillic, latin = _count_scripts(stripped)
        total = cyrillic + latin

        if total == 0:
            # Цифры, знаки препинания — неопределённо
            return LanguageResult(language="und", confidence=0.0, script="unknown")

        if total < self._MIN_LETTERS:
            # Слишком короткий текст — низкая уверенность
            if cyrillic >= latin:
                lang, script = self._detect_cyrillic(stripped), "cyrillic"
            else:
                lang, script = self._detect_latin(stripped), "latin"
            return LanguageResult(language=lang, confidence=0.4, script=script)

        if cyrillic == 0 and latin == 0:
            return LanguageResult(language="und", confidence=0.0, script="unknown")

        # Определяем доминирующий скрипт
        if cyrillic > latin:
            script = "cyrillic" if latin == 0 else "mixed"
            lang = self._detect_cyrillic(stripped)
            confidence = round(cyrillic / total, 3)
        elif latin > cyrillic:
            script = "latin" if cyrillic == 0 else "mixed"
            lang = self._detect_latin(stripped)
            confidence = round(latin / total, 3)
        else:
            # Точно 50/50 — смешанный
            script = "mixed"
            lang = self._detect_latin(stripped)  # латиница приоритетнее при ничьей
            confidence = round(0.5, 3)

        return LanguageResult(language=lang, confidence=confidence, script=script)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_cyrillic(text: str) -> str:
        """Различает русский и украинский по характерным символам."""
        for ch in text:
            if ch in _UK_ONLY_CHARS:
                return "uk"
        return "ru"

    @staticmethod
    def _detect_latin(text: str) -> str:
        """Различает испанский и английский по характерным символам.

        Алгоритм (W1008 F1 fix):
        1. Проверяем маркеры FR/TR/PT → возвращаем «und» при обнаружении.
        2. Считаем испанские маркеры и применяем порог плотности (≥2% букв
           при длине ≥10 символов), чтобы единственный акцентированный символ
           не давал ложного срабатывания.
        3. Если маркеры есть и порог пройден → «es»; иначе → «en».
        """
        text_lower = text.lower()

        # --- Guard 1: исключение FR ---
        for ch in text:
            if ch in _FR_MARKERS:
                return "und"
        words = set(text_lower.split())
        if words & _FR_WORD_MARKERS:
            return "und"

        # --- Guard 2: исключение TR ---
        for ch in text:
            if ch in _TR_MARKERS:
                return "und"

        # --- Guard 3: исключение PT ---
        for ch in text:
            if ch in _PT_MARKERS:
                return "und"
        if words & _PT_WORD_MARKERS:
            return "und"

        # --- Классификация ES с порогом плотности ---
        marker_count = sum(1 for ch in text if ch in _ES_MARKERS)
        if marker_count == 0:
            return "en"

        # Подсчитываем буквы для порога
        letter_count = sum(1 for ch in text if unicodedata.category(ch).startswith("L"))

        # Для коротких текстов порог не применяем — достаточно одного маркера
        if len(text) < _ES_MIN_LEN_FOR_DENSITY:
            return "es"

        # Для длинных текстов требуем минимальную плотность маркеров
        if letter_count > 0 and (marker_count / letter_count) >= _ES_DENSITY_THRESHOLD:
            return "es"

        return "en"

    def detect_batch(self, texts: list[str]) -> list[LanguageResult]:
        """Определяет языки для списка строк.

        Порядок результатов соответствует порядку входных строк.
        """
        return [self.detect(t) for t in texts]
