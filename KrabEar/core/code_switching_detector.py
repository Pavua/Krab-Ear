"""Детектор code-switching (смешения языков) в транскрипциях.

Технические разговоры часто смешивают русский и английский:
«я запушил коммит в main», «сделал PR в develop», «функция return-ит None».
Whisper иногда сбивается на таких текстах -- использование hint в initial_prompt
улучшает качество транскрибации.

Эвристика:
- Разбиваем текст на слова.
- Для каждого слова определяем скрипт: кириллица, латиница или нейтральный
  (цифры, знаки препинания, технические токены вроде camelCase / snake_case).
- Вычисляем долю латинских слов среди нелатинских.
- Code-switching = 10-90% Latin words в доминантно кириллическом тексте.
"""

from __future__ import annotations

import re
from typing import Optional


# Паттерны технических токенов: camelCase, snake_case, URL, хэши.
# Эти слова не учитываются при подсчёте латинского скрипта как «переключение».
_TECH_TOKEN_RE = re.compile(
    r"""
    ^
    (?:
        # camelCase / PascalCase: содержит хотя бы одну заглавную и одну строчную ASCII
        (?=[a-zA-Z])(?=.*[A-Z])(?=.*[a-z])[a-zA-Z0-9]+ |
        # snake_case / SCREAMING_SNAKE: содержит _
        [a-zA-Z0-9]+(?:_[a-zA-Z0-9]+)+ |
        # URL / path: содержит :// или .extension/
        \S+://\S+ |
        [a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)+(?:/\S*)? |
        # Хеш / hex-строка длиной >= 7 символов (SHA/git)
        [0-9a-fA-F]{7,}
    )
    $
    """,
    re.VERBOSE,
)

# Один или несколько кириллических символов
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")

# Один или несколько латинских символов (базовый ASCII латинский диапазон)
_LATIN_RE = re.compile(r"[a-zA-Z]")


def _classify_word(word: str) -> Optional[str]:
    """Классифицирует слово по доминантному скрипту.

    Returns:
        "cyrillic" | "latin" | None (нейтральный / технический токен)
    """
    if not word:
        return None

    # Технические токены пропускаются ПЕРЕД strip (URL, snake_case, camelCase).
    if _TECH_TOKEN_RE.match(word):
        return None

    # Убираем знаки препинания и спецсимволы для анализа скрипта
    clean = re.sub(r"[^\w]", "", word)
    if not clean:
        return None

    # Повторная проверка после strip (camelCase без знаков препинания)
    if _TECH_TOKEN_RE.match(clean):
        return None

    has_cyr = bool(_CYRILLIC_RE.search(clean))
    has_lat = bool(_LATIN_RE.search(clean))

    if has_cyr and not has_lat:
        return "cyrillic"
    if has_lat and not has_cyr:
        return "latin"
    # Смешанное слово (например транслит): нейтральный
    return None


class CodeSwitchingDetector:
    """Эвристический детектор смешения языков (code-switching).

    Анализирует текст на наличие переключения между кириллицей (RU)
    и латиницей (EN/ES) с учётом порога switch_threshold.

    Пример:
        >>> d = CodeSwitchingDetector()
        >>> r = d.analyze("я запушил коммит в main репозиторий")
        >>> r["is_mixed"]
        True
        >>> r["primary_lang"]
        'ru'
    """

    def __init__(
        self,
        switch_threshold: float = 0.10,
        max_switch_ratio: float = 0.90,
    ) -> None:
        """
        Args:
            switch_threshold: Минимальная доля «иностранных» слов для
                детектирования code-switching. По умолчанию 0.10 (10%).
            max_switch_ratio: Максимальная доля «иностранных» слов при которой
                считаем code-switching (выше -- это просто другой язык). 0.90.
        """
        self._threshold = switch_threshold
        self._max_ratio = max_switch_ratio

    def analyze(self, text: str) -> dict:
        """Анализирует текст на code-switching.

        Args:
            text: Входной текст (транскрипция или история).

        Returns:
            dict с ключами:
                - ``is_mixed`` (bool): True если детектировано переключение.
                - ``primary_lang`` (str): Доминантный язык ("ru", "en", "unknown").
                - ``secondary_lang`` (str | None): Второй язык или None.
                - ``switch_ratio`` (float): Доля слов вторичного языка [0.0-1.0].
        """
        if not text or not text.strip():
            return {
                "is_mixed": False,
                "primary_lang": "unknown",
                "secondary_lang": None,
                "switch_ratio": 0.0,
            }

        words = text.split()
        cyrillic_count = 0
        latin_count = 0

        for word in words:
            script = _classify_word(word)
            if script == "cyrillic":
                cyrillic_count += 1
            elif script == "latin":
                latin_count += 1
            # Нейтральные (цифры, технические токены) не считаются

        total_classified = cyrillic_count + latin_count
        if total_classified == 0:
            # Только цифры / нейтральные слова
            return {
                "is_mixed": False,
                "primary_lang": "unknown",
                "secondary_lang": None,
                "switch_ratio": 0.0,
            }

        # Определяем первичный и вторичный язык
        if cyrillic_count >= latin_count:
            primary_lang = "ru"
            secondary_lang = "en" if latin_count > 0 else None
            switch_ratio = latin_count / total_classified
        else:
            primary_lang = "en"
            secondary_lang = "ru" if cyrillic_count > 0 else None
            switch_ratio = cyrillic_count / total_classified

        # Code-switching: вторичный язык присутствует в значимом количестве,
        # но не настолько, чтобы стать доминирующим.
        is_mixed = (
            secondary_lang is not None
            and self._threshold <= switch_ratio <= self._max_ratio
        )

        return {
            "is_mixed": is_mixed,
            "primary_lang": primary_lang,
            "secondary_lang": secondary_lang if is_mixed else None,
            "switch_ratio": round(switch_ratio, 4),
        }
