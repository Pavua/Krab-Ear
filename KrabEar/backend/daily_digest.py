"""DailyDigestGenerator — генератор ежедневного дайджеста транскрипций Krab Ear.

Агрегирует статистику и ключевые выдержки за указанный день,
формирует готовый Markdown-отчёт.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import logging

logger = logging.getLogger("KrabEar.Backend.DailyDigest")

# ---------------------------------------------------------------------------
# Стоп-слова (RU + ES + EN) — копируем из HistoryService для независимости
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset = frozenset({
    # RU
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "не", "ни", "бы", "же", "ли", "и", "а", "но", "да",
    "то", "или", "что", "как", "так", "уже", "ещё", "еще", "все", "этот",
    "это", "эта", "этой", "этого", "этим", "этих", "он", "она", "оно", "они",
    "мы", "вы", "я", "его", "её", "ее", "их", "мой", "твой", "наш", "ваш",
    "свой", "себя", "тот", "та", "те", "такой", "такие", "быть", "есть",
    "был", "была", "были", "будет", "будут", "там", "здесь", "тут", "где",
    "когда", "потому", "потом", "затем", "вот", "ну", "вдруг", "если", "нет",
    "очень", "более", "менее", "больше", "меньше", "можно", "нужно", "надо",
    # ES
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "al", "en", "con", "por", "para", "sin", "sobre", "entre", "ante",
    "bajo", "desde", "hasta", "hacia", "durante", "y", "e", "o", "u",
    "pero", "sino", "que", "como", "si", "se", "me", "te", "le", "nos",
    "os", "les", "lo", "su", "sus", "mi", "mis", "tu", "tus", "este",
    "esta", "estos", "estas", "ese", "esa", "esos", "esas", "yo",
    "él", "ella", "ellos", "ellas", "usted", "ustedes", "nosotros",
    "vosotros", "es", "son", "era", "fue", "ser", "estar", "hay", "ya",
    "no", "más", "muy", "bien", "también", "sí", "así", "todo", "todos",
    # EN
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "up", "about", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under", "again",
    "and", "but", "or", "nor", "so", "yet", "both", "either", "neither",
    "not", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "shall", "can", "must", "it", "its", "this", "that",
    "these", "those", "i", "you", "he", "she", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "our", "their", "what",
    "which", "who", "when", "where", "how", "all", "each", "more", "also",
})


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (нижний регистр, только буквы)."""
    return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------

@dataclass
class DailyDigest:
    """Ежедневный дайджест транскрипций."""

    date: str
    """Дата дайджеста в формате YYYY-MM-DD."""

    total_recordings: int
    """Количество записей за день."""

    total_duration_min: float
    """Суммарная длительность аудио в минутах (на основе audio_duration_sec)."""

    total_words: int
    """Суммарное количество слов во всех транскрипциях."""

    languages_used: dict[str, int]
    """Частота языков: {lang_code: count}."""

    top_topics: list[str]
    """Топ-10 ключевых слов дня (без стоп-слов) — «темы» дня."""

    highlights: list[str]
    """Топ-3 фрагмента: самые длинные / высококонфидентные транскрипции."""

    formatted_markdown: str
    """Готовый Markdown-отчёт для чтения."""


# ---------------------------------------------------------------------------
# Генератор
# ---------------------------------------------------------------------------

