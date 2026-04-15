"""SearchHighlighter — подсветка совпадений в поисковых результатах.

Поддерживает:
- Маркировку совпадений символами-маркерами (Markdown-bold и др.)
- HTML-подсветку через <span>
- Извлечение контекстных сниппетов вокруг совпадений

Поиск без учёта регистра, поддержка многословных запросов
(каждое слово подсвечивается независимо).
"""

from __future__ import annotations

import html
import re
from typing import List


class SearchHighlighter:
    """Подсветка совпадений поискового запроса в тексте."""

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def highlight(
        self,
        text: str,
        query: str,
        marker: str = "**",
    ) -> str:
        """Оборачивает совпавшие слова маркерами.

        Пример: highlight("слово найдено здесь", "найдено") → "слово **найдено** здесь"

        Args:
            text: исходный текст.
            query: поисковый запрос (одно или несколько слов).
            marker: символ/строка маркера (по умолчанию '**' для Markdown bold).

        Returns:
            Текст с обёрнутыми совпадениями.
        """
        if not text or not query:
            return text

        pattern = self._build_pattern(query)
        if pattern is None:
            return text

        def _replace(m: re.Match) -> str:
            return f"{marker}{m.group(0)}{marker}"

        return pattern.sub(_replace, text)

    def highlight_html(
        self,
        text: str,
        query: str,
        css_class: str = "highlight",
    ) -> str:
        """Оборачивает совпавшие слова HTML-тегом <span>.

        Пример: highlight_html("найдено", "найдено") → '<span class="highlight">найдено</span>'

        Args:
            text: исходный текст (будет HTML-экранирован).
            query: поисковый запрос.
            css_class: CSS-класс для тега <span>.

        Returns:
            HTML-строка с подсвеченными совпадениями.
        """
        if not text or not query:
            return html.escape(text) if text else text

        pattern = self._build_pattern(query)
        escaped_text = html.escape(text)

        if pattern is None:
            return escaped_text

        # Паттерн строим заново для escaped-текста, т.к. html.escape может
        # изменить символы (& → &amp; и т.д.) — работаем по словам запроса
        words = self._split_query(query)
        result = escaped_text
        for word in words:
            escaped_word = re.escape(html.escape(word))
            word_pattern = re.compile(escaped_word, re.IGNORECASE | re.UNICODE)
            result = word_pattern.sub(
                lambda m, cls=css_class: f'<span class="{cls}">{m.group(0)}</span>',
                result,
            )
        return result

    def extract_snippets(
        self,
        text: str,
        query: str,
        context_chars: int = 50,
        max_snippets: int = 3,
    ) -> List[str]:
        """Возвращает текстовые сниппеты вокруг совпадений.

        Каждый сниппет содержит совпадение и до context_chars символов контекста
        с каждой стороны.

        Args:
            text: исходный текст.
            query: поисковый запрос.
            context_chars: кол-во символов контекста до и после совпадения.
            max_snippets: максимальное кол-во сниппетов в результате.

        Returns:
            Список строк-сниппетов. Может быть пустым если совпадений нет.
        """
        if not text or not query:
            return []

        pattern = self._build_pattern(query)
        if pattern is None:
            return []

        matches = list(pattern.finditer(text))
        if not matches:
            return []

        snippets: List[str] = []
        used_ranges: List[tuple[int, int]] = []

        for m in matches:
            if len(snippets) >= max_snippets:
                break

            start = max(0, m.start() - context_chars)
            end = min(len(text), m.end() + context_chars)

            # Пропускаем пересекающиеся диапазоны
            if self._overlaps(start, end, used_ranges):
                continue

            used_ranges.append((start, end))

            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(text) else ""
            snippets.append(f"{prefix}{text[start:end]}{suffix}")

        return snippets

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    def _split_query(self, query: str) -> List[str]:
        """Разбивает запрос на отдельные слова, фильтруя пустые."""
        return [w for w in query.split() if w]

    def _build_pattern(self, query: str) -> re.Pattern | None:
        """Строит регулярное выражение для поиска слов из запроса."""
        words = self._split_query(query)
        if not words:
            return None
        # Объединяем слова через | (ИЛИ), каждое экранируем
        alternation = "|".join(re.escape(w) for w in words)
        return re.compile(alternation, re.IGNORECASE | re.UNICODE)

    def _overlaps(self, start: int, end: int, ranges: List[tuple[int, int]]) -> bool:
        """Проверяет, пересекается ли диапазон [start, end) с уже использованными."""
        for rs, re_ in ranges:
            if start < re_ and end > rs:
                return True
        return False
