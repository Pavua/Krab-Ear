"""TimelineViewGenerator — группировка истории транскрипций по временным блокам.

Используется для отображения истории в виде временной шкалы (timeline)
и тепловой карты активности (activity heatmap).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import logging

logger = logging.getLogger("KrabEar.Backend.TimelineView")

# ---------------------------------------------------------------------------
# Стоп-слова (RU + ES + EN) — для краткого summary_text блока
# ---------------------------------------------------------------------------
_STOP_WORDS: frozenset = frozenset({
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "не", "ни", "бы", "же", "ли", "и", "а", "но", "да",
    "то", "или", "что", "как", "так", "уже", "ещё", "еще", "все", "этот",
    "это", "эта", "он", "она", "оно", "они", "мы", "вы", "я",
    "el", "la", "los", "las", "un", "una", "de", "del", "al", "en", "con",
    "por", "para", "sin", "sobre", "y", "o", "pero", "que", "como", "si",
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with", "by",
    "and", "but", "or", "is", "are", "was", "were", "it", "this", "that",
})

_VALID_GROUP_BY = ("hour", "day", "week")


def _tokenize(text: str) -> list[str]:
    """Разбивает текст на слова (нижний регистр, только буквы)."""
    return re.findall(r"[^\W\d_]+", text.lower(), re.UNICODE)


def _parse_ts(ts: Any) -> datetime | None:
    """Парсит временную метку из строки ISO-8601. Возвращает None при ошибке."""
    if not ts:
        return None
    ts_str = str(ts).strip()
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _block_start(dt: datetime, group_by: str) -> datetime:
    """Возвращает начало временного блока для datetime dt."""
    if group_by == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    elif group_by == "day":
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif group_by == "week":
        # ISO weekday: Monday=1 … Sunday=7; смещаем к понедельнику
        days_since_monday = dt.weekday()  # 0=Mon, 6=Sun
        monday = dt - timedelta(days=days_since_monday)
        return monday.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"Неверный group_by: {group_by!r}. Ожидается: hour, day, week")


def _block_end(block_start: datetime, group_by: str) -> datetime:
    """Возвращает конец (exclusive) временного блока."""
    if group_by == "hour":
        return block_start + timedelta(hours=1)
    elif group_by == "day":
        return block_start + timedelta(days=1)
    elif group_by == "week":
        return block_start + timedelta(weeks=1)
    else:
        raise ValueError(f"Неверный group_by: {group_by!r}")


# ---------------------------------------------------------------------------
# Dataclass результата
# ---------------------------------------------------------------------------

@dataclass
class TimelineBlock:
    """Один временной блок в timeline транскрипций."""

    start_time: str
    """ISO-8601 начало блока."""

    end_time: str
    """ISO-8601 конец блока (exclusive)."""

    items_count: int
    """Количество записей в блоке."""

    total_duration_sec: float
    """Суммарная длительность аудио в секундах (на основе audio_duration_sec)."""

    total_words: int
    """Суммарное количество слов во всех транскрипциях блока."""

    languages: list[str]
    """Список уникальных языков в блоке (отсортирован по частоте)."""

    summary_text: str
    """Краткий текст: топ-5 ключевых слов блока через запятую."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
            "items_count": self.items_count,
            "total_duration_sec": self.total_duration_sec,
            "total_words": self.total_words,
            "languages": self.languages,
            "summary_text": self.summary_text,
        }


# ---------------------------------------------------------------------------
# Генератор
# ---------------------------------------------------------------------------

