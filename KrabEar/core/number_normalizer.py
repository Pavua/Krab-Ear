"""Нормализация числительных в транскрибированном тексте.

NumberNormalizer конвертирует словесные числительные в цифровую форму:
    «сто двадцать три» → «123»
    «первый» → «1-й»
    «тридцать процентов» → «30%»

Поддерживаемые языки: ru, es, en.
Принцип: эвристический lookup-table + regex без тяжёлых NLP библиотек.
Идемпотентность: повторное применение не ломает уже-нормализованный текст.

Запуск тестов:
    PYTHONPATH=$(pwd)/KrabEar python -m pytest KrabEar/tests/test_normalizers.py -v
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("KrabEar.NumberNormalizer")

# ---------------------------------------------------------------------------
# Русские числительные
# ---------------------------------------------------------------------------

_RU_ONES: Dict[str, int] = {
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1, "одно": 1,
    "два": 2, "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
}

_RU_TENS: Dict[str, int] = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}

_RU_HUNDREDS: Dict[str, int] = {
    "сто": 100,
    "двести": 200,
    "триста": 300,
    "четыреста": 400,
    "пятьсот": 500,
    "шестьсот": 600,
    "семьсот": 700,
    "восемьсот": 800,
    "девятьсот": 900,
}

_RU_MULTIPLIERS: Dict[str, int] = {
    "тысяча": 1_000, "тысячи": 1_000, "тысяч": 1_000,
    "миллион": 1_000_000, "миллиона": 1_000_000, "миллионов": 1_000_000,
    "миллиард": 1_000_000_000, "миллиарда": 1_000_000_000, "миллиардов": 1_000_000_000,
}

# Порядковые прилагательные → суффиксы для склонений
_RU_ORDINAL_SUFFIXES: Dict[str, Tuple[str, int]] = {
    "первый": ("-й", 1), "первая": ("-я", 1), "первое": ("-е", 1), "первого": ("-го", 1),
    "первом": ("-м", 1), "первому": ("-му", 1), "первой": ("-й", 1),
    "второй": ("-й", 2), "вторая": ("-я", 2), "второе": ("-е", 2), "второго": ("-го", 2),
    "втором": ("-м", 2), "второму": ("-му", 2),
    "третий": ("-й", 3), "третья": ("-я", 3), "третье": ("-е", 3), "третьего": ("-го", 3),
    "третьем": ("-м", 3), "третьему": ("-му", 3), "третьей": ("-й", 3),
    "четвёртый": ("-й", 4), "четвертый": ("-й", 4), "четвёртого": ("-го", 4),
    "пятый": ("-й", 5), "пятая": ("-я", 5), "пятого": ("-го", 5),
    "шестой": ("-й", 6), "шестого": ("-го", 6),
    "седьмой": ("-й", 7), "седьмого": ("-го", 7),
    "восьмой": ("-й", 8), "восьмого": ("-го", 8),
    "девятый": ("-й", 9), "девятого": ("-го", 9),
    "десятый": ("-й", 10), "десятого": ("-го", 10),
}

# Дроби
_RU_FRACTIONS: Dict[str, str] = {
    "половина": "1/2", "половину": "1/2", "половиной": "1/2",
    "треть": "1/3", "трети": "1/3",
    "четверть": "1/4", "четверти": "1/4",
}

# Суффиксы единиц после числительных
_RU_UNIT_WORDS: Dict[str, str] = {
    "процент": "%", "процента": "%", "процентов": "%",
    "рублей": " руб.", "рубля": " руб.", "рубль": " руб.",
    "доллар": " $", "доллара": " $", "долларов": " $",
    "евро": " €",
    "километр": " км", "километра": " км", "километров": " км",
    "килограмм": " кг", "килограмма": " кг", "килограммов": " кг",
    "метр": " м", "метра": " м", "метров": " м",
    "литр": " л", "литра": " л", "литров": " л",
    "час": " ч", "часа": " ч", "часов": " ч",
    "минута": " мин", "минуты": " мин", "минут": " мин",
    "секунда": " с", "секунды": " с", "секунд": " с",
}

# Объединённый словарь числительных RU (в порядке убывания длины — для поиска)
_RU_ALL: Dict[str, int] = {}
_RU_ALL.update(_RU_HUNDREDS)
_RU_ALL.update(_RU_TENS)
_RU_ALL.update(_RU_ONES)


# ---------------------------------------------------------------------------
# Испанские числительные
# ---------------------------------------------------------------------------

_ES_ONES: Dict[str, int] = {
    "cero": 0,
    "uno": 1, "una": 1, "un": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12,
    "trece": 13,
    "catorce": 14,
    "quince": 15,
    "dieciséis": 16, "dieciseis": 16,
    "diecisiete": 17,
    "dieciocho": 18,
    "diecinueve": 19,
    "veinte": 20,
    "veintiuno": 21, "veintiuna": 21,
    "veintidós": 22, "veintidos": 22,
    "veintitrés": 23, "veintitres": 23,
    "veinticuatro": 24,
    "veinticinco": 25,
    "veintiséis": 26, "veintiseis": 26,
    "veintisiete": 27,
    "veintiocho": 28,
    "veintinueve": 29,
}

_ES_TENS: Dict[str, int] = {
    "treinta": 30,
    "cuarenta": 40,
    "cincuenta": 50,
    "sesenta": 60,
    "setenta": 70,
    "ochenta": 80,
    "noventa": 90,
}

_ES_HUNDREDS: Dict[str, int] = {
    "cien": 100, "ciento": 100,
    "doscientos": 200, "doscientas": 200,
    "trescientos": 300, "trescientas": 300,
    "cuatrocientos": 400, "cuatrocientas": 400,
    "quinientos": 500, "quinientas": 500,
    "seiscientos": 600, "seiscientas": 600,
    "setecientos": 700, "setecientas": 700,
    "ochocientos": 800, "ochocientas": 800,
    "novecientos": 900, "novecientas": 900,
}

_ES_MULTIPLIERS: Dict[str, int] = {
    "mil": 1_000,
    "millón": 1_000_000, "millon": 1_000_000,
    "millones": 1_000_000,
    "mil millones": 1_000_000_000,
}

_ES_ALL: Dict[str, int] = {}
_ES_ALL.update(_ES_HUNDREDS)
_ES_ALL.update(_ES_TENS)
_ES_ALL.update(_ES_ONES)

_ES_UNIT_WORDS: Dict[str, str] = {
    "por ciento": "%", "porciento": "%",
    "euros": " €", "euro": " €",
    "dólares": " $", "dolares": " $", "dólar": " $", "dolar": " $",
    "kilómetros": " km", "kilometros": " km", "kilómetro": " km", "kilometro": " km",
    "kilogramos": " kg", "kilogramos": " kg", "kilogramo": " kg",
    "metros": " m", "metro": " m",
    "litros": " l", "litro": " l",
    "horas": " h", "hora": " h",
    "minutos": " min", "minuto": " min",
    "segundos": " s", "segundo": " s",
}

# Порядковые прилагательные испанского языка → суффикс + число
# Формат: слово → (суффикс_ординала, число)
# Используем º для мужского рода / ª для женского — по умолчанию º
_ES_ORDINAL_SUFFIXES: Dict[str, Tuple[str, int]] = {
    # 1
    "primero": (".º", 1), "primer": (".º", 1), "primera": (".ª", 1),
    # 2
    "segundo": (".º", 2), "segunda": (".ª", 2),
    # 3
    "tercero": (".º", 3), "tercer": (".º", 3), "tercera": (".ª", 3),
    # 4
    "cuarto": (".º", 4), "cuarta": (".ª", 4),
    # 5
    "quinto": (".º", 5), "quinta": (".ª", 5),
    # 6
    "sexto": (".º", 6), "sexta": (".ª", 6),
    # 7
    "séptimo": (".º", 7), "septimo": (".º", 7), "séptima": (".ª", 7), "septima": (".ª", 7),
    # 8
    "octavo": (".º", 8), "octava": (".ª", 8),
    # 9
    "noveno": (".º", 9), "novena": (".ª", 9),
    # 10
    "décimo": (".º", 10), "decimo": (".º", 10), "décima": (".ª", 10), "decima": (".ª", 10),
}


# ---------------------------------------------------------------------------
# Английские числительные
# ---------------------------------------------------------------------------

_EN_ONES: Dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}

_EN_TENS: Dict[str, int] = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

_EN_MULTIPLIERS: Dict[str, int] = {
    "hundred": 100,
    "thousand": 1_000,
    "million": 1_000_000,
    "billion": 1_000_000_000,
}

_EN_ALL: Dict[str, int] = {}
_EN_ALL.update(_EN_TENS)
_EN_ALL.update(_EN_ONES)

_EN_UNIT_WORDS: Dict[str, str] = {
    "percent": "%",
    "dollars": " $", "dollar": " $",
    "euros": " €", "euro": " €",
    "kilometers": " km", "kilometer": " km",
    "kilograms": " kg", "kilogram": " kg",
    "meters": " m", "meter": " m",
    "litres": " l", "litre": " l", "liters": " l", "liter": " l",
    "hours": " h", "hour": " h",
    "minutes": " min", "minute": " min",
    "seconds": " s", "second": " s",
}

_EN_ORDINAL_SUFFIXES: Dict[str, Tuple[str, int]] = {
    "first": ("st", 1), "second": ("nd", 2), "third": ("rd", 3),
    "fourth": ("th", 4), "fifth": ("th", 5), "sixth": ("th", 6),
    "seventh": ("th", 7), "eighth": ("th", 8), "ninth": ("th", 9), "tenth": ("th", 10),
}

# ---------------------------------------------------------------------------
# Числовые токены → цифра
# ---------------------------------------------------------------------------

# Словарь «отрицательное число»
_NEGATIVE_WORDS: Dict[str, Dict[str, str]] = {
    "ru": {"минус": "-"},
    "es": {"menos": "-"},
    "en": {"minus": "-", "negative": "-"},
}

# Разделитель сотен «и» в русском (двести сорок и два → 242)
_RU_AND = {"и"}
_ES_AND = {"y"}
_EN_AND = {"and"}


def _parse_number_ru(tokens: List[str]) -> Optional[int]:
    """Парсит список русских токенов-числительных в целое число."""
    result = 0
    current = 0
    i = 0
    # 🔴 Группа из ОДНИХ союзов числом не является. Союз попадает в паттерн
    # числительных ради составных форм («двадцать и пять» → 25), но одиночное
    # «и» матчилось как числовая группа, а при result=0 и пустом current
    # возвращалось 0 — фраза «работаете и есть ли места» превращалась в
    # «работаете 0 есть ли места». Замер 30.08.2026: 767 записей из 12807 (6%)
    # истории владельца испорчены так, включая живые звонки скрининга спама.
    seen_numeral = False
    while i < len(tokens):
        w = tokens[i].lower()
        if w in _RU_AND:
            i += 1
            continue
        seen_numeral = True
        if w in _RU_HUNDREDS:
            current += _RU_HUNDREDS[w]
        elif w in _RU_TENS:
            current += _RU_TENS[w]
        elif w in _RU_ONES:
            current += _RU_ONES[w]
        elif w in _RU_MULTIPLIERS:
            mult = _RU_MULTIPLIERS[w]
            if current == 0:
                current = 1
            if mult >= 1_000_000:
                result += current * mult
                current = 0
            elif mult == 1_000:
                result += current * 1_000
                current = 0
            else:
                current *= mult
        else:
            return None
        i += 1
    if not seen_numeral:
        return None
    return result + current


def _parse_number_es(tokens: List[str]) -> Optional[int]:
    """Парсит список испанских токенов-числительных в целое число."""
    result = 0
    current = 0
    i = 0
    # 🔴 Группа из ОДНИХ союзов числом не является. Союз попадает в паттерн
    # числительных ради составных форм («veinte y cinco» → 25), но одиночное
    # «y» матчилось как числовая группа, а при result=0 и пустом current
    # возвращалось 0 — фраза «abiertos hoy y si tienen» превращалась в
    # «abiertos hoy 0 si tienen». Замер 30.08.2026: 767 записей из 12807 (6%)
    # истории владельца испорчены так, включая живые звонки скрининга спама.
    seen_numeral = False
    while i < len(tokens):
        w = tokens[i].lower()
        if w in _ES_AND:
            i += 1
            continue
        seen_numeral = True
        if w in _ES_HUNDREDS:
            current += _ES_HUNDREDS[w]
        elif w in _ES_TENS:
            current += _ES_TENS[w]
        elif w in _ES_ONES:
            current += _ES_ONES[w]
        elif w in _ES_MULTIPLIERS:
            mult = _ES_MULTIPLIERS[w]
            if current == 0:
                current = 1
            if mult >= 1_000_000:
                result += current * mult
                current = 0
            elif mult == 1_000:
                result += current * 1_000
                current = 0
            else:
                current *= mult
        else:
            return None
        i += 1
    if not seen_numeral:
        return None
    return result + current


def _parse_number_en(tokens: List[str]) -> Optional[int]:
    """Парсит список английских токенов-числительных в целое число."""
    result = 0
    current = 0
    i = 0
    while i < len(tokens):
        w = tokens[i].lower()
        if w in _EN_AND:
            i += 1
            continue
        if w in _EN_TENS:
            current += _EN_TENS[w]
        elif w in _EN_ONES:
            current += _EN_ONES[w]
        elif w == "hundred":
            if current == 0:
                current = 1
            current *= 100
        elif w == "thousand":
            if current == 0:
                current = 1
            result += current * 1_000
            current = 0
        elif w == "million":
            if current == 0:
                current = 1
            result += current * 1_000_000
            current = 0
        elif w == "billion":
            if current == 0:
                current = 1
            result += current * 1_000_000_000
            current = 0
        else:
            return None
        i += 1
    return result + current


# ---------------------------------------------------------------------------
# Главный класс
# ---------------------------------------------------------------------------

class NumberNormalizer:
    """Нормализует словесные числительные в транскрибированном тексте.

    Поддерживаемые языки: ``ru``, ``es``, ``en``.

    Примеры::

        n = NumberNormalizer()
        n.normalize("сто двадцать три", "ru")        # → "123"
        n.normalize("первый", "ru")                  # → "1-й"
        n.normalize("тридцать процентов", "ru")       # → "30%"
        n.normalize("ciento veintitres", "es")        # → "123"
        n.normalize("one hundred twenty three", "en") # → "123"
    """

    # Кэш скомпилированных паттернов — ключ = язык
    _compiled: Dict[str, re.Pattern] = {}

    def normalize(self, text: str, language: str = "ru") -> str:
        """Нормализует числительные в ``text`` для заданного языка.

        Идемпотентно: если текст уже содержит цифры — они не трогаются.
        """
        lang = language.lower()[:2]
        if lang == "ru":
            return self._normalize_ru(text)
        elif lang == "es":
            return self._normalize_es(text)
        elif lang == "en":
            return self._normalize_en(text)
        else:
            return text

    # ------------------------------------------------------------------
    # Русский
    # ------------------------------------------------------------------

    def _normalize_ru(self, text: str) -> str:
        # 0. Дроби
        for frac_word, frac_val in _RU_FRACTIONS.items():
            pattern = r"(?<!\w)" + re.escape(frac_word) + r"(?!\w)"
            text = re.sub(pattern, frac_val, text, flags=re.IGNORECASE)

        # 1. Порядковые числительные (первый, второй, ...)
        text = self._replace_ordinals_ru(text)

        # 2. Количественные числительные + опциональные единицы
        text = self._replace_cardinals_ru(text)

        return text

    def _replace_ordinals_ru(self, text: str) -> str:
        for word, (suffix, value) in _RU_ORDINAL_SUFFIXES.items():
            pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
            replacement = f"{value}{suffix}"
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def _replace_cardinals_ru(self, text: str) -> str:
        # Все числительные слова — сортированы от длинных к коротким
        num_words = sorted(
            list(_RU_ALL.keys()) + list(_RU_MULTIPLIERS.keys()),
            key=len, reverse=True
        )
        unit_words = list(_RU_UNIT_WORDS.keys())

        # Строим паттерн: одно или несколько числительных слов подряд (до 20, чтобы избежать ReDoS и ValueError limit)
        word_pat = "|".join(re.escape(w) for w in num_words + ["и"])
        num_seq_pat = rf"(?<!\w)(?:(?:{word_pat})(?:\s+(?:{word_pat})){{0,20}})(?!\w)"

        # Единицы
        unit_pat = "|".join(re.escape(u) for u in sorted(unit_words, key=len, reverse=True))
        full_pat = rf"(минус\s+)?({num_seq_pat})(?:\s+({unit_pat})(?!\w))?"

        def _repl(m: re.Match) -> str:
            neg_prefix = m.group(1)
            num_str = m.group(2)
            unit_str = m.group(3)
            tokens = num_str.split()
            parsed = _parse_number_ru(tokens)
            if parsed is None:
                return m.group(0)
            sign = "-" if neg_prefix else ""
            unit_repr = ""
            if unit_str:
                ul = unit_str.lower()
                unit_repr = _RU_UNIT_WORDS.get(ul, f" {unit_str}")
            return f"{sign}{parsed}{unit_repr}"

        return re.sub(full_pat, _repl, text, flags=re.IGNORECASE)

    # ------------------------------------------------------------------
    # Испанский
    # ------------------------------------------------------------------

    def _normalize_es(self, text: str) -> str:
        # 1. Порядковые числительные (primero, segundo, tercero, ...)
        text = self._replace_ordinals_es(text)
        # 2. Количественные числительные + опциональные единицы
        text = self._replace_cardinals_es(text)
        return text

    def _replace_ordinals_es(self, text: str) -> str:
        for word, (suffix, value) in _ES_ORDINAL_SUFFIXES.items():
            pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
            replacement = f"{value}{suffix}"
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def _replace_cardinals_es(self, text: str) -> str:
        num_words = sorted(
            list(_ES_ALL.keys()) + list(_ES_MULTIPLIERS.keys()),
            key=len, reverse=True
        )
        unit_words = list(_ES_UNIT_WORDS.keys())

        word_pat = "|".join(re.escape(w) for w in num_words + ["y"])
        num_seq_pat = rf"(?<!\w)(?:(?:{word_pat})(?:\s+(?:{word_pat})){{0,20}})(?!\w)"
        unit_pat = "|".join(re.escape(u) for u in sorted(unit_words, key=len, reverse=True))
        full_pat = rf"(menos\s+)?({num_seq_pat})(?:\s+({unit_pat})(?!\w))?"

        def _repl(m: re.Match) -> str:
            neg_prefix = m.group(1)
            num_str = m.group(2)
            unit_str = m.group(3)
            tokens = num_str.split()
            parsed = _parse_number_es(tokens)
            if parsed is None:
                return m.group(0)
            sign = "-" if neg_prefix else ""
            unit_repr = ""
            if unit_str:
                ul = unit_str.lower()
                unit_repr = _ES_UNIT_WORDS.get(ul, f" {unit_str}")
            return f"{sign}{parsed}{unit_repr}"

        return re.sub(full_pat, _repl, text, flags=re.IGNORECASE)

    # ------------------------------------------------------------------
    # Английский
    # ------------------------------------------------------------------

    def _normalize_en(self, text: str) -> str:
        # Порядковые
        text = self._replace_ordinals_en(text)
        # Количественные
        text = self._replace_cardinals_en(text)
        return text

    def _replace_ordinals_en(self, text: str) -> str:
        for word, (suffix, value) in _EN_ORDINAL_SUFFIXES.items():
            pattern = r"(?<!\w)" + re.escape(word) + r"(?!\w)"
            replacement = f"{value}{suffix}"
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
        return text

    def _replace_cardinals_en(self, text: str) -> str:
        num_words = sorted(
            list(_EN_ALL.keys()) + list(_EN_MULTIPLIERS.keys()),
            key=len, reverse=True
        )
        unit_words = list(_EN_UNIT_WORDS.keys())

        word_pat = "|".join(re.escape(w) for w in num_words + ["and"])
        num_seq_pat = rf"(?<!\w)(?:(?:{word_pat})(?:[\s-]+(?:{word_pat})){{0,20}})(?!\w)"
        unit_pat = "|".join(re.escape(u) for u in sorted(unit_words, key=len, reverse=True))
        full_pat = rf"(minus\s+|negative\s+)?({num_seq_pat})(?:\s+({unit_pat})(?!\w))?"

        def _repl(m: re.Match) -> str:
            neg_prefix = m.group(1)
            num_str = m.group(2)
            unit_str = m.group(3)
            # Нормализуем дефисы (twenty-three → twenty three)
            tokens = re.split(r"[\s-]+", num_str)
            # Убираем 'and' токены
            tokens = [t for t in tokens if t.lower() != "and"]
            parsed = _parse_number_en(tokens)
            if parsed is None:
                return m.group(0)
            sign = "-" if neg_prefix else ""
            unit_repr = ""
            if unit_str:
                ul = unit_str.lower()
                unit_repr = _EN_UNIT_WORDS.get(ul, f" {unit_str}")
            return f"{sign}{parsed}{unit_repr}"

        return re.sub(full_pat, _repl, text, flags=re.IGNORECASE)
