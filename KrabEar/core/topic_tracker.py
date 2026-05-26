"""topic_tracker.py — отслеживание смены тем разговора в транскрибациях.

Использует скользящее окно и TF-IDF-подобное взвешивание для детекции
изменений топика без внешних зависимостей.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import List, Dict

# ── Стоп-слова (объединённые для всех поддерживаемых языков) ────────────────

_STOP_WORDS: frozenset = frozenset({
    # Русские
    "в", "на", "с", "по", "из", "от", "до", "за", "под", "над", "к", "о",
    "об", "про", "при", "для", "без", "через", "между", "перед", "после",
    "во", "со", "ко", "и", "а", "но", "да", "то", "или", "что", "как",
    "если", "хотя", "пока", "когда", "чтобы", "потому", "поэтому",
    "не", "ни", "бы", "же", "ли", "вот", "ну", "уже", "ещё", "еще",
    "даже", "только", "лишь", "он", "она", "оно", "они", "мы", "вы", "я",
    "его", "её", "ее", "их", "мой", "твой", "наш", "ваш", "свой",
    "этот", "это", "эта", "такой", "такие", "такая", "сам", "сама",
    "весь", "вся", "всё", "все", "быть", "есть", "был", "была", "были",
    "было", "будет", "стать", "там", "здесь", "тут", "где",
    "очень", "более", "менее", "больше", "меньше", "совсем", "просто",
    "так", "всегда", "никогда", "иногда", "сейчас", "теперь",
    "можно", "нужно", "надо", "нет", "хорошо", "ладно", "который",
    "которая", "которое", "которые", "один", "одна", "одно",
    # Испанские
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "de", "del", "al", "en", "con", "por", "para", "sin", "sobre",
    "entre", "ante", "bajo", "desde", "hasta", "hacia",
    "y", "e", "o", "u", "pero", "sino", "que", "como", "si", "aunque",
    "porque", "cuando", "mientras", "donde", "pues",
    "yo", "tú", "él", "ella", "nosotros", "ellos", "ellas",
    "me", "te", "le", "nos", "os", "les", "lo",
    "este", "esta", "estos", "estas", "ese", "esa",
    "es", "son", "era", "fue", "ser", "estar", "hay", "tener",
    "no", "sí", "ya", "más", "muy", "bien", "también", "así",
    "todo", "todos", "aquí", "ahora", "antes", "después",
    "siempre", "nunca", "algo", "nada", "mucho", "poco",
    # Английские
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "with",
    "by", "from", "up", "about", "into", "through", "during", "before",
    "after", "above", "below", "between", "out", "off", "over", "under",
    "and", "but", "or", "nor", "so", "yet", "although", "because",
    "if", "since", "though", "unless", "until", "when", "while",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her",
    "us", "them", "my", "your", "his", "our", "their", "its",
    "this", "that", "these", "those", "which", "who", "whom",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "must",
    "not", "no", "never", "always", "often", "just", "only", "even",
    "also", "too", "very", "much", "many", "more", "most", "some",
    "any", "all", "each", "every", "few", "other", "same", "than",
    "then", "there", "here", "where", "how", "what", "why", "as",
})

_WORD_RE = re.compile(r"[А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñÜü]{3,}", re.UNICODE)

# Hard limit on items passed to track_topics — prevents IPC DoS (W1277 F2).
# A caller passing 5000 items caused 103s block on the single-threaded IPC loop.
_HARD_MAX_ITEMS: int = 500


# ── Dataclass результата ─────────────────────────────────────────────────────

@dataclass
class TopicSegment:
    """Сегмент разговора с определённой темой."""
    start_index: int
    end_index: int
    topic_words: List[str]
    summary: str
    items_count: int = field(init=False)

    def __post_init__(self):
        self.items_count = self.end_index - self.start_index + 1

    def to_dict(self) -> dict:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "topic_words": self.topic_words,
            "summary": self.summary,
            "items_count": self.items_count,
        }


# ── Вспомогательные функции ──────────────────────────────────────────────────

def _extract_text(item: dict) -> str:
    """Извлекает текст из записи истории."""
    return str(item.get("text") or item.get("source_text") or "")


def _tokenize(text: str) -> List[str]:
    """Разбивает текст на слова, фильтруя стоп-слова и короткие токены."""
    return [
        w.lower()
        for w in _WORD_RE.findall(text)
        if w.lower() not in _STOP_WORDS and len(w) >= 3
    ]


def _compute_tfidf(
    window_tokens: List[str],
    all_windows_tokens: List[List[str]],
) -> Dict[str, float]:
    """Вычисляет TF-IDF-подобные веса для слов текущего окна.

    Args:
        window_tokens: токены текущего окна (одного сегмента).
        all_windows_tokens: токены всех окон (для расчёта IDF).
    Returns:
        Словарь {слово: вес}.
    """
    if not window_tokens:
        return {}

    total_windows = len(all_windows_tokens) or 1
    tf: Counter = Counter(window_tokens)
    total_tf = len(window_tokens)

    scores: Dict[str, float] = {}
    for word, count in tf.items():
        tf_val = count / total_tf
        # Количество окон, содержащих это слово.
        # W1277 F4: convert each window token list to set before `in` for O(1)
        # membership vs O(n) list scan — 3.1s → 0.6s for n=500.
        doc_freq = sum(
            1 for w_tokens in all_windows_tokens if word in set(w_tokens)
        )
        idf_val = math.log((total_windows + 1) / (doc_freq + 1)) + 1.0
        scores[word] = tf_val * idf_val

    return scores


def _keyword_overlap(keywords_a: List[str], keywords_b: List[str]) -> float:
    """Возвращает долю пересечения двух наборов ключевых слов (0.0–1.0)."""
    if not keywords_a or not keywords_b:
        return 0.0
    set_a = set(keywords_a)
    set_b = set(keywords_b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _top_keywords(scores: Dict[str, float], top_n: int = 8) -> List[str]:
    """Возвращает top_n слов по убыванию TF-IDF-веса."""
    return [w for w, _ in sorted(scores.items(), key=lambda x: -x[1])[:top_n]]


def _make_summary(topic_words: List[str], max_words: int = 5) -> str:
    """Строит краткое описание темы из ключевых слов."""
    words = topic_words[:max_words]
    if not words:
        return "неизвестная тема"
    return ", ".join(words)


# ── Основной класс ───────────────────────────────────────────────────────────

class TopicTracker:
    """Отслеживает смену тем разговора в списке транскрибаций.

    Алгоритм:
    1. Разбиваем элементы на скользящие окна размером window_size.
    2. Для каждого окна вычисляем TF-IDF-взвешенные ключевые слова.
    3. Сравниваем соседние окна: если пересечение ключевых слов < 30%,
       фиксируем смену темы.
    4. Формируем TopicSegment для каждого непрерывного тематического блока.

    Пример::

        tracker = TopicTracker()
        segments = tracker.track_topics(history_items)
        timeline = tracker.get_topic_timeline(history_items)
        current = tracker.get_current_topic(history_items)
    """

    # Порог пересечения, ниже которого считаем тему изменившейся
    SHIFT_THRESHOLD: float = 0.30

    def track_topics(
        self,
        items: List[dict],
        window_size: int = 5,
    ) -> List[TopicSegment]:
        """Определяет тематические сегменты в списке транскрибаций.

        Args:
            items:       список записей истории (поле ``text`` или ``source_text``).
            window_size: размер скользящего окна (минимум 1).
        Returns:
            Список TopicSegment — хронологически упорядоченных сегментов.
        """
        if not items:
            return []

        # Hard cap regardless of caller — prevents IPC DoS (W1277 F2).
        if len(items) > _HARD_MAX_ITEMS:
            items = items[-_HARD_MAX_ITEMS:]

        window_size = max(1, window_size)
        n = len(items)

        # Токены для каждого элемента
        item_tokens: List[List[str]] = [_tokenize(_extract_text(item)) for item in items]

        # Токены по окнам
        window_tokens: List[List[str]] = []
        for i in range(n):
            wend = min(i + window_size, n)
            merged: List[str] = []
            for j in range(i, wend):
                merged.extend(item_tokens[j])
            window_tokens.append(merged)

        # TF-IDF для каждого окна
        window_keywords: List[List[str]] = []
        for i in range(n):
            scores = _compute_tfidf(window_tokens[i], window_tokens)
            window_keywords.append(_top_keywords(scores, top_n=8))

        # Детекция переходов между темами
        # Начинаем первый сегмент с индекса 0
        segment_starts = [0]
        for i in range(1, n):
            overlap = _keyword_overlap(window_keywords[i - 1], window_keywords[i])
            if overlap < self.SHIFT_THRESHOLD:
                segment_starts.append(i)

        # Формируем TopicSegment для каждого перехода
        segments: List[TopicSegment] = []
        for idx, start in enumerate(segment_starts):
            end = (segment_starts[idx + 1] - 1) if idx + 1 < len(segment_starts) else n - 1

            # Объединяем токены всего сегмента для итоговых ключевых слов
            seg_tokens: List[str] = []
            for j in range(start, end + 1):
                seg_tokens.extend(item_tokens[j])

            seg_scores = _compute_tfidf(seg_tokens, window_tokens)
            topic_words = _top_keywords(seg_scores, top_n=8)
            summary = _make_summary(topic_words)

            segments.append(TopicSegment(
                start_index=start,
                end_index=end,
                topic_words=topic_words,
                summary=summary,
            ))

        return segments

    def get_topic_timeline(self, items: List[dict], window_size: int = 5) -> List[dict]:
        """Возвращает хронологический таймлайн смен тем.

        Args:
            items:       список записей истории.
            window_size: размер окна для track_topics.
        Returns:
            Список dict с полями: start_index, end_index, topic_words,
            summary, items_count, is_shift (True — новая тема).
        """
        segments = self.track_topics(items, window_size=window_size)
        timeline: List[dict] = []
        for i, seg in enumerate(segments):
            entry = seg.to_dict()
            entry["is_shift"] = i > 0  # первый сегмент не является сменой
            timeline.append(entry)
        return timeline

    def get_current_topic(self, items: List[dict], last_n: int = 5) -> dict:
        """Определяет текущую тему разговора по последним записям.

        Args:
            items:  список записей истории.
            last_n: количество последних элементов для анализа.
        Returns:
            Dict с полями: topic_words, summary, items_count, start_index.
        """
        if not items:
            return {
                "topic_words": [],
                "summary": "нет данных",
                "items_count": 0,
                "start_index": 0,
            }

        last_n = max(1, last_n)
        window = items[-last_n:]
        start_index = max(0, len(items) - last_n)

        tokens: List[str] = []
        for item in window:
            tokens.extend(_tokenize(_extract_text(item)))

        if not tokens:
            return {
                "topic_words": [],
                "summary": "нет данных",
                "items_count": len(window),
                "start_index": start_index,
            }

        tf: Counter = Counter(tokens)
        total = len(tokens)
        # Простой TF без IDF для текущей темы (только одно окно)
        scores = {w: count / total for w, count in tf.items()}
        topic_words = _top_keywords(scores, top_n=8)

        return {
            "topic_words": topic_words,
            "summary": _make_summary(topic_words),
            "items_count": len(window),
            "start_index": start_index,
        }