class TimelineViewGenerator:
    """Генерирует timeline-view и activity heatmap для истории транскрипций."""

    def generate_timeline(
        self,
        items: list[Any],
        group_by: str = "hour",
    ) -> list[TimelineBlock]:
        """Группирует записи истории по временным блокам.

        Args:
            items:    Список объектов истории (HistoryItem или dict) с полями
                      ``ts``, ``text``, ``audio_duration_sec``, ``source_lang``.
            group_by: Гранулярность блоков: ``"hour"``, ``"day"``, ``"week"``.

        Returns:
            Список ``TimelineBlock``, отсортированный по ``start_time`` (возрастание).

        Raises:
            ValueError: если ``group_by`` не из допустимых значений.
        """
        if group_by not in _VALID_GROUP_BY:
            raise ValueError(
                f"Неверный group_by: {group_by!r}. Допустимые значения: {_VALID_GROUP_BY}"
            )

        # Группируем items по ключу блока
        groups: dict[datetime, list[Any]] = defaultdict(list)
        for item in items:
            ts_val = self._get_field(item, "ts")
            dt = _parse_ts(ts_val)
            if dt is None:
                continue
            key = _block_start(dt, group_by)
            groups[key].append(item)

        blocks: list[TimelineBlock] = []
        for block_key in sorted(groups):
            block_items = groups[block_key]
            end_dt = _block_end(block_key, group_by)
            block = self._build_block(block_key, end_dt, block_items)
            blocks.append(block)

        return blocks

    def generate_activity_heatmap(
        self,
        items: list[Any],
        days: int = 30,
    ) -> dict[str, Any]:
        """Генерирует матрицу активности: час-дня × день-недели.

        Args:
            items: Список объектов истории с полем ``ts``.
            days:  Горизонт в прошлое (дней). Записи старше этого периода игнорируются.

        Returns:
            Словарь с ключами:
              - ``"matrix"``: dict[hour_str -> dict[dow_str -> count]]
                (hour 0–23, dow 0–6, где 0=понедельник, 6=воскресенье)
              - ``"total_items"``: количество учтённых записей
              - ``"days_covered"``: фактическое количество дней с записями
              - ``"peak_hour"``: час дня с максимальной активностью (0–23 или None)
              - ``"peak_dow"``: день недели с максимальной активностью (0–6 или None)
        """
        if days <= 0:
            raise ValueError(f"days должен быть > 0, получено: {days}")

        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(days=days)

        # Матрица: hour (0-23) × dow (0-6, Mon=0)
        matrix: dict[int, dict[int, int]] = {
            h: {d: 0 for d in range(7)} for h in range(24)
        }

        total_items = 0
        active_days: set[str] = set()

        for item in items:
            ts_val = self._get_field(item, "ts")
            dt = _parse_ts(ts_val)
            if dt is None:
                continue
            if dt < cutoff:
                continue

            hour = dt.hour
            dow = dt.weekday()  # 0=Mon, 6=Sun
            matrix[hour][dow] += 1
            total_items += 1
            active_days.add(dt.date().isoformat())

        # Находим пиковые значения
        peak_hour: int | None = None
        peak_dow: int | None = None
        max_hour_sum = -1
        max_dow_sum = -1

        for h in range(24):
            s = sum(matrix[h].values())
            if s > max_hour_sum:
                max_hour_sum = s
                peak_hour = h

        for d in range(7):
            s = sum(matrix[h][d] for h in range(24))
            if s > max_dow_sum:
                max_dow_sum = s
                peak_dow = d

        if max_hour_sum == 0:
            peak_hour = None
        if max_dow_sum == 0:
            peak_dow = None

        # Сериализуем ключи как строки для JSON-совместимости
        serialized_matrix: dict[str, dict[str, int]] = {
            str(h): {str(d): matrix[h][d] for d in range(7)}
            for h in range(24)
        }

        return {
            "matrix": serialized_matrix,
            "total_items": total_items,
            "days_covered": len(active_days),
            "peak_hour": peak_hour,
            "peak_dow": peak_dow,
        }

    # ------------------------------------------------------------------
    # Вспомогательные методы
    # ------------------------------------------------------------------

    @staticmethod
    def _get_field(item: Any, field_name: str) -> Any:
        """Читает поле из объекта или словаря."""
        if isinstance(item, dict):
            return item.get(field_name)
        return getattr(item, field_name, None)

    def _build_block(
        self,
        start_dt: datetime,
        end_dt: datetime,
        block_items: list[Any],
    ) -> TimelineBlock:
        """Строит TimelineBlock из списка записей."""
        total_duration_sec = 0.0
        total_words = 0
        lang_counter: Counter = Counter()
        all_tokens: list[str] = []

        for item in block_items:
            dur = self._get_field(item, "audio_duration_sec")
            if dur is not None:
                try:
                    total_duration_sec += float(dur)
                except (TypeError, ValueError):
                    pass

            text = self._get_field(item, "text") or ""
            words = text.split()
            total_words += len(words)

            tokens = [
                w for w in _tokenize(text)
                if w not in _STOP_WORDS and len(w) > 2
            ]
            all_tokens.extend(tokens)

            lang = self._get_field(item, "source_lang") or ""
            if isinstance(lang, str) and lang.strip():
                lang_counter[lang.strip()] += 1

        # Топ-5 ключевых слов для краткого описания блока
        word_counter = Counter(all_tokens)
        top_words = [w for w, _ in word_counter.most_common(5)]
        summary_text = ", ".join(top_words) if top_words else ""

        # Языки отсортированы по частоте (самый частый первым)
        languages = [lang for lang, _ in lang_counter.most_common()]

        return TimelineBlock(
            start_time=start_dt.isoformat(),
            end_time=end_dt.isoformat(),
            items_count=len(block_items),
            total_duration_sec=round(total_duration_sec, 3),
            total_words=total_words,
            languages=languages,
            summary_text=summary_text,
        )
