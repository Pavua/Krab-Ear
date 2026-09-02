"""Утилита построения initial_prompt для Whisper STT из недавней истории.

Использует последние N элементов истории и пользовательские hotwords
для формирования контекстного подсказа, который помогает Whisper
лучше распознавать продолжение диктовки и терминологию пользователя.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from core.code_switching_detector import CodeSwitchingDetector

logger = logging.getLogger(__name__)

# Горизонт давности: элементы старше этого порога не используются как контекст.
_MAX_AGE_SECONDS: int = 30 * 60  # 30 минут

# Количество последних элементов истории для просмотра.
_DEFAULT_HISTORY_LIMIT: int = 10

# Разделитель между элементами в контексте.
_ITEM_SEP: str = " "

# Максимальное число терминов в объединённом глоссарии (hotwords + auto_glossary).
_MAX_COMBINED_TERMS: int = 250

# Максимальная длина итогового initial_prompt в символах.
# Основано на лимите Whisper в 224 токена × 2.5 символа/токен (Кириллица — worst-case BPE).
# При 250 многословных терминах один раздел Glossary может превысить лимит в 3×.
_MAX_PROMPT_CHARS: int = 560

# W23 MED: per-item character cap before joining.  A single planted item cannot
# fill the whole _MAX_PROMPT_CHARS budget.  We take only the LAST N chars of
# each item text (tail bias = most recent words, consistent with the word-tail
# slicing done on the joined combined string below).
_MAX_ITEM_CHARS: int = 200

# W23 MED: strip leading imperative/markup prefixes from the Previous-transcript
# section to reduce prompt-injection blast radius.  Matched case-insensitively
# at the start of each item contribution and at the start of the joined block.
_INJECTION_PREFIX_RE = re.compile(
    r"^(?:SYSTEM|IGNORE\s+(?:ABOVE|PREVIOUS)|USER|ASSISTANT|PROMPT|INSTRUCTION|CONTEXT)"
    r"\s*:\s*",
    re.IGNORECASE,
)

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


def merge_language_hotwords(
    common: "list[str] | None",
    language: "str | None",
    per_language: "dict[str, list[str]] | None",
) -> "list[str]":
    """Склеивает общий словарь подсказок с языковым.

    Зачем: бюджет `initial_prompt` у Whisper — 224 токена, и на живых диктовках
    он уже режется (лог: 948 знаков → 560). Испанский медицинский словарь в
    русской диктовке бесполезен, но место выкупает у контекста истории. Поэтому
    языковые термины подключаются только когда распознан их язык.

    Порядок значим: общий список идёт ПЕРВЫМ. Обрезка промпта идёт с конца, а
    общие термины относятся ко всем диктовкам — терять их из-за доменного
    списка неправильно.

    Язык приходит от движков в разном виде (`es`, `ES`, `es-ES`), поэтому
    сравнивается только первый сегмент в нижнем регистре. Неизвестный или
    неопределённый язык — обычная ситуация, а не ошибка: возвращается общий
    список без потерь.
    """
    merged: list[str] = []
    seen: set[str] = set()

    def _add(items: "list[str] | None") -> None:
        for item in items or []:
            text = str(item).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)

    _add(common)
    if language and per_language:
        code = str(language).strip().lower().replace("_", "-").split("-")[0]
        if code:
            _add(per_language.get(code))
    return merged


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
    if max_words <= 0:
        return ""

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
            # W23 LOW: clamp age to >=0 so future-dated items are treated as
            # age=0 (included) rather than receiving a large negative age that
            # could bypass the staleness horizon in unexpected ways.
            age = max(0.0, now - _iso_to_epoch(raw_ts))
            if age > max_age_seconds:
                continue

        # W23 MED: per-item character cap — take only the last _MAX_ITEM_CHARS
        # characters so a single planted item cannot dominate the whole prompt.
        item_text = raw_text
        if len(item_text) > _MAX_ITEM_CHARS:
            item_text = item_text[-_MAX_ITEM_CHARS:]
            # Re-align to word boundary: drop a possible partial leading word.
            space_pos = item_text.find(" ")
            if 0 < space_pos < _MAX_ITEM_CHARS // 2:
                item_text = item_text[space_pos + 1:]

        # W23 MED: strip obvious imperative/markup prefix tokens that a planted
        # item might use to steer Whisper (e.g. "SYSTEM: ignore above").
        item_text = _INJECTION_PREFIX_RE.sub("", item_text).strip()
        if not item_text:
            continue

        texts.append(item_text)

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

    prompt = " ".join(parts)

    # Обрезаем до _MAX_PROMPT_CHARS, чтобы не превысить лимит 224 токена Whisper.
    # Эвристика BPE: ~2.5 символа/токен для кириллицы (worst-case).
    if len(prompt) > _MAX_PROMPT_CHARS:
        orig_len = len(prompt)
        capped = prompt[:_MAX_PROMPT_CHARS]
        # Если обрезка попала в середину глоссарного термина (после запятой),
        # откатываемся до последней полной запятой, чтобы не оставлять рваный конец.
        last_comma = capped.rfind(",")
        last_period = capped.rfind(".")
        # Граница: последний разделитель терминов (запятая) или конец секции (точка)
        cut_at = max(last_comma, last_period)
        if cut_at > _MAX_PROMPT_CHARS // 2:
            capped = capped[:cut_at + 1].rstrip()
        else:
            capped = capped.rstrip()
        logger.info(
            "initial_prompt truncated to fit Whisper 224-token limit "
            "(input %d chars → %d chars)",
            orig_len,
            len(capped),
        )
        return capped

    return prompt
