"""Нормализация дат и времени в транскрибированном тексте.

DateTimeNormalizer конвертирует словесные даты и время в цифровую форму:
    «третье ноября» → «03.11» (european, default)
    «девять часов утра» → «09:00»
    «пятнадцатого января две тысячи двадцать шестого года» → «15.01.2026»

Поддерживаемые языки: ru, es, en.
Принцип: эвристический lookup-table + regex без тяжёлых NLP библиотек.
Идемпотентность: уже нормализованные строки «03.11», «09:00» не трогаются.

Формат вывода дат управляется модульной константой ``DATETIME_OUTPUT_FORMAT``:

    * ``"european"`` (default) — ``DD.MM.YYYY`` / ``DD.MM`` — формат,
      соответствующий тест-спеке и ожиданиям UI.
    * ``"iso8601"`` — ``YYYY-MM-DD`` для полных дат, ``MM-DD`` для дат без
      года. Подходит для лексикографической сортировки и RFC-3339 парсинга.

Переопределить глобально::

    import core.datetime_normalizer as dn
    dn.DATETIME_OUTPUT_FORMAT = "iso8601"

Переопределить на уровне экземпляра::

    normalizer = DateTimeNormalizer(output_format="iso8601")

Запуск тестов:
    PYTHONPATH=$(pwd)/KrabEar python -m unittest KrabEar/tests/test_datetime_normalizer -v
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Literal, Optional

# ---------------------------------------------------------------------------
# Формат вывода дат.  Значение по умолчанию — European (DD.MM.YYYY).
# Установите "iso8601" для YYYY-MM-DD поведения.
# ---------------------------------------------------------------------------
DATETIME_OUTPUT_FORMAT: Literal["iso8601", "european"] = "european"

logger = logging.getLogger("KrabEar.DateTimeNormalizer")

# ---------------------------------------------------------------------------
# Русские месяцы (именительный + родительный + датный падежи)
# ---------------------------------------------------------------------------

_RU_MONTHS: Dict[str, int] = {
    # Январь
    "январь": 1, "января": 1, "январе": 1, "январю": 1,
    # Февраль
    "февраль": 2, "февраля": 2, "феврале": 2, "февралю": 2,
    # Март
    "март": 3, "марта": 3, "марте": 3, "марту": 3,
    # Апрель
    "апрель": 4, "апреля": 4, "апреле": 4, "апрелю": 4,
    # Май
    "май": 5, "мая": 5, "мае": 5, "маю": 5,
    # Июнь
    "июнь": 6, "июня": 6, "июне": 6, "июню": 6,
    # Июль
    "июль": 7, "июля": 7, "июле": 7, "июлю": 7,
    # Август
    "август": 8, "августа": 8, "августе": 8, "августу": 8,
    # Сентябрь
    "сентябрь": 9, "сентября": 9, "сентябре": 9, "сентябрю": 9,
    # Октябрь
    "октябрь": 10, "октября": 10, "октябре": 10, "октябрю": 10,
    # Ноябрь
    "ноябрь": 11, "ноября": 11, "ноябре": 11, "ноябрю": 11,
    # Декабрь
    "декабрь": 12, "декабря": 12, "декабре": 12, "декабрю": 12,
}

# Порядковые числительные для дней (именительный + родительный)
_RU_DAY_ORDINALS: Dict[str, int] = {
    "первое": 1, "первого": 1, "первому": 1,
    "второе": 2, "второго": 2, "второму": 2,
    "третье": 3, "третьего": 3, "третьему": 3,
    "четвёртое": 4, "четвертое": 4, "четвёртого": 4, "четвертого": 4,
    "пятое": 5, "пятого": 5,
    "шестое": 6, "шестого": 6,
    "седьмое": 7, "седьмого": 7,
    "восьмое": 8, "восьмого": 8,
    "девятое": 9, "девятого": 9,
    "десятое": 10, "десятого": 10,
    "одиннадцатое": 11, "одиннадцатого": 11,
    "двенадцатое": 12, "двенадцатого": 12,
    "тринадцатое": 13, "тринадцатого": 13,
    "четырнадцатое": 14, "четырнадцатого": 14,
    "пятнадцатое": 15, "пятнадцатого": 15,
    "шестнадцатое": 16, "шестнадцатого": 16,
    "семнадцатое": 17, "семнадцатого": 17,
    "восемнадцатое": 18, "восемнадцатого": 18,
    "девятнадцатое": 19, "девятнадцатого": 19,
    "двадцатое": 20, "двадцатого": 20,
    "двадцать первое": 21, "двадцать первого": 21,
    "двадцать второе": 22, "двадцать второго": 22,
    "двадцать третье": 23, "двадцать третьего": 23,
    "двадцать четвёртое": 24, "двадцать четвертое": 24, "двадцать четвёртого": 24, "двадцать четвертого": 24,
    "двадцать пятое": 25, "двадцать пятого": 25,
    "двадцать шестое": 26, "двадцать шестого": 26,
    "двадцать седьмое": 27, "двадцать седьмого": 27,
    "двадцать восьмое": 28, "двадцать восьмого": 28,
    "двадцать девятое": 29, "двадцать девятого": 29,
    "тридцатое": 30, "тридцатого": 30,
    "тридцать первое": 31, "тридцать первого": 31,
}

# Год словами: «две тысячи двадцать шестого года»
_RU_YEAR_THOUSANDS: Dict[str, int] = {
    "две тысячи": 2000, "двух тысяч": 2000,
    "тысяча девятьсот": 1900,
    "тысяча восемьсот": 1800,
}

_RU_YEAR_DECADES: Dict[str, int] = {
    "десятого": 10, "десятый": 10,
    "двадцатого": 20, "двадцать": 20,
    "тридцатого": 30, "тридцать": 30,
    "сорокового": 40, "сорок": 40,
    "пятидесятого": 50, "пятьдесят": 50,
    "шестидесятого": 60, "шестьдесят": 60,
    "семидесятого": 70, "семьдесят": 70,
    "восьмидесятого": 80, "восемьдесят": 80,
    "девяностого": 90, "девяносто": 90,
}

_RU_YEAR_ONES_ORDINAL: Dict[str, int] = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
    "нулевого": 0, "нулевой": 0,
}

# Временные маркеры
_RU_TIME_MARKERS: Dict[str, str] = {
    "утра": "am", "утром": "am",
    "дня": "pm_mild",  # 12–17
    "вечера": "pm", "вечером": "pm",
    "ночи": "night",  # 0–5 считаем ночью
    "полдень": "noon", "полуночи": "midnight", "полночь": "midnight",
}

# ---------------------------------------------------------------------------
# Испанские месяцы
# ---------------------------------------------------------------------------

_ES_MONTHS: Dict[str, int] = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12, "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "jun": 6, "jul": 7, "ago": 8,
}

_ES_DAY_ORDINALS: Dict[str, int] = {
    "primero": 1, "segundo": 2, "tercero": 3, "cuarto": 4, "quinto": 5,
    "sexto": 6, "séptimo": 7, "septimo": 7, "octavo": 8, "noveno": 9, "décimo": 10,
    "decimo": 10,
}

_ES_TIME_MARKERS: Dict[str, str] = {
    "de la mañana": "am", "mañana": "am",
    "de la tarde": "pm", "tarde": "pm",
    "de la noche": "night", "noche": "night",
    "mediodía": "noon", "mediodia": "noon", "medianoche": "midnight",
}

# ---------------------------------------------------------------------------
# Английские месяцы
# ---------------------------------------------------------------------------

_EN_MONTHS: Dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_EN_DAY_ORDINALS: Dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20,
    "twenty-first": 21, "twenty-second": 22, "twenty-third": 23, "twenty-fourth": 24,
    "twenty-fifth": 25, "twenty-sixth": 26, "twenty-seventh": 27, "twenty-eighth": 28,
    "twenty-ninth": 29, "thirtieth": 30, "thirty-first": 31,
}

_EN_TIME_MARKERS: Dict[str, str] = {
    "in the morning": "am", "morning": "am",
    "in the afternoon": "pm", "afternoon": "pm",
    "in the evening": "pm_mild", "evening": "pm_mild",
    "at night": "night", "night": "night",
    "noon": "noon", "midnight": "midnight",
    "a.m.": "am", "am": "am", "p.m.": "pm", "pm": "pm",
}

# ---------------------------------------------------------------------------
# Числа в словах для часов/минут — используем отдельные таблицы
# ---------------------------------------------------------------------------

_RU_HOUR_WORDS: Dict[str, int] = {
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1,
    "два": 2, "две": 2,
    "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
    "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "двадцать один": 21, "двадцать одна": 21, "двадцать два": 22,
    "двадцать три": 23,
}

_RU_MINUTE_WORDS: Dict[str, int] = dict(_RU_HOUR_WORDS)  # те же слова
_RU_MINUTE_WORDS.update({
    "тридцать": 30, "сорок": 40, "пятьдесят": 50, "сорок пять": 45,
    "пятнадцать": 15,
})

_EN_HOUR_WORDS: Dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_ES_HOUR_WORDS: Dict[str, int] = {
    "cero": 0, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9,
    "diez": 10, "once": 11, "doce": 12,
}


def _apply_time_marker(hour: int, marker: str) -> int:
    """Применяет маркер времени к часу."""
    if marker == "am":
        if hour == 12:
            return 0
        return hour
    elif marker in ("pm", "pm_mild"):
        if hour < 12:
            return hour + 12
        return hour
    elif marker == "night":
        if hour < 6:
            return hour
        if hour < 12:
            # e.g. «восемь часов ночи» → 08 + 12 = 20:00
            return hour + 12
        return hour
    elif marker == "noon":
        return 12
    elif marker == "midnight":
        return 0
    return hour


# ---------------------------------------------------------------------------
# Главный класс
# ---------------------------------------------------------------------------

class DateTimeNormalizer:
    """Нормализует словесные даты и время в транскрибированном тексте.

    Поддерживаемые языки: ``ru``, ``es``, ``en``.

    По умолчанию использует ISO-8601 (``YYYY-MM-DD``) для вывода дат.
    Для обратной совместимости передайте ``output_format="european"``.

    Примеры::

        d = DateTimeNormalizer()
        d.normalize("третье ноября", "ru")                              # → "11-03"
        d.normalize("девять часов утра", "ru")                          # → "09:00"
        d.normalize("пятнадцатого января две тысячи двадцать шестого", "ru")
        # → "2026-01-15"

        d_legacy = DateTimeNormalizer(output_format="european")
        d_legacy.normalize("третье ноября", "ru")                       # → "03.11"
    """

    def __init__(
        self,
        output_format: Optional[Literal["iso8601", "european"]] = None,
    ) -> None:
        """Инициализирует нормализатор.

        Args:
            output_format: ``"iso8601"`` (default) или ``"european"``.
                           Если ``None`` — берётся из модульной константы
                           :data:`DATETIME_OUTPUT_FORMAT`.
        """
        self._output_format: str = output_format or DATETIME_OUTPUT_FORMAT

    # ------------------------------------------------------------------
    # Вспомогательный метод форматирования даты
    # ------------------------------------------------------------------

    def _fmt_date(self, day: int, month: int, year: Optional[int] = None) -> str:
        """Форматирует дату в соответствии с ``_output_format``.

        Args:
            day: День (1–31).
            month: Месяц (1–12).
            year: Год (4 цифры) или ``None`` для дат без года.

        Returns:
            Строка даты: ``YYYY-MM-DD`` / ``MM-DD`` (iso8601) или
            ``DD.MM.YYYY`` / ``DD.MM`` (european).
        """
        if self._output_format == "european":
            if year is not None:
                return f"{day:02d}.{month:02d}.{year}"
            return f"{day:02d}.{month:02d}"
        # iso8601
        if year is not None:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return f"{month:02d}-{day:02d}"

    def normalize(self, text: str, language: str = "ru") -> str:
        """Нормализует даты и время в ``text`` для заданного языка."""
        lang = language.lower()[:2]
        if lang == "ru":
            text = self._normalize_time_ru(text)
            text = self._normalize_date_ru(text)
        elif lang == "es":
            text = self._normalize_time_es(text)
            text = self._normalize_date_es(text)
        elif lang == "en":
            text = self._normalize_time_en(text)
            text = self._normalize_date_en(text)
        return text

    # ------------------------------------------------------------------
    # Русский: ВРЕМЯ
    # ------------------------------------------------------------------

    def _normalize_time_ru(self, text: str) -> str:
        """«девять часов утра» → «09:00», «в два часа тридцать минут» → «02:30»."""

        # Паттерн: <час> часов|час [<минуты> минут] [<маркер>]
        time_markers_pat = "|".join(
            re.escape(m) for m in sorted(_RU_TIME_MARKERS.keys(), key=len, reverse=True)
        )
        hour_words_pat = "|".join(
            re.escape(h) for h in sorted(_RU_HOUR_WORDS.keys(), key=len, reverse=True)
        )
        minute_words_pat = "|".join(
            re.escape(m) for m in sorted(_RU_MINUTE_WORDS.keys(), key=len, reverse=True)
        )

        # Паттерн с часами (число-слово или цифра)
        hour_pat = rf"(?:{hour_words_pat}|\d{{1,2}})"
        minute_pat = rf"(?:{minute_words_pat}|\d{{1,2}})"
        marker_pat = rf"(?:\s+(?:{time_markers_pat}))?"

        full_pat = (
            rf"(?<!\d)(?:в\s+)?({hour_pat})\s+"
            rf"(?:час(?:ов|а)?)"
            rf"(?:\s+({minute_pat})\s+минут(?:ы)?)?"
            rf"({marker_pat})"
        )

        def _repl_time(m: re.Match) -> str:
            hour_str = m.group(1).strip().lower()
            min_str = (m.group(2) or "").strip().lower()
            marker_raw = (m.group(3) or "").strip().lower()

            hour = _RU_HOUR_WORDS.get(hour_str)
            if hour is None:
                try:
                    hour = int(hour_str)
                except ValueError:
                    return m.group(0)

            minute = 0
            if min_str:
                minute = _RU_MINUTE_WORDS.get(min_str)
                if minute is None:
                    try:
                        minute = int(min_str)
                    except ValueError:
                        minute = 0

            # Ищем маркер в конце
            marker_key = None
            for mk in sorted(_RU_TIME_MARKERS.keys(), key=len, reverse=True):
                if mk in marker_raw:
                    marker_key = _RU_TIME_MARKERS[mk]
                    break

            if marker_key:
                hour = _apply_time_marker(hour, marker_key)

            return f"{hour:02d}:{minute:02d}"

        return re.sub(full_pat, _repl_time, text, flags=re.IGNORECASE)

    # ------------------------------------------------------------------
    # Русский: ДАТА
    # ------------------------------------------------------------------

    def _normalize_date_ru(self, text: str) -> str:
        """«третье ноября» → «03.11», «15 января 2026 года» → «15.01.2026»."""

        months_pat = "|".join(
            re.escape(m) for m in sorted(_RU_MONTHS.keys(), key=len, reverse=True)
        )

        # Порядковые двухсловные сначала
        day_ordinals_sorted = sorted(_RU_DAY_ORDINALS.keys(), key=len, reverse=True)
        day_ordinals_pat = "|".join(re.escape(d) for d in day_ordinals_sorted)

        # Год: «две тысячи двадцать шестого года» или «2026 года»
        year_words_pat = (
            r"(?:"
            r"двух?\s+тысяч(?:и|ного)?\s+(?:\w+\s+)*?\w+ого\s+года?"
            r"|тысяча\s+\w+\s+\w+\s+года?"
            r"|\d{4}\s+года?"
            r")?"
        )

        full_pat = (
            rf"(?<!\d)({day_ordinals_pat})"
            rf"\s+({months_pat})(?!\w)"
            rf"(?:\s+({year_words_pat}))?"
        )

        def _repl_date(m: re.Match) -> str:
            day_str = m.group(1).strip().lower()
            month_str = m.group(2).strip().lower()
            year_part = (m.group(3) or "").strip()

            day = _RU_DAY_ORDINALS.get(day_str)
            if day is None:
                return m.group(0)
            month = _RU_MONTHS.get(month_str)
            if month is None:
                return m.group(0)

            year = self._parse_year_ru(year_part)
            if year:
                return self._fmt_date(day, month, year)
            return self._fmt_date(day, month)

        text = re.sub(full_pat, _repl_date, text, flags=re.IGNORECASE)

        # Цифровой день + месяц словом
        digit_day_pat = (
            rf"(?<!\d)(\d{{1,2}})"
            rf"\s+({months_pat})(?!\w)"
            rf"(?:\s+(\d{{4}})\s+года?)?"
        )

        def _repl_digit_date(m: re.Match) -> str:
            day = int(m.group(1))
            month_str = m.group(2).lower()
            year_str = m.group(3) or ""
            if not (1 <= day <= 31):
                return m.group(0)
            month = _RU_MONTHS.get(month_str)
            if month is None:
                return m.group(0)
            if year_str:
                return self._fmt_date(day, month, int(year_str))
            return self._fmt_date(day, month)

        text = re.sub(digit_day_pat, _repl_digit_date, text, flags=re.IGNORECASE)
        return text

    def _parse_year_ru(self, year_text: str) -> Optional[int]:
        """Парсит год из русских словесных форм."""
        if not year_text:
            return None

        # Цифровой год
        m = re.search(r"\b(\d{4})\b", year_text)
        if m:
            return int(m.group(1))

        yt = year_text.lower()

        # «две тысячи двадцать шестого года»
        base_match = re.match(
            r"две\s+тысячи\s*(.*?)(?:\s+года?)?$", yt
        )
        if base_match:
            rest = base_match.group(1).strip()
            decade = 0
            ones = 0
            for dk, dv in _RU_YEAR_DECADES.items():
                if dk in rest:
                    decade = dv
                    rest = rest.replace(dk, "").strip()
                    break
            for ok, ov in _RU_YEAR_ONES_ORDINAL.items():
                if ok in rest:
                    ones = ov
                    break
            return 2000 + decade + ones

        # «тысяча девятьсот...»
        base_match2 = re.match(
            r"тысяча\s+(девятьсот|восемьсот|семьсот|шестьсот|пятьсот)\s*(.*?)(?:\s+года?)?$",
            yt
        )
        if base_match2:
            century_map = {
                "девятьсот": 900, "восемьсот": 800, "семьсот": 700,
                "шестьсот": 600, "пятьсот": 500,
            }
            base_year = 1000 + century_map.get(base_match2.group(1), 0)
            rest = base_match2.group(2).strip()
            decade = 0
            ones = 0
            for dk, dv in _RU_YEAR_DECADES.items():
                if dk in rest:
                    decade = dv
                    rest = rest.replace(dk, "").strip()
                    break
            for ok, ov in _RU_YEAR_ONES_ORDINAL.items():
                if ok in rest:
                    ones = ov
                    break
            return base_year + decade + ones

        return None

    # ------------------------------------------------------------------
    # Испанский: ВРЕМЯ
    # ------------------------------------------------------------------

    def _normalize_time_es(self, text: str) -> str:
        """«nueve de la mañana» → «09:00», «a las 9 de la mañana» → «09:00»"""
        hour_words_pat = "|".join(
            re.escape(h) for h in sorted(_ES_HOUR_WORDS.keys(), key=len, reverse=True)
        )
        markers_pat = "|".join(
            re.escape(mk) for mk in sorted(_ES_TIME_MARKERS.keys(), key=len, reverse=True)
        )

        # Pattern 1: word-hour + REQUIRED anchor (half, time marker, or «las» prefix).
        # Bare «una persona» / «dos cosas» must NOT be converted.
        # Anchors: «las/la» prefix, «y media/cuarto», or a time-of-day marker phrase.
        word_hour_pat = (
            rf"(?:(?:las?\s+)({hour_words_pat})"  # «las diez», «la una» (prefixed)
            rf"(?:\s+y\s+(\w+)(?:\s+({markers_pat}))?)?(?:\s+({markers_pat}))?)"
            rf"|(?:(?<!\w)({hour_words_pat})"  # un-prefixed word-hour with mandatory anchor
            rf"(?:"
            rf"(?:\s+y\s+(\w+)(?:\s+({markers_pat}))?)"  # y media/cuarto [marker]
            rf"|(?:\s+({markers_pat}))"  # standalone marker
            rf")(?!\w))"
        )

        # Pattern 2: "a las N" (requires «a las» prefix) → digit hour
        alas_pat = (
            rf"(?<!\d)(?:a\s+las?\s+)(\d{{1,2}})"
            rf"(?:\s+y\s+(\w+))?"
            rf"(?:\s+({markers_pat}))?"
        )

        def _repl_word_time(m: re.Match) -> str:
            # Groups for word_hour_pat (two alternation branches, 8 groups):
            #   Branch A (las-prefixed): 1=hour, 2=half, 3=marker-after-half, 4=trailing-marker
            #   Branch B (un-prefixed):  5=hour, 6=half, 7=marker-after-half, 8=standalone-marker
            hour_str = (m.group(1) or m.group(5) or "").strip().lower()
            half_str = (m.group(2) or m.group(6) or "").strip().lower()
            marker_raw = (
                m.group(3) or m.group(4) or m.group(7) or m.group(8) or ""
            ).strip().lower()

            hour = _ES_HOUR_WORDS.get(hour_str)
            if hour is None:
                try:
                    hour = int(hour_str)
                except ValueError:
                    return m.group(0)

            minute = 0
            if half_str == "media":
                minute = 30
            elif half_str == "cuarto":
                minute = 15

            marker_key = None
            for mk in sorted(_ES_TIME_MARKERS.keys(), key=len, reverse=True):
                if mk in marker_raw:
                    marker_key = _ES_TIME_MARKERS[mk]
                    break

            if marker_key:
                hour = _apply_time_marker(hour, marker_key)

            return f"{hour:02d}:{minute:02d}"

        def _repl_alas_time(m: re.Match) -> str:
            # Groups for alas_pat (3 groups): 1: digit hour, 2: half, 3: marker
            hour_str = m.group(1).strip().lower()
            half_str = (m.group(2) or "").strip().lower()
            marker_raw = (m.group(3) or "").strip().lower()

            try:
                hour = int(hour_str)
            except ValueError:
                return m.group(0)

            minute = 0
            if half_str == "media":
                minute = 30
            elif half_str == "cuarto":
                minute = 15

            marker_key = None
            for mk in sorted(_ES_TIME_MARKERS.keys(), key=len, reverse=True):
                if mk in marker_raw:
                    marker_key = _ES_TIME_MARKERS[mk]
                    break

            if marker_key:
                hour = _apply_time_marker(hour, marker_key)

            return f"{hour:02d}:{minute:02d}"

        text = re.sub(word_hour_pat, _repl_word_time, text, flags=re.IGNORECASE)
        text = re.sub(alas_pat, _repl_alas_time, text, flags=re.IGNORECASE)
        return text

    def _normalize_date_es(self, text: str) -> str:
        """«tres de noviembre» → «03.11»"""
        months_pat = "|".join(
            re.escape(m) for m in sorted(_ES_MONTHS.keys(), key=len, reverse=True)
        )
        day_ordinals_pat = "|".join(
            re.escape(d) for d in sorted(_ES_DAY_ORDINALS.keys(), key=len, reverse=True)
        )

        # «el tres de noviembre de dos mil veintiséis»
        full_pat = (
            rf"(?:el\s+)?(\d{{1,2}}|{day_ordinals_pat})"
            rf"\s+de\s+({months_pat})"
            rf"(?:\s+de\s+(\d{{4}}))?"
        )

        def _repl_date(m: re.Match) -> str:
            day_str = m.group(1).strip().lower()
            month_str = m.group(2).strip().lower()
            year_str = m.group(3) or ""

            day = _ES_DAY_ORDINALS.get(day_str)
            if day is None:
                try:
                    day = int(day_str)
                except ValueError:
                    return m.group(0)
            if not (1 <= day <= 31):
                return m.group(0)
            month = _ES_MONTHS.get(month_str)
            if month is None:
                return m.group(0)

            if year_str:
                return self._fmt_date(day, month, int(year_str))
            return self._fmt_date(day, month)

        return re.sub(full_pat, _repl_date, text, flags=re.IGNORECASE)

    # ------------------------------------------------------------------
    # Английский: ВРЕМЯ
    # ------------------------------------------------------------------

    def _normalize_time_en(self, text: str) -> str:
        """«nine in the morning» → «09:00», «9 am» → «09:00»"""
        hour_words_pat = "|".join(
            re.escape(h) for h in sorted(_EN_HOUR_WORDS.keys(), key=len, reverse=True)
        )
        markers_pat = "|".join(
            re.escape(mk) for mk in sorted(_EN_TIME_MARKERS.keys(), key=len, reverse=True)
        )

        # Pattern 1: word-hour + REQUIRED time anchor (marker or :MM).
        # Bare cardinals like «two people» must NOT be converted.
        # Anchors accepted: «o'clock», am/pm, or any time-of-day marker phrase.
        oclock_anchor = r"o'?clock"
        word_hour_pat = (
            rf"(?<!\w)({hour_words_pat})"
            rf"(?:"
            rf"(?::(\d{{2}})\s*({markers_pat})?)"  # :MM [marker]
            rf"|(?:\s+({oclock_anchor})(?:\s+({markers_pat}))?)"  # o'clock [marker]
            rf"|(?:\s+({markers_pat}))"  # standalone marker (in the morning / pm / etc.)
            rf")"
            rf"(?!\w)"
        )

        # Pattern 2: digit + explicit am/pm marker (mandatory)
        # «9 am», «11:30 pm»
        ampm_markers = r"(?:a\.m\.|p\.m\.|am|pm)"
        digit_ampm_pat = (
            rf"(?<!\d)(\d{{1,2}})"
            rf"(?::(\d{{2}}))?"
            rf"\s*({ampm_markers})"
            rf"(?!\w)"
        )

        def _repl_word_time(m: re.Match) -> str:
            # Groups for word_hour_pat (anchored variant, 6 groups):
            #   1: hour word
            #   2: :MM digits  (branch 1)
            #   3: marker after :MM  (branch 1)
            #   4: o'clock keyword  (branch 2)
            #   5: marker after o'clock  (branch 2)
            #   6: standalone marker  (branch 3)
            hour_str = m.group(1).strip().lower()
            min_str = m.group(2) or "00"
            marker_raw = (m.group(3) or m.group(5) or m.group(6) or "").strip().lower()

            hour = _EN_HOUR_WORDS.get(hour_str)
            if hour is None:
                try:
                    hour = int(hour_str)
                except ValueError:
                    return m.group(0)

            try:
                minute = int(min_str)
            except ValueError:
                minute = 0

            marker_key = None
            for mk in sorted(_EN_TIME_MARKERS.keys(), key=len, reverse=True):
                if mk in marker_raw:
                    marker_key = _EN_TIME_MARKERS[mk]
                    break

            if marker_key:
                hour = _apply_time_marker(hour, marker_key)

            return f"{hour:02d}:{minute:02d}"

        def _repl_digit_time(m: re.Match) -> str:
            # Groups for digit_ampm_pat (3 groups):
            #   1: digit hour, 2: :MM digits, 3: am/pm marker
            hour_str = m.group(1).strip().lower()
            min_str = m.group(2) or "00"
            marker_raw = (m.group(3) or "").strip().lower()

            try:
                hour = int(hour_str)
            except ValueError:
                return m.group(0)

            try:
                minute = int(min_str)
            except ValueError:
                minute = 0

            marker_key = None
            for mk in sorted(_EN_TIME_MARKERS.keys(), key=len, reverse=True):
                if mk in marker_raw:
                    marker_key = _EN_TIME_MARKERS[mk]
                    break

            if marker_key:
                hour = _apply_time_marker(hour, marker_key)

            return f"{hour:02d}:{minute:02d}"

        text = re.sub(word_hour_pat, _repl_word_time, text, flags=re.IGNORECASE)
        text = re.sub(digit_ampm_pat, _repl_digit_time, text, flags=re.IGNORECASE)
        return text

    def _normalize_date_en(self, text: str) -> str:
        """«third of November» → «03.11», «November 3rd 2026» → «03.11.2026»"""
        months_pat = "|".join(
            re.escape(m) for m in sorted(_EN_MONTHS.keys(), key=len, reverse=True)
        )
        day_ordinals_pat = "|".join(
            re.escape(d) for d in sorted(_EN_DAY_ORDINALS.keys(), key=len, reverse=True)
        )

        # «the third of November» / «third November»
        full_pat_1 = (
            rf"(?:the\s+)?({day_ordinals_pat})"
            rf"\s+(?:of\s+)?({months_pat})"
            rf"(?:[,\s]+(\d{{4}}))?"
        )

        def _repl_date1(m: re.Match) -> str:
            day_str = m.group(1).strip().lower().replace("-", " ")
            month_str = m.group(2).strip().lower()
            year_str = m.group(3) or ""
            day = _EN_DAY_ORDINALS.get(day_str)
            if day is None:
                return m.group(0)
            month = _EN_MONTHS.get(month_str)
            if month is None:
                return m.group(0)
            if year_str:
                return self._fmt_date(day, month, int(year_str))
            return self._fmt_date(day, month)

        text = re.sub(full_pat_1, _repl_date1, text, flags=re.IGNORECASE)

        # «November 3rd 2026» / «November 3 2026»
        full_pat_2 = (
            rf"({months_pat})"
            rf"\s+(\d{{1,2}})(?:st|nd|rd|th)?"
            rf"(?:[,\s]+(\d{{4}}))?"
        )

        def _repl_date2(m: re.Match) -> str:
            month_str = m.group(1).strip().lower()
            day = int(m.group(2))
            year_str = m.group(3) or ""
            if not (1 <= day <= 31):
                return m.group(0)
            month = _EN_MONTHS.get(month_str)
            if month is None:
                return m.group(0)
            if year_str:
                return self._fmt_date(day, month, int(year_str))
            return self._fmt_date(day, month)

        text = re.sub(full_pat_2, _repl_date2, text, flags=re.IGNORECASE)
        return text
