"""KeywordCloudGenerator — генератор данных облака ключевых слов для Krab Ear.

Подготавливает данные частоты слов для визуализации word cloud:
- фильтрация стоп-слов (RU/ES/EN/UK)
- нормализация регистра
- слияние похожих форм слов
- генерация SVG-облака с позиционированием по весу
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import logging

logger = logging.getLogger("KrabEar.Backend.KeywordCloud")

# ---------------------------------------------------------------------------
# Стоп-слова
# ---------------------------------------------------------------------------
try:
    from core.stop_words import StopWords
    _STOP_WORDS: frozenset = (
        StopWords.get_stop_words("ru")
        | StopWords.get_stop_words("es")
        | StopWords.get_stop_words("en")
        | StopWords.get_stop_words("uk")
    )
except Exception:  # fallback если core.stop_words недоступен
    _STOP_WORDS: frozenset = frozenset({
        # RU
        "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
        "об", "про", "при", "для", "без", "через", "между", "перед", "после",
        "и", "а", "но", "да", "то", "или", "не", "ни", "бы", "же", "ли",
        "он", "она", "оно", "они", "мы", "вы", "я", "его", "её", "их",
        "это", "этот", "эта", "быть", "есть", "был", "была", "были",
        # ES
        "el", "la", "los", "las", "un", "una", "de", "del", "en", "con",
        "por", "para", "sin", "que", "es", "son", "era", "fue", "y", "o",
        # EN
        "the", "a", "an", "of", "in", "on", "at", "to", "for", "with",
        "and", "but", "or", "not", "is", "are", "was", "were", "be",
        "have", "has", "had", "do", "does", "did", "it", "its",
        "i", "you", "he", "she", "we", "they", "my", "your",
    })

# ---------------------------------------------------------------------------
# Минимальные пороги
# ---------------------------------------------------------------------------
_MIN_WORD_LENGTH = 2
_FONT_SIZE_MIN = 12
_FONT_SIZE_MAX = 72

# Верхняя граница max_words на уровне модуля (защита от OOM при огромных значениях)
_MAX_WORDS_LIMIT = 1000

# ---------------------------------------------------------------------------
# Похожие слова: пары вариантов написания для слияния
# ---------------------------------------------------------------------------
_MERGE_PAIRS: list[tuple[str, str]] = [
    # Русские варианты «е» / «ё»
    ("ещё", "еще"),
    ("её", "ее"),
]

# Словарь слияния похожих слов (вариант → канонический вид).
# Построен один раз на уровне модуля, не пересоздаётся при каждом вызове.
_MERGE_MAP: dict[str, str] = {variant: canonical for canonical, variant in _MERGE_PAIRS}


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------

@dataclass
class CloudWord:
    """Слово облака ключевых слов с метриками визуализации."""

    word: str
    """Слово (нормализованный нижний регистр)."""

    count: int
    """Абсолютная частота в корпусе."""

    weight: float
    """Нормализованный вес 0–1 относительно самого частого слова."""

    font_size: int
    """Размер шрифта в px, масштабированный в диапазон 12–72."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "word": self.word,
            "count": self.count,
            "weight": self.weight,
            "font_size": self.font_size,
        }


# ---------------------------------------------------------------------------
# Основной генератор
# ---------------------------------------------------------------------------

