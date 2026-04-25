"""Утилита построения initial_prompt для Whisper STT из недавней истории.

Использует последние N элементов истории и пользовательские hotwords
для формирования контекстного подсказа, который помогает Whisper
лучше распознавать продолжение диктовки и терминологию пользователя.
"""

from __future__ import annotations

import time
from typing import Any

# Горизонт давности: элементы старше этого порога не используются как контекст.
_MAX_AGE_SECONDS: int = 30 * 60  # 30 минут

# Количество последних элементов истории для просмотра.
_DEFAULT_HISTORY_LIMIT: int = 10

# Разделитель между элементами в контексте.
_ITEM_SEP: str = " "


def _iso_to_epoch(ts: str) -> float:
    """Конвертирует ISO-8601 строку в Unix epoch (float).

    StateStore сохраняет timestamps как UTC datetime без суффикса "Z".
    Эта функция обрабатывает строку как UTC (не local time).

    Поддерживает форматы:
    - "2024-01-15T10:30:00.123456"  (UTC без суффикса — формат StateStore)
    - "2024-01-15T10:30:00Z"
    - "2024-01-15T10:30:00+00:00"
    - "2024-01-15 10:30:00"

    При ошибке парсинга возвращает 0.0 (элемент будет считаться устаревшим).
    """
    import datetime
    import calendar

    ts_clean = ts.strip()

    # Если есть timezone info — используем fromisoformat (поддерживает +HH:MM, Z в Python 3.11+)
    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"

    try:
        dt = datetime.datetime.fromisoformat(ts_clean)
        if dt.tzinfo is not None:
            # Aware datetime → epoch напрямую
            return dt.timestamp()
        # Naive datetime — считаем UTC (StateStore пишет UTC без суффикса)
        return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
    except ValueError:
        pass

    # Fallback: попытка strptime с явной трактовкой как UTC
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(ts_clean, fmt)
            return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
        except ValueError:
            continue

    return 0.0


def build_initial_prompt(
    history_items: list[Any],
    hotwords: list[str] | None = None,
    max_words: int = 250,
    max_age_seconds: int = _MAX_AGE_SECONDS,
    history_limit: int = _DEFAULT_HISTORY_LIMIT,
) -> str:
    """Строит строку initial_prompt для передачи в mlx_whisper.transcribe.

    Алгоритм:
    1. Берёт последние ``history_limit`` элементов (уже отсортированы newest-first
       в StateStore — берём их в обратном хронологическом порядке, потом реверсируем
       для хронологии).
    2. Отфильтровывает элементы старше ``max_age_seconds``.
    3. Объединяет тексты через пробел, обрезает до ``max_words`` слов.
    4. Если заданы ``hotwords`` — добавляет префикс "Glossary: term1, term2. ".
    5. Итоговая строка: "Glossary: ...<hotwords>. Previous transcript: <context>"
       или просто "<context>" если hotwords пустой.

    Args:
        history_items: Список объектов HistoryItem (или dict) с полями ``text`` и ``ts``.
                       Ожидается порядок newest-first (как возвращает StateStore.get_history).
        hotwords: Пользовательские термины для boosting'а. None/[] → без Glossary-префикса.
        max_words: Максимальное количество слов в части "Previous transcript".
        max_age_seconds: Максимальный возраст элемента в секундах.
        history_limit: Сколько последних элементов рассматривать.

    Returns:
        Строка initial_prompt (может быть пустой, если нет валидного контекста и hotwords).
    """
    now = time.time()

    # Берём не более history_limit элементов (newest-first → реверс для хронологии).
    recent = list(history_items[:history_limit])
    recent.reverse()  # теперь oldest-first; контекст читается естественно

    texts: list[str] = []
    for item in recent:
        # Поддержка как dataclass HistoryItem, так и dict (для тестов)
        if isinstance(item, dict):
            raw_text: str = str(item.get("text", "")).strip()
            raw_ts: str = str(item.get("ts", "")).strip()
        else:
            raw_text = str(getattr(item, "text", "")).strip()
            raw_ts = str(getattr(item, "ts", "")).strip()

        if not raw_text:
            continue

        # Проверка давности
        if raw_ts:
            age = now - _iso_to_epoch(raw_ts)
            if age > max_age_seconds:
                continue

        texts.append(raw_text)

    # Объединяем и обрезаем по словам
    combined = _ITEM_SEP.join(texts).strip()
    if combined:
        words = combined.split()
        if len(words) > max_words:
            words = words[-max_words:]  # берём хвост — он самый свежий
        combined = " ".join(words)

    # Формируем итоговый prompt
    parts: list[str] = []

    cleaned_hotwords = [w.strip() for w in (hotwords or []) if w.strip()]
    if cleaned_hotwords:
        parts.append(f"Glossary: {', '.join(cleaned_hotwords)}.")

    if combined:
        parts.append(f"Previous transcript: {combined}")

    return " ".join(parts)
