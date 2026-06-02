"""context_memory.py — контекстная память для улучшения распознавания STT.

Хранит скользящее окно последних N транскрибаций и извлекает из них
«заметные» слова (имена собственные, технические термины, аббревиатуры)
для использования в качестве подсказок whisper (extra_vocabulary).
"""

from __future__ import annotations

import re
import threading
from collections import Counter, deque
from typing import Deque, List

# ── Переиспользуем стоп-слова из term_extractor ─────────────────────────────
try:
    from core.term_extractor import _ALL_STOP_WORDS as _STOP_WORDS
except ImportError:
    # Fallback при прямом запуске / тестах
    _STOP_WORDS: frozenset = frozenset([
        "the", "and", "that", "this", "with", "from", "have", "been",
        "will", "would", "could", "should", "which", "their", "there",
        "they", "them", "then", "than", "when", "what", "where", "who",
        "how", "but", "for", "not", "are", "was", "were", "has", "had",
        "быть", "было", "была", "были", "этот", "этой", "этих",
        "который", "которая", "которые", "может", "можно", "если",
        "это", "при", "или", "для", "как", "что", "так", "его", "её",
        "они", "она", "оно", "мне", "мы", "вы", "он", "но", "от", "до",
        "по", "из", "на", "в", "с", "к", "о", "у", "за", "не", "и", "а",
        "то", "да", "же", "бы", "был",
        "pero", "para", "como", "desde", "este", "esta", "esto",
        "que", "del", "los", "las", "por", "con", "una", "uno",
    ])

# ── Константы ────────────────────────────────────────────────────────────────

# Максимальная длина текста для анализа (защита от ReDoS на длинных токенах)
MAX_NOTABLE_TEXT_LEN = 8000
# Токены длиннее этого порога пропускаются перед применением _RE_TECH
_MAX_TECH_TOKEN_LEN = 64

# ── Regex-паттерны для извлечения заметных слов ─────────────────────────────

# Аббревиатуры (2+ заглавных букв)
_RE_ABBREV = re.compile(r"\b([A-ZА-Я]{2,})\b")
# CamelCase
_RE_CAMEL = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
# Технические термины с цифрами (GPT-4o, iPhone13, Python3, qwen3, mlx4bit).
# ВАЖНО: применять только к отдельным коротким токенам (≤_MAX_TECH_TOKEN_LEN),
# иначе второй вариант [A-Za-z0-9\-]*[0-9]+[A-Za-z]+ создаёт квадратичный ReDoS
# из-за пересечения char-класса префикса с [0-9]+ (MED, wave-18).
_RE_TECH = re.compile(
    r"\b([A-Za-zА-Яа-я]+[0-9]+[A-Za-zА-Яа-я0-9\-]*"
    r"|[A-Za-zА-Яа-я0-9\-]*[0-9]+[A-Za-zА-Яа-я]+)\b"
)
# Разбивка на пробельные токены для безопасного применения _RE_TECH
_RE_WHITESPACE = re.compile(r"\s+")
# Заглавные слова в середине предложения (не первое слово)
_RE_CAP_MID = re.compile(r"(?<=[.!?\s])([А-ЯA-Z][А-Яа-яa-z]{2,})\b")
# Длинные слова (7+ символов) не из стоп-списка — вероятно предметная лексика
_RE_LONG_WORD = re.compile(r"\b([А-Яа-яA-Za-zÁÉÍÓÚáéíóúÑñ]{7,})\b")
# Разбиение текста на предложения
_RE_SENT_SPLIT = re.compile(r"[.!?]+")
# Очистка слова от не-буквенных символов (кроме дефиса)
_RE_WORD_CLEAN = re.compile(r"[^\wА-Яа-яÁÉÍÓÚáéíóúÑñ-]")

# Минимальная частота для включения в контекстные слова
_MIN_WORD_FREQ = 1
_MIN_TOPIC_WORD_LEN = 4


def _extract_notable_words(text: str) -> List[str]:
    """Извлекает заметные слова из одной транскрибации.

    Стратегии:
    - Аббревиатуры (GPT, API, IPC, ЭКГ)
    - CamelCase идентификаторы (BackendService, AudioEngine)
    - Технические термины с цифрами (GPT-4, Python3)
    - Заглавные слова в середине предложения (имена собственные)
    - Длинные слова (≥7 символов), не являющиеся стоп-словами
    """
    if not text or not text.strip():
        return []

    # Защита от ReDoS: ограничиваем длину входа (wave-18)
    text = text[:MAX_NOTABLE_TEXT_LEN]

    seen: set[str] = set()
    results: List[str] = []

    def _add(word: str) -> None:
        key = word.lower()
        if key in seen or key in _STOP_WORDS:
            return
        if len(word) < 2:
            return
        seen.add(key)
        results.append(word)

    for m in _RE_ABBREV.finditer(text):
        _add(m.group(1))

    for m in _RE_CAMEL.finditer(text):
        _add(m.group(1))

    # _RE_TECH содержит квадратично-опасный вариант при длинных токенах.
    # Применяем только к коротким токенам (≤_MAX_TECH_TOKEN_LEN) — safe O(n).
    for token in _RE_WHITESPACE.split(text):
        if len(token) > _MAX_TECH_TOKEN_LEN:
            continue
        for m in _RE_TECH.finditer(token):
            _add(m.group(1))

    # Заглавные слова не в начале предложения
    for sent in _RE_SENT_SPLIT.split(text):
        words = sent.split()
        for i, word in enumerate(words):
            if i == 0:
                continue
            clean = _RE_WORD_CLEAN.sub("", word)
            if not clean or len(clean) < 3:
                continue
            if clean[0].isupper() and clean.lower() not in _STOP_WORDS:
                _add(clean)

    # Длинные предметные слова
    for m in _RE_LONG_WORD.finditer(text):
        word = m.group(1)
        if word.lower() not in _STOP_WORDS:
            _add(word)

    return results