class KeywordCloudGenerator:
    """Генерирует данные облака ключевых слов из списка элементов истории."""

    def __init__(
        self,
        stop_words: frozenset | None = None,
        min_word_length: int = _MIN_WORD_LENGTH,
        font_size_min: int = _FONT_SIZE_MIN,
        font_size_max: int = _FONT_SIZE_MAX,
        seed: int | None = 42,
    ) -> None:
        self._stop_words = stop_words if stop_words is not None else _STOP_WORDS
        self._min_word_length = min_word_length
        self._font_size_min = font_size_min
        self._font_size_max = font_size_max
        self._seed = seed

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def generate_cloud(
        self,
        items: list[Any],
        max_words: int = 100,
        language: str | None = None,
    ) -> list[CloudWord]:
        """Генерирует список CloudWord из элементов истории.

        Args:
            items:     Список объектов истории или словарей с полем ``text``
                       (и опционально ``source_lang``).
            max_words: Максимальное количество слов в облаке (default 100).
            language:  Фильтрация по языку-источнику ('ru', 'es', 'en', …).
                       Если None — обрабатываются все записи.

        Returns:
            Список ``CloudWord`` отсортированный по убыванию count.
        """
        if not items:
            return []

        # Зажать max_words в диапазон [0, _MAX_WORDS_LIMIT] на уровне генератора.
        # max_words <= 0 означает «вернуть пустой список».
        max_words = min(max(0, int(max_words)), _MAX_WORDS_LIMIT)
        if max_words == 0:
            return []

        words = self._collect_words(items, language=language)
        if not words:
            return []

        counter = Counter(words)
        top_n = counter.most_common(max_words)

        if not top_n:
            return []

        max_count = top_n[0][1]

        result: list[CloudWord] = []
        for word, count in top_n:
            weight = round(count / max_count, 4) if max_count > 0 else 0.0
            font_size = self._scale_font(weight)
            result.append(CloudWord(word=word, count=count, weight=weight, font_size=font_size))

        return result

    def generate_cloud_svg(
        self,
        items: list[Any],
        width: int = 800,
        height: int = 400,
        max_words: int = 60,
        language: str | None = None,
    ) -> str:
        """Генерирует SVG-облако ключевых слов.

        Каждое слово размещается в случайной позиции, размер шрифта
        пропорционален частоте слова. Используется детерминированный
        seed для воспроизводимости (если задан при создании генератора).

        Args:
            items:     Элементы истории (см. generate_cloud).
            width:     Ширина SVG в px.
            height:    Высота SVG в px.
            max_words: Максимальное число слов (default 60).
            language:  Фильтр языка.

        Returns:
            SVG-строка.
        """
        cloud_words = self.generate_cloud(items, max_words=max_words, language=language)

        if not cloud_words:
            return self._empty_svg(width, height)

        rng = random.Random(self._seed)

        # Цветовая палитра
        colors = [
            "#4A90D9", "#E67E22", "#27AE60", "#8E44AD",
            "#E74C3C", "#16A085", "#2980B9", "#D35400",
            "#1ABC9C", "#9B59B6", "#C0392B", "#2ECC71",
        ]

        elements: list[str] = []
        for cw in cloud_words:
            # Случайная позиция с отступом от краёв, пропорциональным размеру шрифта
            margin_x = cw.font_size * 3
            margin_y = cw.font_size
            x = rng.randint(margin_x, max(margin_x + 1, width - margin_x))
            y = rng.randint(margin_y + cw.font_size, max(margin_y + cw.font_size + 1, height - margin_y))
            color = rng.choice(colors)
            # Небольшой наклон для реалистичности
            rotate = rng.choice([0, 0, 0, -15, 15, -30, 30])
            opacity = round(0.6 + cw.weight * 0.4, 2)
            transform = f"rotate({rotate}, {x}, {y})" if rotate != 0 else ""
            elements.append(
                f'  <text x="{x}" y="{y}" font-size="{cw.font_size}" fill="{color}" '
                f'opacity="{opacity}" font-family="Arial,sans-serif" '
                f'text-anchor="middle" transform="{transform}">'
                f'{_escape_xml(cw.word)}</text>'
            )

        body = "\n".join(elements)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n'
            f'  <rect width="{width}" height="{height}" fill="#1a1a2e" rx="8"/>\n'
            f'{body}\n'
            f'</svg>'
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _collect_words(
        self,
        items: list[Any],
        language: str | None = None,
    ) -> list[str]:
        """Извлекает, фильтрует и нормализует слова из истории."""
        language_filter = (language or "").strip().lower() or None
        words: list[str] = []

        for item in items:
            # Поддержка как словарей, так и объектов с атрибутами
            if isinstance(item, dict):
                text = (item.get("text") or "").strip()
                lang = (item.get("source_lang") or "").strip().lower()
            else:
                text = (getattr(item, "text", "") or "").strip()
                lang = (getattr(item, "source_lang", "") or "").strip().lower()

            if language_filter and lang != language_filter:
                continue

            if not text:
                continue

            tokens = self._tokenize(text)
            filtered = [
                w for w in tokens
                if len(w) >= self._min_word_length and w not in self._stop_words
            ]
            words.extend(filtered)

        return self._merge_similar(words)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Разбивает текст на слова (нижний регистр, только буквы)."""
        return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)

    @staticmethod
    def _merge_similar(words: list[str]) -> list[str]:
        """Объединяет известные варианты написания одного слова.

        Использует модульную константу _MERGE_MAP (вариант → канонический вид),
        которая построена один раз при загрузке модуля.
        """
        return [_MERGE_MAP.get(w, w) for w in words]

    def _scale_font(self, weight: float) -> int:
        """Масштабирует вес (0-1) в размер шрифта (font_size_min..font_size_max)."""
        span = self._font_size_max - self._font_size_min
        return int(round(self._font_size_min + weight * span))

    @staticmethod
    def _empty_svg(width: int, height: int) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">\n'
            f'  <rect width="{width}" height="{height}" fill="#1a1a2e" rx="8"/>\n'
            f'  <text x="{width // 2}" y="{height // 2}" fill="#888" '
            f'font-family="Arial,sans-serif" text-anchor="middle">Нет данных</text>\n'
            f'</svg>'
        )

    @property
    def _stop_words(self) -> frozenset:
        return self.__stop_words

    @_stop_words.setter
    def _stop_words(self, value: frozenset) -> None:
        self.__stop_words = value


def _escape_xml(text: str) -> str:
    """Экранирует специальные XML-символы."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#39;")
    )
