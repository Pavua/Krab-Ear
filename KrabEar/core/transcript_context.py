"""Утилита построения initial_prompt для Whisper STT из недавней истории.

Использует последние N элементов истории и пользовательские hotwords
для формирования контекстного подсказа, который помогает Whisper
лучше распознавать продолжение диктовки и терминологию пользователя.
"""

from __future__ import annotations

import re
import time
from typing import Any

from core.code_switching_detector import CodeSwitchingDetector

# Горизонт давности: элементы старше этого порога не используются как контекст.
_MAX_AGE_SECONDS: int = 30 * 60  # 30 минут

# Количество последних элементов истории для просмотра.
_DEFAULT_HISTORY_LIMIT: int = 10

# Разделитель между элементами в контексте.
_ITEM_SEP: str = " "

# Максимальное число терминов в объединённом глоссарии (hotwords + auto_glossary).
_MAX_COMBINED_TERMS: int = 250

# W873-4 MEDIUM: Whisper BPE limit is 224 tokens for initial_prompt.
# Cyrillic words tokenize at ~2.5–3 BPE tokens each (morphologically rich,
# multi-character grapheme clusters), while Latin words average ~1.5 tokens.
# A 250-word Latin budget already risks overflow; 250 Cyrillic words exceed
# the limit by ~3×. We apply a language-aware cap:
#   - Cyrillic-heavy text (≥1 Cyrillic char in combined):  80 words  (~224 tokens)
#   - Latin / mixed / empty text:                         170 words  (~224 tokens)
# The detection is a fast O(1) regex search on the combined string — no
# character-level counting required.
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_MAX_WORDS_CYRILLIC: int = 80
_MAX_WORDS_LATIN: int = 170
# Hint добавляемый в initial_prompt при детектировании code-switching.
_CODE_SWITCHING_HINT = (
    "В записи может звучать смесь русского и английского (технические термины)."
)

_detector_cache: "CodeSwitchingDetector | None" = None


def _get_detector(threshold: float = 0.1) -> "CodeSwitchingDetector":
    """Возвращает (кэшированный) экземпляр детектора."""
    global _detector_cache
    if _detector_cache is None or _detector_cache._threshold != threshold:
        _detector_cache = CodeSwitchingDetector(switch_threshold=threshold)
    return _detector_cache


def _iso_to_epoch(ts: str) -> float:
    """Конвертирует ISO-8601 строку в Unix epoch (float).

    StateStore сохраняет timestamps как UTC datetime без суффикса "Z".
    Эта функция обрабатывает строку как UTC (не local time).

    Поддерживает форматы:
    - "2024-01-15T10:30:00.123456"  (UTC без суффикса -- формат StateStore)
    - "2024-01-15T10:30:00Z"
    - "2024-01-15T10:30:00+00:00"
    - "2024-01-15 10:30:00"

    При ошибке парсинга возвращает 0.0 (элемент будет считаться устаревшим).
    """
    import datetime
    import calendar

    ts_clean = ts.strip()

    if ts_clean.endswith("Z"):
        ts_clean = ts_clean[:-1] + "+00:00"

    try:
        dt = datetime.datetime.fromisoformat(ts_clean)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
    except ValueError:
        pass

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
    auto_glossary: list[str] | None = None,
    code_switching_detect: bool = True,
    code_switching_threshold: float = 0.1,
) -> str:
    """Строит строку initial_prompt для передачи в mlx_whisper.transcribe.

    Алгоритм:
    1. Берёт последние history_limit элементов newest-first, реверсирует для хронологии.
    2. Отфильтровывает элементы старше max_age_seconds.
    3. Объединяет тексты через пробел, обрезает до effective_max_words слов.
       effective_max_words выбирается динамически:
         - Если combined содержит хотя бы одну кириллическую букву →
           min(max_words, _MAX_WORDS_CYRILLIC).  Кириллические слова
           кодируются ~2.5–3 BPE-токена каждое; без лимита 250 слов
           на кириллице превышают лимит Whisper 224 токена в ~3×.
         - Иначе → min(max_words, _MAX_WORDS_LATIN).
       (W873-4 MEDIUM fix)
    4. Если заданы hotwords -- добавляет "Glossary: term1, term2. ".
    5. Если code_switching_detect=True и последний item содержит смешение
       кириллицы/латиницы выше code_switching_threshold -- добавляет hint для Whisper.

    Args:
        history_items: Список HistoryItem или dict (text, ts). newest-first.
        hotwords: Пользовательские термины для boosting. None/[] -- без эффекта.
        max_words: Верхняя граница числа слов (caller-supplied); перекрывается
                   языковым лимитом если он строже (см. _MAX_WORDS_CYRILLIC /
                   _MAX_WORDS_LATIN).
        max_age_seconds: Максимальный возраст элемента в секундах.
        history_limit: Сколько последних элементов рассматривать.
        auto_glossary: Автоматически извлечённые термины из истории (AutoGlossaryBuilder).
                       Объединяются с hotwords; дубликаты (case-insensitive) удаляются.
                       hotwords имеют приоритет над auto_glossary.
        code_switching_detect: Включить детектирование RU+EN смешения.
        code_switching_threshold: Минимальная доля вторичного языка (0.1 = 10%).

    Returns:
        Строка initial_prompt (может быть пустой).
    """
    now = time.time()

    recent = list(history_items[:history_limit])
    recent.reverse()

    texts: list[str] = []
    for item in recent:
        if isinstance(item, dict):
            raw_text: str = str(item.get("text", "")).strip()
            raw_ts: str = str(item.get("ts", "")).strip()
        else:
            raw_text = str(getattr(item, "text", "")).strip()
            raw_ts = str(getattr(item, "ts", "")).strip()

        if not raw_text:
            continue

        if raw_ts:
            age = now - _iso_to_epoch(raw_ts)
            if age > max_age_seconds:
                continue

        texts.append(raw_text)

    combined = _ITEM_SEP.join(texts).strip()
    if combined:
        words = combined.split()
        # W873-4 MEDIUM: choose word cap based on script to stay within the
        # Whisper BPE 224-token limit for initial_prompt.  Cyrillic text uses
        # ~2×-more tokens per word than Latin due to morphological richness.
        if _CYRILLIC_RE.search(combined):
            effective_max = min(max_words, _MAX_WORDS_CYRILLIC)
        else:
            effective_max = min(max_words, _MAX_WORDS_LATIN)
        if len(words) > effective_max:
            words = words[-effective_max:]
        combined = " ".join(words)

    parts: list[str] = []

    # Объединяем hotwords (приоритет) + auto_glossary с дедупликацией (case-insensitive).
    seen_lower: set[str] = set()
    combined_terms: list[str] = []
    for w in list(hotwords or []) + list(auto_glossary or []):
        w = w.strip()
        if not w:
            continue
        key = w.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            combined_terms.append(w)
        if len(combined_terms) >= _MAX_COMBINED_TERMS:
            break

    if combined_terms:
        parts.append(f"Glossary: {', '.join(combined_terms)}.")

    if combined:
        parts.append(f"Previous transcript: {combined}")

    # Code-switching hint: если в последнем item обнаружено смешение RU+EN.
    if code_switching_detect and history_items:
        last_item = history_items[0]
        if isinstance(last_item, dict):
            last_text = str(last_item.get("text", "")).strip()
        else:
            last_text = str(getattr(last_item, "text", "")).strip()
        if last_text:
            det = _get_detector(threshold=code_switching_threshold)
            cs_result = det.analyze(last_text)
            if cs_result["is_mixed"]:
                parts.append(_CODE_SWITCHING_HINT)

    return " ".join(parts)