class ContextMemory:
    """Скользящая контекстная память для улучшения STT-распознавания.

    Хранит последние `window_size` транскрибаций и извлекает из них
    наиболее значимые слова для передачи в whisper как extra_vocabulary.

    Thread-safe.
    """

    def __init__(self, window_size: int = 50) -> None:
        """Инициализирует контекстную память.

        Args:
            window_size: размер скользящего окна (кол-во последних транскрибаций).
        """
        self._window_size = window_size
        self._lock = threading.RLock()
        # Скользящее окно транскрибаций (сырой текст)
        self._texts: Deque[str] = deque(maxlen=window_size)
        # Агрегированные счётчики слов по всему окну
        self._word_counter: Counter = Counter()
        # Извлечённые слова для каждой позиции окна (для корректного вычитания)
        self._word_lists: Deque[List[str]] = deque(maxlen=window_size)

    # ── Публичный API ────────────────────────────────────────────────────────

    def update(self, text: str) -> None:
        """Добавляет новую транскрибацию в скользящее окно.

        Если окно заполнено, самая старая транскрибация вычитается из счётчиков.

        Args:
            text: новая транскрибация (уже очищенная).
        """
        if not text or not text.strip():
            return

        words = _extract_notable_words(text)

        with self._lock:
            # Если окно полное — вычитаем слова вытесняемой записи
            if len(self._word_lists) == self._window_size:
                evicted = self._word_lists[0]  # будет вытеснен при append
                for w in evicted:
                    key = w.lower()
                    self._word_counter[key] -= 1
                    if self._word_counter[key] <= 0:
                        del self._word_counter[key]

            self._texts.append(text)
            self._word_lists.append(words)
            for w in words:
                self._word_counter[w.lower()] += 1

    def get_context_words(self, max_words: int = 20) -> List[str]:
        """Возвращает список наиболее частых контекстных слов для STT.

        Слова отсортированы по убыванию частоты встречаемости в окне.

        Args:
            max_words: максимальное количество возвращаемых слов.
        Returns:
            Список строк — подсказки для STT (extra_vocabulary).
        """
        with self._lock:
            if not self._word_counter:
                return []
            top = self._word_counter.most_common(max_words)
            # Возвращаем оригинальный регистр из последней встречи
            # (храним ключи в lower, но оригинал берём из word_lists)
            key_to_orig: dict[str, str] = {}
            for word_list in reversed(self._word_lists):
                for w in word_list:
                    k = w.lower()
                    if k not in key_to_orig:
                        key_to_orig[k] = w
            return [key_to_orig.get(k, k) for k, _ in top]

    def get_recent_topics(self, max_topics: int = 5, last_n: int = 10) -> List[str]:
        """Извлекает основные темы из последних N транскрибаций.

        Возвращает наиболее часто встречающиеся значимые слова/термины
        из последних `last_n` транскрибаций скользящего окна.

        Args:
            max_topics: максимальное кол-во тем.
            last_n: кол-во последних транскрибаций для анализа.
        Returns:
            Список строк — главные темы.
        """
        with self._lock:
            # Берём последние last_n пар (text, words)
            recent_word_lists = list(self._word_lists)[-last_n:]

        if not recent_word_lists:
            return []

        counter: Counter = Counter()
        key_to_orig: dict[str, str] = {}
        for word_list in recent_word_lists:
            for w in word_list:
                if len(w) < _MIN_TOPIC_WORD_LEN:
                    continue
                k = w.lower()
                counter[k] += 1
                if k not in key_to_orig:
                    key_to_orig[k] = w

        top = counter.most_common(max_topics)
        return [key_to_orig.get(k, k) for k, _ in top]

    def clear(self) -> None:
        """Очищает всю контекстную память."""
        with self._lock:
            self._texts.clear()
            self._word_lists.clear()
            self._word_counter.clear()

    def size(self) -> int:
        """Возвращает текущее кол-во транскрибаций в окне."""
        with self._lock:
            return len(self._texts)

    def to_dict(self) -> dict:
        """Сериализует состояние контекстной памяти для IPC-ответа."""
        with self._lock:
            top = list(self._word_counter.most_common(20))
            current_size = len(self._texts)
            # RLock позволяет повторный захват из того же потока
            context_words = self.get_context_words(max_words=20)
            recent_topics = self.get_recent_topics()
            return {
                "window_size": self._window_size,
                "current_size": current_size,
                "context_words": context_words,
                "recent_topics": recent_topics,
                "top_words": [{"word": w, "count": c} for w, c in top],
            }