class DailyDigestGenerator:
    """Генерирует ежедневный дайджест транскрипций из StateStore."""

    def generate_digest(
        self,
        date_str: str | None = None,
        store: Any = None,
    ) -> DailyDigest:
        """Генерирует дайджест за указанный день.

        Args:
            date_str: Дата в формате ``YYYY-MM-DD``.
                      Если ``None`` — используется сегодняшняя дата (локальная).
            store:    Экземпляр ``StateStore`` (или совместимый объект).
                      Если ``None`` — возвращает пустой дайджест.

        Returns:
            ``DailyDigest`` с агрегированной статистикой и Markdown-отчётом.
        """
        # Определяем целевую дату
        if date_str is None:
            date_str = datetime.now(timezone.utc).date().isoformat()  # UTC to match stored UTC timestamps

        # Валидация формата
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"Неверный формат даты: {date_str!r}. Ожидается YYYY-MM-DD") from exc

        if store is None:
            return self._empty_digest(date_str)

        # Загружаем все активные записи
        try:
            all_items = store._load_active_items_with_lock()
        except Exception:
            logger.exception("Ошибка при загрузке истории из store")
            return self._empty_digest(date_str)

        # Фильтруем по дате
        day_items = []
        for item in all_items:
            item_date = self._parse_item_date(item)
            if item_date == target_date:
                day_items.append(item)

        if not day_items:
            return self._empty_digest(date_str)

        # Агрегируем
        total_recordings = len(day_items)
        total_duration_sec = sum(
            (item.audio_duration_sec or 0.0) for item in day_items
        )
        total_duration_min = round(total_duration_sec / 60.0, 2)

        total_words = sum(
            len((item.text or "").split()) for item in day_items
        )

        languages_used: dict[str, int] = {}
        for item in day_items:
            lang = (getattr(item, "source_lang", "") or "").strip()
            if lang:
                languages_used[lang] = languages_used.get(lang, 0) + 1

        top_topics = self._extract_top_topics(day_items, top_n=10)
        highlights = self._extract_highlights(day_items, top_n=3)

        formatted_markdown = self._build_markdown(
            date_str=date_str,
            total_recordings=total_recordings,
            total_duration_min=total_duration_min,
            total_words=total_words,
            languages_used=languages_used,
            top_topics=top_topics,
            highlights=highlights,
        )

        return DailyDigest(
            date=date_str,
            total_recordings=total_recordings,
            total_duration_min=total_duration_min,
            total_words=total_words,
            languages_used=languages_used,
            top_topics=top_topics,
            highlights=highlights,
            formatted_markdown=formatted_markdown,
        )

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_item_date(item: Any) -> "date | None":
        """Возвращает дату записи или None при ошибке парсинга."""
        ts = getattr(item, "ts", "") or ""
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    @staticmethod
    def _extract_top_topics(items: list, top_n: int = 10) -> list[str]:
        """Топ-N ключевых слов из всех транскрипций (без стоп-слов)."""
        all_tokens: list[str] = []
        for item in items:
            text = (getattr(item, "text", "") or "").strip()
            if not text:
                continue
            tokens = [
                w for w in _tokenize(text)
                if w not in _STOP_WORDS and len(w) > 2
            ]
            all_tokens.extend(tokens)
        counter = Counter(all_tokens)
        return [word for word, _ in counter.most_common(top_n)]

    @staticmethod
    def _extract_highlights(items: list, top_n: int = 3) -> list[str]:
        """Топ-N записей по длине текста (или confidence, если есть).

        Сначала сортируем по confidence (убывание), затем по количеству слов.
        """
        def _score(item: Any) -> tuple:
            conf = item.confidence if item.confidence is not None else -1.0
            words = len((item.text or "").split())
            return (conf, words)

        sorted_items = sorted(items, key=_score, reverse=True)
        highlights = []
        for item in sorted_items[:top_n]:
            text = (getattr(item, "text", "") or "").strip()
            if text:
                # Обрезаем до 200 символов для краткости
                snippet = text[:200] + ("…" if len(text) > 200 else "")
                highlights.append(snippet)
        return highlights

    @staticmethod
    def _build_markdown(
        date_str: str,
        total_recordings: int,
        total_duration_min: float,
        total_words: int,
        languages_used: dict[str, int],
        top_topics: list[str],
        highlights: list[str],
    ) -> str:
        """Формирует Markdown-отчёт."""
        lines: list[str] = []
        lines.append(f"# Дайджест транскрипций — {date_str}")
        lines.append("")

        # Сводная статистика
        lines.append("## Сводка")
        lines.append("")
        lines.append(f"- **Записей:** {total_recordings}")
        lines.append(f"- **Длительность:** {total_duration_min} мин")
        lines.append(f"- **Слов:** {total_words}")

        if languages_used:
            lang_str = ", ".join(
                f"{lang} ({cnt})" for lang, cnt in sorted(
                    languages_used.items(), key=lambda x: -x[1]
                )
            )
            lines.append(f"- **Языки:** {lang_str}")

        lines.append("")

        # Темы дня
        if top_topics:
            lines.append("## Темы дня")
            lines.append("")
            lines.append(", ".join(f"`{w}`" for w in top_topics))
            lines.append("")

        # Избранные фрагменты
        if highlights:
            lines.append("## Избранные фрагменты")
            lines.append("")
            for i, snippet in enumerate(highlights, start=1):
                lines.append(f"**{i}.** {snippet}")
                lines.append("")

        if total_recordings == 0:
            lines.append("_Записей за этот день не найдено._")
            lines.append("")

        return "\n".join(lines)

    def _empty_digest(self, date_str: str) -> DailyDigest:
        """Возвращает пустой дайджест для дня без записей."""
        return DailyDigest(
            date=date_str,
            total_recordings=0,
            total_duration_min=0.0,
            total_words=0,
            languages_used={},
            top_topics=[],
            highlights=[],
            formatted_markdown=self._build_markdown(
                date_str=date_str,
                total_recordings=0,
                total_duration_min=0.0,
                total_words=0,
                languages_used={},
                top_topics=[],
                highlights=[],
            ),
        )
